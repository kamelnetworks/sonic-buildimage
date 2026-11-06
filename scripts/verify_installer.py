#!/usr/bin/env python3
"""Read-only check of a SONiC ONIE installer (sonic-<platform>.bin).

The installer is never executed. The checks follow the layout written by
onie-mk-demo.sh and build_debian.sh:

  sonic-<platform>.bin
    shell header (installer/sharch_body.sh) ... 'exit_marker'
    payload: tar of installer/ (payload_image_size bytes, payload_sha1)
      installer/install.sh, machine.conf, platforms_asic, onie-image.conf, ...
      installer/fs.zip
        boot/vmlinuz-<kver>, boot/initrd.img-<kver>
        fs.squashfs
        platform.tar.gz   ./<platform>/*.deb, ./common/, ./grub/
        dockerfs.tar.gz   (or next to fs.zip in installer/ when >= 4 GiB)
    [CMS signature, only for SECURE_UPGRADE_MODE=dev/prod]

What is checked:
  - the exit_marker boundary, payload size and SHA-1, and trailing bytes;
  - the outer tar, and the installer files install.sh needs;
  - every CRC in fs.zip, and that it holds kernel, initrd, squashfs, platform
    and dockerfs;
  - a full gzip/zstd + tar scan of dockerfs and platform.tar.gz;
  - for each expected platform: it is in platforms_asic, platform.tar.gz has
    its .deb(s), every kernel module in them has the image kernel's vermagic,
    and (with unsquashfs) its device directory is in fs.squashfs.

The expected platforms default to the release platforms for the ASIC named
in machine.conf (RELEASE_PLATFORMS); --platform replaces that list.

Only the Python standard library is needed. zstd-compressed members need the
'zstd' tool, and the squashfs listing needs 'unsquashfs'; a check whose tool
is missing is reported as SKIP (a failure with --strict).

Exit status: 0 if every check passed, 1 if any failed, 2 on usage errors.
"""

import argparse
import gzip
import hashlib
import io
import lzma
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

# Release platforms per ASIC (machine.conf 'machine='), see AGENTS.md.
RELEASE_PLATFORMS = {
    'marvell-teralynx': ['x86_64-cel_silverstone-x-r0'],
    'broadcom': ['x86_64-dellemc_s5232f_c3538-r0', 'x86_64-cel_seastone_2-r0'],
}

EXIT_MARKER = b'\nexit_marker\n'
# Files install.sh and sharch_body.sh need in the payload's installer/ dir.
INSTALLER_FILES = ['install.sh', 'machine.conf', 'platforms_asic',
                   'onie-image.conf', 'platform.conf', 'default_platform.conf']
FS_ZIP = 'fs.zip'
SQUASHFS = 'fs.squashfs'
PLATFORM_TAR = 'platform.tar.gz'
DOCKERFS = 'dockerfs.tar.gz'
DEVICE_FILES = ['platform_asic', 'default_sku']
CHUNK = 1 << 20
# Refuse absolute paths and '..' when extracting (Python 3.12+; 3.14 default).
TAR_FILTER = {'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}

GZIP_MAGIC = b'\x1f\x8b'
ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'
XZ_MAGIC = b'\xfd7zXZ\x00'


class Report:
    def __init__(self, strict=False, out=sys.stdout):
        self.strict = strict
        self.out = out
        self.failed = 0
        self.skipped = 0

    def ok(self, msg):
        print('PASS  ' + msg, file=self.out)

    def fail(self, msg):
        self.failed += 1
        print('FAIL  ' + msg, file=self.out)

    def skip(self, msg):
        self.skipped += 1
        if self.strict:
            self.fail(msg + ' (skipped, --strict)')
        else:
            print('SKIP  ' + msg, file=self.out)

    def info(self, msg):
        print('INFO  ' + msg, file=self.out)

    def check(self, cond, msg):
        (self.ok if cond else self.fail)(msg)
        return cond


class SliceFile(io.RawIOBase):
    """A read-only, seekable window [offset, offset + size) of a file."""

    def __init__(self, f, offset, size):
        self.f, self.offset, self.size, self.pos = f, offset, size, 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, pos, whence=io.SEEK_SET):
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = max(0, base + pos)
        return self.pos

    def readinto(self, b):
        n = max(0, min(len(b), self.size - self.pos))
        if n == 0:
            return 0
        self.f.seek(self.offset + self.pos)
        data = self.f.read(n)
        b[:len(data)] = data
        self.pos += len(data)
        return len(data)


