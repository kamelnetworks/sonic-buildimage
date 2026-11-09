"""Tests for scripts/verify_installer.py on small synthetic installers.

Each installer is assembled like onie-mk-demo.sh does it: the real
installer/sharch_body.sh with its placeholders substituted, then the payload
tar of installer/, with fs.zip built like build_debian.sh builds it.
"""
import gzip
import hashlib
import importlib.util
import io
import lzma
import os
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location('verify_installer', ROOT / 'scripts' / 'verify_installer.py')
vi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vi)

KVER = '6.12.41+deb13-sonic-amd64'
SSX = 'x86_64-cel_silverstone-x-r0'
S5232F = 'x86_64-dellemc_s5232f_c3538-r0'
SEASTONE2 = 'x86_64-cel_seastone_2-r0'


class Link:
    def __init__(self, target):
        self.target = target


def tar_bytes(files, compress=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:' + (compress or '')) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            if isinstance(data, Link):
                info.type, info.linkname = tarfile.SYMTYPE, data.target
                tar.addfile(info)
                continue
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def ar_bytes(members):
    out = b'!<arch>\n'
    for name, data in members.items():
        out += ('%-16s%-12s%-6s%-6s%-8s%-10s`\n' % (name, 0, 0, 0, 100644, len(data))).encode()
        out += data + (b'\n' if len(data) & 1 else b'')
    return out


def ko(kver):
    return b'\x7fELF...\x00license=GPL\x00vermagic=%s SMP preempt mod_unload modversions \x00' % kver.encode()


def deb(modules):
    data = tar_bytes({'./lib/modules/%s/extra/%s' % (KVER, n): b for n, b in modules.items()}
                     or {'./usr/share/doc/x/README': b'x'})
    return ar_bytes({'debian-binary': b'2.0\n',
                     'control.tar.xz': lzma.compress(tar_bytes({'./control': b'Package: x\n'})),
                     'data.tar.xz': lzma.compress(data)})


def build(path, machine='marvell-teralynx', platforms_asic=(SSX,), debs=None,
          kver=KVER, dockerfs_in_zip=True, trailing=b'', zip_hook=None, dockerfs=None,
          link_debs=True, extra_platform_files=None):
    if debs is None:
        debs = {SSX: {'platform-modules-silverstone-x_0.2.2_amd64.deb':
                      deb({'optoe.ko': ko(KVER), 'fpga.ko': ko(KVER)})}}
    # Like sonic_debian_extension.sh: packages in ./common/, linked from ./<platform>/.
    pfiles = {'./firsttime': b'', './common/Packages.gz': gzip.compress(b'')}
    for p, pkgs in debs.items():
        for n, b in pkgs.items():
            if link_debs:
                pfiles['./common/' + n] = b
                pfiles['./%s/%s' % (p, n)] = Link('../common/' + n)
            else:
                pfiles['./%s/%s' % (p, n)] = b
    pfiles.update(extra_platform_files or {})
    platform_tar = tar_bytes(pfiles, 'gz')
    if dockerfs is None:
        dockerfs = tar_bytes({'./overlay2/l/abc': b'layer', './image/repositories.json': b'{}'}, 'gz')
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, 'w') as zf:
        zf.writestr('boot/vmlinuz-%s' % kver, b'kernel')
        zf.writestr('boot/initrd.img-%s' % kver, b'initrd')
        zf.writestr('platform.tar.gz', platform_tar)
        zf.writestr('fs.squashfs', b'hsqs' + b'\0' * 64)
        if dockerfs_in_zip:
            zf.writestr('dockerfs.tar.gz', dockerfs)
    fs_zip = zbuf.getvalue()
    if zip_hook:
        fs_zip = zip_hook(fs_zip)
    files = {'installer/install.sh': b'#!/bin/sh\n',
             'installer/machine.conf': ('machine=%s\nplatform=x\n' % machine).encode(),
             'installer/platforms_asic': ('\n'.join(platforms_asic) + '\n').encode(),
             'installer/onie-image.conf': b'', 'installer/platform.conf': b'',
             'installer/default_platform.conf': b'', 'installer/fs.zip': fs_zip}
    if not dockerfs_in_zip:
        files['installer/dockerfs.tar.gz'] = dockerfs
    payload = tar_bytes(files)
    header = (ROOT / 'installer' / 'sharch_body.sh').read_text()
    header = header.replace('%%IMAGE_SHA1%%', hashlib.sha1(payload).hexdigest())
    header = header.replace('%%PAYLOAD_IMAGE_SIZE%%', str(len(payload)))
    Path(path).write_bytes(header.encode() + payload + trailing)
    return len(header.encode())


class VerifyInstallerTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, 'sonic-test.bin')
        # Keep the result independent of the host's tools: no unsquashfs
        # (the squashfs here is a stub), zstd as found.
        real_which = shutil.which
        patcher = mock.patch.object(vi.shutil, 'which',
                                    side_effect=lambda t: None if t == 'unsquashfs' else real_which(t))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp)

    def run_verify(self, **kwargs):
        out = io.StringIO()
        report = vi.verify(self.bin, out=out, workdir=self.tmp, **kwargs)
        return report, out.getvalue()

    def assertPasses(self, **kwargs):
        report, out = self.run_verify(**kwargs)
        self.assertEqual(report.failed, 0, out)
        return out

    def assertFails(self, needle, **kwargs):
        report, out = self.run_verify(**kwargs)
        self.assertGreater(report.failed, 0, out)
        self.assertIn(needle, [l for l in out.splitlines() if l.startswith('FAIL')].__str__(), out)
        return out

    def test_good_marvell_installer(self):
        build(self.bin)
        out = self.assertPasses()
        self.assertIn('expected platforms for marvell-teralynx: %s' % SSX, out)
        self.assertIn('2 kernel module(s), all with vermagic %s' % KVER, out)
        self.assertIn('SKIP  fs.squashfs: device directories not checked', out)

    def test_good_broadcom_installer_uses_broadcom_release_platforms(self):
        build(self.bin, machine='broadcom', platforms_asic=(S5232F, SEASTONE2),
              debs={S5232F: {'platform-modules-s5232f_1.1_amd64.deb': deb({'dell_s5232f_fpga_ocores.ko': ko(KVER)})},
                    SEASTONE2: {'platform-modules-seastone2_0.1_amd64.deb': deb({})}})
        out = self.assertPasses()
        self.assertIn('expected platforms for broadcom: %s, %s' % (S5232F, SEASTONE2), out)
        self.assertIn('no kernel modules', out)

    def test_cli_exit_status(self):
        build(self.bin)
        with mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(vi.main([self.bin, '--workdir', self.tmp]), 0)
            build(self.bin, kver='6.1.0-sonic-amd64')
            self.assertEqual(vi.main([self.bin, '--workdir', self.tmp]), 1)

    def test_strict_turns_skip_into_failure(self):
        build(self.bin)
        self.assertFails('device directories not checked', strict=True)

    def test_corrupted_payload_fails_sha1(self):
        start = build(self.bin)
        data = bytearray(Path(self.bin).read_bytes())
        data[start + 600] ^= 0xff
        Path(self.bin).write_bytes(bytes(data))
        self.assertFails('payload SHA-1')

    def test_truncated_installer(self):
        build(self.bin)
        data = Path(self.bin).read_bytes()
        Path(self.bin).write_bytes(data[:-100])
        self.assertFails('payload is complete')

    def test_trailing_bytes_need_allow_trailing(self):
        build(self.bin, trailing=b'0\x82CMS-signature')
        self.assertFails('trailing bytes')
        out = self.assertPasses(allow_trailing=True)
        self.assertIn('trailing bytes after the payload (signature, --allow-trailing)', out)

    def test_bad_zip_crc(self):
        def flip(fs_zip):
            idx = fs_zip.find(b'initrd', fs_zip.find(b'boot/initrd.img') + 20)
            return fs_zip[:idx] + b'INITRD' + fs_zip[idx + 6:]
        build(self.bin, zip_hook=flip)
        self.assertFails('CRCs')

    def test_module_with_other_kernel_vermagic(self):
        build(self.bin, debs={SSX: {'platform-modules-silverstone-x_0.2.2_amd64.deb':
                                    deb({'optoe.ko': ko(KVER), 'stale.ko': ko('6.1.0-29-2-amd64')})}})
        self.assertFails('stale.ko has vermagic')

    def test_platform_package_as_regular_file(self):
        build(self.bin, link_debs=False)
        self.assertPasses()

    def test_dangling_platform_package_link(self):
        build(self.bin, debs={}, extra_platform_files={
            './%s/platform-modules-silverstone-x_0.2.2_amd64.deb' % SSX: Link('../common/gone.deb')})
        self.assertFails('link to common/gone.deb resolves')

    def test_missing_platform_package(self):
        build(self.bin, debs={})
        self.assertFails('%s has a platform package' % SSX)

    def test_platform_not_in_platforms_asic(self):
        build(self.bin, platforms_asic=('x86_64-other-r0',))
        self.assertFails('platforms_asic lists %s' % SSX)

    def test_explicit_platform_overrides_release_list(self):
        build(self.bin, platforms_asic=(SSX, 'x86_64-other-r0'))
        self.assertFails('platforms_asic lists x86_64-cel_ds4000-r0', platforms=[SSX, 'x86_64-cel_ds4000-r0'])
        out = self.assertPasses(platforms=[])
        self.assertNotIn('platform package', out)

    def test_dockerfs_next_to_fs_zip(self):
        build(self.bin, dockerfs_in_zip=False)
        out = self.assertPasses()
        self.assertIn('dockerfs.tar.gz next to fs.zip', out)

    def test_truncated_dockerfs(self):
        good = tar_bytes({'./overlay2/l/abc': os.urandom(4096)}, 'gz')
        build(self.bin, dockerfs=good[:len(good) // 2])
        self.assertFails('dockerfs.tar.gz: decompresses and untars')

    def test_two_kernels(self):
        def add_kernel(fs_zip):
            buf = io.BytesIO(fs_zip)
            with zipfile.ZipFile(buf, 'a') as zf:
                zf.writestr('boot/vmlinuz-6.1.0-sonic-amd64', b'old kernel')
            return buf.getvalue()
        build(self.bin, zip_hook=add_kernel)
        self.assertFails('one kernel in boot/')

    def test_not_an_installer(self):
        Path(self.bin).write_bytes(b'#!/bin/sh\necho hi\n')
        self.assertFails('exit_marker line found')


if __name__ == '__main__':
    unittest.main()


class CompressionFooterTest(unittest.TestCase):
    def test_corrupt_footers(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w') as archive:
            info = tarfile.TarInfo('file')
            info.size = 1
            archive.addfile(info, io.BytesIO(b'x'))
        good = gzip.compress(raw.getvalue() + bytes(100000))
        cases = [good[:-8], good[:-8] + bytes([good[-8] ^ 1]) + good[-7:]]
        for data in cases:
            with self.subTest(data=data[-8:]), tempfile.NamedTemporaryFile() as f:
                f.write(data)
                f.flush()
                count, error, skip = vi.scan_tar(f.name)
                self.assertEqual(count, 1)
                self.assertIsNotNone(error)
                self.assertIsNone(skip)

    def test_zstd_nonzero_after_valid_tar(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w') as archive:
            archive.addfile(tarfile.TarInfo('empty'))
        proc = mock.Mock()
        proc.stdout = io.BytesIO(raw.getvalue())
        proc.wait.return_value = 1
        proc.poll.return_value = 1
        with tempfile.NamedTemporaryFile() as f:
            f.write(vi.ZSTD_MAGIC)
            f.flush()
            with mock.patch.object(vi.shutil, 'which', return_value='/usr/bin/zstd'), \
                 mock.patch.object(vi.subprocess, 'Popen', return_value=proc):
                count, error, skip = vi.scan_tar(f.name)
        self.assertEqual(count, 1)
        self.assertIn('zstd decompression failed', error)
        self.assertIsNone(skip)
        proc.wait.assert_called()
