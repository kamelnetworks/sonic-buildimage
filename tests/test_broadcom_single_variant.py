"""The Broadcom installer builds only the plain broadcom variant (#56).

Upstream's sonic-broadcom.bin also assembles the broadcom-dnx and
broadcom-legacy-th variants (one-image.mk DEPENDENT_MACHINE): three root
filesystems, docker sets and ONIE payloads per build, about two thirds of the
34 min installer assembly on CI. The 202611 platforms (Dell S5232F-ON,
Celestica Seastone 2) are plain-variant Trident 3 switches, so the fork builds
only that variant (decision on #56).

Checks, without a build:
  - both release platforms declare platform_asic = broadcom;
  - slave.mk, parsed for PLATFORM=broadcom, defines exactly one RFS target
    and does not pull the dnx / legacy-th syncd dockers or SAI debs into
    the image.

Needs git and make. Run with: python3 -m pytest tests/test_broadcom_single_variant.py
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BLDENV = 'trixie'
RELEASE_PLATFORMS = ('device/dell/x86_64-dellemc_s5232f_c3538-r0',
                     'device/celestica/x86_64-cel_seastone_2-r0')
GIT_ENV = dict(os.environ,
               GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@example.invalid',
               GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@example.invalid')


def run(cmd, cwd, **kw):
    return subprocess.run(cmd, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=GIT_ENV, **kw)


class BroadcomSingleVariantTest(unittest.TestCase):
    VARIABLES = ('sonic-broadcom.bin_MACHINE', 'sonic-broadcom.bin_DEPENDENT_MACHINE',
                 'SONIC_RFS_TARGETS', 'sonic-broadcom.bin_DOCKERS', 'SONIC_INSTALL_DOCKER_IMAGES',
                 'sonic-broadcom.bin_LAZY_BUILD_INSTALLS', 'SONIC_ONLINE_DEBS', 'SONIC_DOCKER_IMAGES')

    @classmethod
    def setUpClass(cls):
        for tool in ('git', 'make'):
            if shutil.which(tool) is None:
                raise unittest.SkipTest(f'{tool} is not installed')
        cls.temp = tempfile.TemporaryDirectory(prefix='broadcom-single-variant-')
        cls.tree = Path(cls.temp.name) / 'tree'
        run(['git', 'worktree', 'add', '--detach', '--quiet', str(cls.tree), 'HEAD'], ROOT)
        diff = run(['git', 'diff', '--binary', 'HEAD'], ROOT).stdout
        if diff.strip():
            run(['git', 'apply', '--allow-empty', '-'], cls.tree, input=diff)
        (cls.tree / 'scripts/prepare_slave_container_buildinfo.sh').write_text('#!/bin/bash\nexit 0\n')
        (cls.tree / '.platform').write_text('broadcom\n')
        (cls.tree / '.arch').write_text('amd64\n')
        for sub in ('debs', 'python-wheels'):
            (cls.tree / 'target' / sub / BLDENV).mkdir(parents=True)
        # A second makefile adds a target that prints the expanded values; the
        # make database (-p) would show recursive variables unexpanded.
        printer = cls.tree / 'print-vars.mk'
        printer.write_text('print-vars:\n' + ''.join(
            f'\t@echo "{v}=$({v})"\n' for v in cls.VARIABLES))
        cmd = ['make', '-f', 'slave.mk', '-f', str(printer), '--no-print-directory', '-s',
               'PLATFORM=broadcom', 'PLATFORM_ARCH=amd64', f'BLDENV={BLDENV}',
               'SLAVE_BASE_TAG=test', 'SONIC_DPKG_CACHE_METHOD=none',
               'SONIC_BUILD_QUIETER=1', 'INSHOOKS=', 'print-vars']
        p = subprocess.run(cmd, cwd=cls.tree, text=True, env=GIT_ENV,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert p.returncode == 0, f'{" ".join(cmd)} failed:\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}'
        cls.values = {}
        for line in p.stdout.splitlines():
            name, sep, value = line.partition('=')
            if sep and name in cls.VARIABLES:
                cls.values[name] = value.split()
        assert set(cls.values) == set(cls.VARIABLES), f'missing: {set(cls.VARIABLES) - set(cls.values)}\n{p.stdout[-2000:]}'

    @classmethod
    def tearDownClass(cls):
        try:
            run(['git', 'worktree', 'remove', '--force', str(cls.tree)], ROOT)
        except subprocess.CalledProcessError:
            shutil.rmtree(cls.tree, ignore_errors=True)
            subprocess.run(['git', 'worktree', 'prune'], cwd=ROOT)
        cls.temp.cleanup()

    def variable(self, name):
        return self.values[name]

    def test_release_platforms_are_plain_broadcom(self):
        for platform in RELEASE_PLATFORMS:
            self.assertEqual((ROOT / platform / 'platform_asic').read_text().strip(), 'broadcom', platform)

    def test_installer_has_no_dependent_machines(self):
        self.assertEqual(self.variable('sonic-broadcom.bin_MACHINE'), ['broadcom'])
        self.assertEqual(self.variable('sonic-broadcom.bin_DEPENDENT_MACHINE'), [])
        self.assertEqual(self.variable('SONIC_RFS_TARGETS'), ['sonic-broadcom.bin__broadcom__rfs.squashfs'])

    def test_variant_only_packages_are_not_in_the_image(self):
        dockers = self.variable('sonic-broadcom.bin_DOCKERS') + self.variable('SONIC_INSTALL_DOCKER_IMAGES')
        self.assertIn('docker-syncd-brcm.gz', dockers)
        self.assertEqual([d for d in dockers if 'dnx' in d or 'legacy-th' in d], [])
        lazy = self.variable('sonic-broadcom.bin_LAZY_BUILD_INSTALLS')
        self.assertTrue(any(d.startswith('opennsl-modules_') for d in lazy), lazy)
        self.assertEqual([d for d in lazy if 'dnx' in d or 'legacy-th' in d], [])
        online = self.variable('SONIC_ONLINE_DEBS')
        self.assertTrue(any(d.startswith('libsaibcm_15.') for d in online), online)
        self.assertEqual([d for d in online if d.startswith('libsaibcm_dnx') or d.startswith('libsaibcm_13.')], [])
        self.assertEqual([d for d in self.variable('SONIC_DOCKER_IMAGES') if 'dnx' in d or 'legacy-th' in d], [])


if __name__ == '__main__':
    unittest.main()