def parse_header(header):
    """Return (payload_image_size, payload_sha1) from the shell header."""
    text = header.decode('utf-8', 'replace')
    size = re.search(r'^payload_image_size=(\d+)\s*$', text, re.M)
    sha1 = re.search(r'^payload_sha1=([0-9a-f]{40})\s*$', text, re.M)
    return (int(size.group(1)) if size else None, sha1.group(1) if sha1 else None)


def parse_kv(text):
    result = {}
    for line in text.splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            result[k.strip()] = v.strip().strip('"\'')
    return result


def find_marker(f):
    """Offset of the first byte after '\\nexit_marker\\n', or None."""
    f.seek(0)
    tail, pos = b'', 0
    while True:
        chunk = f.read(CHUNK)
        if not chunk:
            return None
        buf = tail + chunk
        idx = buf.find(EXIT_MARKER)
        if idx >= 0:
            return pos - len(tail) + idx + len(EXIT_MARKER)
        tail = buf[-(len(EXIT_MARKER) - 1):]
        pos += len(chunk)


def open_compressed(path):
    """A readable stream of the decompressed file, or (None, reason)."""
    with open(path, 'rb') as f:
        magic = f.read(6)
    if magic.startswith(GZIP_MAGIC):
        return gzip.open(path, 'rb'), None
    if magic.startswith(XZ_MAGIC):
        return lzma.open(path, 'rb'), None
    if magic.startswith(ZSTD_MAGIC):
        if not shutil.which('zstd'):
            return None, 'zstd-compressed and no zstd tool'
        proc = subprocess.Popen(['zstd', '-dc', path], stdout=subprocess.PIPE)
        return proc.stdout, None
    return open(path, 'rb'), None


def scan_tar(path):
    """Read a (compressed) tar to the end. Returns (members, error, skip)."""
    stream, reason = open_compressed(path)
    if stream is None:
        return None, None, reason
    count = 0
    try:
        with stream, tarfile.open(fileobj=stream, mode='r|') as tar:
            for member in tar:
                count += 1
                if member.isfile():
                    fobj = tar.extractfile(member)
                    while fobj.read(CHUNK):
                        pass
    except (tarfile.TarError, OSError, EOFError, lzma.LZMAError) as e:
        return count, '%s: %s' % (type(e).__name__, e), None
    return count, None, None


def ar_members(data):
    """Yield (name, bytes) of a Debian ar archive."""
    if not data.startswith(b'!<arch>\n'):
        raise ValueError('not an ar archive')
    pos = 8
    while pos + 60 <= len(data):
        hdr = data[pos:pos + 60]
        name = hdr[:16].decode().strip().rstrip('/')
        size = int(hdr[48:58].decode().strip())
        pos += 60
        yield name, data[pos:pos + size]
        pos += size + (size & 1)


def deb_tar(blob):
    """A tarfile for a control/data member of a .deb, or (None, reason)."""
    if blob.startswith(ZSTD_MAGIC):
        if not shutil.which('zstd'):
            return None, 'zstd-compressed and no zstd tool'
        blob = subprocess.run(['zstd', '-dc'], input=blob, stdout=subprocess.PIPE,
                              check=True).stdout
    elif blob.startswith(XZ_MAGIC):
        blob = lzma.decompress(blob)
    elif blob.startswith(GZIP_MAGIC):
        blob = gzip.decompress(blob)
    return tarfile.open(fileobj=io.BytesIO(blob), mode='r:'), None


def vermagic(ko):
    m = re.search(rb'vermagic=([^\x00]*)', ko)
    return m.group(1).decode('utf-8', 'replace') if m else None


def check_deb(report, name, data, kver):
    """Check one platform .deb: its kernel modules match the image kernel."""
    try:
        members = dict(ar_members(data))
    except ValueError as e:
        report.fail('%s: %s' % (name, e))
        return
    data_member = next((k for k in members if k.startswith('data.tar')), None)
    control_member = next((k for k in members if k.startswith('control.tar')), None)
    if data_member is None or control_member is None:
        report.fail('%s: no control.tar/data.tar member' % name)
        return
    tar, reason = deb_tar(members[data_member])
    if tar is None:
        report.skip('%s: kernel modules not checked (%s)' % (name, reason))
        return
    mods = []
    with tar:
        for m in tar:
            if m.isfile() and m.name.endswith('.ko'):
                mods.append((m.name, vermagic(tar.extractfile(m).read())))
    if not mods:
        report.info('%s: no kernel modules' % name)
        return
    bad = [(n, v) for n, v in mods if v is None or v.split()[0] != kver]
    if bad:
        for n, v in bad:
            report.fail('%s: %s has vermagic %r, image kernel is %s' % (name, n, v, kver))
    else:
        report.ok('%s: %d kernel module(s), all with vermagic %s' % (name, len(mods), kver))


def check_platform_tar(report, path, platforms, kver):
    """Scan platform.tar.gz and check each expected platform's packages.

    sonic_debian_extension.sh puts the packages in ./common/ and links them
    from ./<platform>/<pkg>.deb (a relative symlink, ../common/<pkg>.deb).
    """
    try:
        with tarfile.open(path, 'r:*') as tar:
            members = {os.path.normpath(m.name): m for m in tar.getmembers()}
            debs = {}
            for name, m in members.items():
                parts = name.split(os.sep)
                if len(parts) == 2 and parts[1].endswith('.deb') and (m.isfile() or m.issym()):
                    debs.setdefault(parts[0], []).append(m)
            report.ok('%s: tar is readable (%d directories with packages)' % (PLATFORM_TAR, len(debs)))
            for p in platforms:
                if not report.check(p in debs, '%s: %s has a platform package' % (PLATFORM_TAR, p)):
                    continue
                for m in debs[p]:
                    name = os.path.normpath(m.name)
                    if m.issym():
                        target = os.path.normpath(os.path.join(os.path.dirname(name), m.linkname))
                        if not report.check(target in members and members[target].isfile(),
                                            '%s: link to %s resolves' % (name, target)):
                            continue
                        m = members[target]
                    if kver is None:
                        report.skip('%s: kernel modules not checked (no kernel in boot/)' % name)
                        continue
                    check_deb(report, name, tar.extractfile(m).read(), kver)
    except (tarfile.TarError, OSError, EOFError) as e:
        report.fail('%s: %s: %s' % (PLATFORM_TAR, type(e).__name__, e))


def check_squashfs(report, path, platforms):
    if not shutil.which('unsquashfs'):
        report.skip('%s: device directories not checked (no unsquashfs)' % SQUASHFS)
        return
    proc = subprocess.run(['unsquashfs', '-l', '-d', '', path], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)
    if not report.check(proc.returncode == 0, '%s: listing (unsquashfs -l)' % SQUASHFS):
        report.info(proc.stderr.strip())
        return
    entries = set(line.strip().lstrip('/') for line in proc.stdout.splitlines())
    for p in platforms:
        base = 'usr/share/sonic/device/%s' % p
        missing = [f for f in DEVICE_FILES if '%s/%s' % (base, f) not in entries]
        report.check(not missing, '%s: /%s has %s%s' % (
            SQUASHFS, base, ', '.join(DEVICE_FILES),
            '' if not missing else ' (missing: %s)' % ', '.join(missing)))


def verify(installer, platforms=None, allow_trailing=False, strict=False,
           workdir=None, out=sys.stdout):
    """Run every check. Returns the Report."""
    report = Report(strict, out)
    size = os.path.getsize(installer)
    report.info('%s: %d bytes' % (installer, size))
    scratch = tempfile.mkdtemp(prefix='verify-installer-', dir=workdir)
    try:
        with open(installer, 'rb') as f:
            _verify(report, f, size, platforms, allow_trailing, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print('%s: %d failed, %d skipped' % ('FAILED' if report.failed else 'OK',
                                         report.failed, report.skipped), file=out)
    return report


def _verify(report, f, size, platforms, allow_trailing, scratch):
    # 1. Shell header and payload.
    start = find_marker(f)
    if not report.check(start is not None, 'exit_marker line found'):
        return
    f.seek(0)
    pl_size, pl_sha1 = parse_header(f.read(start))
    if not report.check(pl_size is not None and pl_sha1 is not None,
                        'header has payload_image_size and payload_sha1'):
        return
    if not report.check(start + pl_size <= size, 'payload is complete (%d bytes at offset %d)' %
                        (pl_size, start)):
        return
    sha1 = hashlib.sha1()
    f.seek(start)
    left = pl_size
    while left:
        chunk = f.read(min(CHUNK, left))
        sha1.update(chunk)
        left -= len(chunk)
    report.check(sha1.hexdigest() == pl_sha1, 'payload SHA-1 %s' % pl_sha1)
    trailing = size - start - pl_size
    if trailing == 0:
        report.ok('no trailing bytes after the payload')
    elif allow_trailing:
        report.info('%d trailing bytes after the payload (signature, --allow-trailing)' % trailing)
    else:
        report.fail('%d trailing bytes after the payload (a signed image needs --allow-trailing)'
                    % trailing)

    # 2. Outer tar.
    try:
        tar = tarfile.open(fileobj=SliceFile(f, start, pl_size), mode='r:')
        names = {os.path.normpath(n) for n in tar.getnames()}
    except tarfile.TarError as e:
        report.fail('payload tar: %s' % e)
        return
    report.ok('payload tar is readable (%d members)' % len(names))
    with tar:
        missing = [n for n in INSTALLER_FILES if 'installer/' + n not in names]
        report.check(not missing, 'installer/ has %s%s' % (
            ', '.join(INSTALLER_FILES), '' if not missing else ' (missing: %s)' % ', '.join(missing)))

        def read(name):
            return tar.extractfile('installer/' + name).read().decode('utf-8', 'replace')

        machine = parse_kv(read('machine.conf')).get('machine') if 'machine.conf' not in missing else None
        report.info('machine.conf: machine=%s' % machine)
        if platforms is None:
            platforms = RELEASE_PLATFORMS.get(machine, [])
            report.info('expected platforms for %s: %s' % (machine, ', '.join(platforms) or 'none'))
        asic_list = read('platforms_asic').split() if 'platforms_asic' not in missing else []
        for p in platforms:
            report.check(p in asic_list, 'platforms_asic lists %s' % p)

        if not report.check('installer/' + FS_ZIP in names, 'installer/%s present' % FS_ZIP):
            return
        for name in (FS_ZIP, DOCKERFS):
            if 'installer/' + name in names:
                tar.extract('installer/' + name, scratch, set_attrs=False, **TAR_FILTER)
    zip_path = os.path.join(scratch, 'installer', FS_ZIP)

    # 3. fs.zip.
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        report.fail('%s: %s' % (FS_ZIP, e))
        return
    with zf:
        bad = zf.testzip()
        report.check(bad is None, '%s: all %d CRCs%s' % (
            FS_ZIP, len(zf.namelist()), '' if bad is None else ' (first bad member: %s)' % bad))
        znames = set(zf.namelist())
        kernels = sorted(n[len('boot/vmlinuz-'):] for n in znames if n.startswith('boot/vmlinuz-'))
        kver = kernels[0] if len(kernels) == 1 else None
        if report.check(len(kernels) == 1, '%s: one kernel in boot/ (%s)' % (FS_ZIP, ', '.join(kernels) or 'none')):
            report.check('boot/initrd.img-%s' % kver in znames, '%s: boot/initrd.img-%s' % (FS_ZIP, kver))
        for name in (SQUASHFS, PLATFORM_TAR):
            if report.check(name in znames, '%s: %s present' % (FS_ZIP, name)):
                zf.extract(name, scratch)
        if DOCKERFS in znames:
            report.ok('%s: %s present' % (FS_ZIP, DOCKERFS))
            zf.extract(DOCKERFS, scratch)
            dockerfs = os.path.join(scratch, DOCKERFS)
        else:
            dockerfs = os.path.join(scratch, 'installer', DOCKERFS)
            report.check(os.path.exists(dockerfs), '%s next to %s (not inside it)' % (DOCKERFS, FS_ZIP))

    # 4. dockerfs, platform packages, squashfs.
    if os.path.exists(dockerfs):
        count, err, skip = scan_tar(dockerfs)
        if skip:
            report.skip('%s: not scanned (%s)' % (DOCKERFS, skip))
        else:
            report.check(err is None and count, '%s: decompresses and untars (%d members)%s' %
                         (DOCKERFS, count or 0, '' if err is None else ': ' + err))
    platform_tar = os.path.join(scratch, PLATFORM_TAR)
    if os.path.exists(platform_tar):
        check_platform_tar(report, platform_tar, platforms, kver)
    squashfs = os.path.join(scratch, SQUASHFS)
    if os.path.exists(squashfs) and platforms:
        check_squashfs(report, squashfs, platforms)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Read-only check of a SONiC ONIE installer; it is never executed.')
    parser.add_argument('installer', help='sonic-<platform>.bin')
    parser.add_argument('--platform', action='append', dest='platforms', metavar='PLATFORM',
                        help='expected platform, e.g. x86_64-cel_silverstone-x-r0 (repeatable; '
                             'default: the release platforms of the installer\'s ASIC)')
    parser.add_argument('--allow-trailing', action='store_true',
                        help='accept bytes after the payload (secure-upgrade signature)')
    parser.add_argument('--strict', action='store_true',
                        help='treat a check skipped for a missing tool as a failure')
    parser.add_argument('--workdir', help='directory for the temporary extraction '
                        '(needs about twice the installer size; default: $TMPDIR)')
    args = parser.parse_args(argv)
    if not os.path.isfile(args.installer):
        parser.error('no such file: %s' % args.installer)
    report = verify(args.installer, args.platforms, args.allow_trailing, args.strict, args.workdir)
    return 1 if report.failed else 0


if __name__ == '__main__':
    sys.exit(main())
