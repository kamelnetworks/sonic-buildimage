"""The DPKG cache key must change when a package's sources change (#138).

Makefile.cache names each cache entry after a content hash of the package's
DEP_FILES (and SMDEP_FILES for submodule packages), computed by the
target/.../<pkg>.dep and .smdep rules into <pkg>.dep.sha / <pkg>.smdep.smsha.
Commit e58716f15 wrote the shell substitution in those recipes as $$(...)
inside $(eval $(call ...)); make consumed it as an empty variable, every hash
became the empty-blob hash, and rc1 (CI run 41798236605) shipped a
platform-modules-silverstone-x built from Oct 31 sources although #111 had
changed them.

This test drives the real slave.mk/Makefile.cache rules, without a build, in
a scratch worktree of this checkout (plus its uncommitted changes), and
checks for the Celestica Silverstone-X platform modules that
  - <pkg>.dep.sha is the git content hash of the listed files;
  - it changes when a file under sonic-platform-modules-cel changes;
  - it changes when a file under device/.../sonic_platform changes, which
    silverstone-x/setup.py packages into the deb;
and, with a synthetic src/sonic-host-services repository standing in for the
submodule, the same for the .smdep.smsha of a submodule package.

Needs git and make. Run with: python3 -m pytest tests/test_dpkg_cache_key.py
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EMPTY_BLOB = 'e69de29bb2d1d6434b8b29ae775ad8c2e48c5391'
PLATFORM = 'marvell-teralynx'
BLDENV = 'trixie'
PKG = 'platform-modules-midstone-200i_0.2.2_amd64.deb'      # CEL_MIDSTONE_200I_PLATFORM_MODULE
WHEEL = 'sonic_host_services-1.0-py3-none-any.whl'          # SONIC_HOST_SERVICES_PY3
PLATFORM_FILE = 'platform/marvell-teralynx/sonic-platform-modules-cel/silverstone-x/modules/Makefile'
DEVICE_FILE = 'device/celestica/x86_64-cel_silverstone-x-r0/sonic_platform/sfp.py'
SUBMODULE = 'src/sonic-host-services'
GIT_ENV = dict(os.environ,
               GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@example.invalid',
               GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@example.invalid')


def run(cmd, cwd, **kw):
    return subprocess.run(cmd, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=GIT_ENV, **kw)


def content_hashes(tree, listing):
    """git hash-object of every listed file that exists, in list order."""
    files = [f for f in listing.split() if (tree / f).is_file()]
    return run(['git', 'hash-object', '--'] + files, tree).stdout


class DpkgCacheKeyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ('git', 'make'):
            if shutil.which(tool) is None:
                raise unittest.SkipTest(f'{tool} is not installed')
        cls.temp = tempfile.TemporaryDirectory(prefix='dpkg-cache-key-')
        cls.tree = Path(cls.temp.name) / 'tree'
        run(['git', 'worktree', 'add', '--detach', '--quiet', str(cls.tree), 'HEAD'], ROOT)
        # The checkout's uncommitted changes are part of what is tested.
        diff = run(['git', 'diff', '--binary', 'HEAD'], ROOT).stdout
        if diff.strip():
            run(['git', 'apply', '--allow-empty', '-'], cls.tree, input=diff)
        # slave.mk prepares the slave container's buildinfo at parse time;
        # this is not a slave container.
        (cls.tree / 'scripts/prepare_slave_container_buildinfo.sh').write_text('#!/bin/bash\nexit 0\n')
        (cls.tree / '.platform').write_text(PLATFORM + '\n')
        (cls.tree / '.arch').write_text('amd64\n')
        cls.debs = cls.tree / 'target/debs' / BLDENV
        cls.wheels = cls.tree / 'target/python-wheels' / BLDENV
        cls.debs.mkdir(parents=True)
        cls.wheels.mkdir(parents=True)
        # A small repository stands in for the pinned submodule: the rules
        # only need `git ls-files` and `git hash-object` to work inside it.
        sub = cls.tree / SUBMODULE
        sub.mkdir(parents=True, exist_ok=True)
        (sub / 'setup.py').write_text('# synthetic submodule\n')
        (sub / 'scripts').mkdir()
        (sub / 'scripts/caclmgrd').write_text('#!/usr/bin/env python3\n')
        run(['git', 'init', '--quiet'], sub)
        run(['git', 'add', '-A'], sub)
        run(['git', 'commit', '--quiet', '-m', 'synthetic submodule'], sub)

    @classmethod
    def tearDownClass(cls):
        try:
            run(['git', 'worktree', 'remove', '--force', str(cls.tree)], ROOT)
        except subprocess.CalledProcessError:
            shutil.rmtree(cls.tree, ignore_errors=True)
            subprocess.run(['git', 'worktree', 'prune'], cwd=ROOT)
        cls.temp.cleanup()

    def make(self, *targets):
        cmd = ['make', '-f', 'slave.mk', '--no-print-directory',
               f'PLATFORM={PLATFORM}', 'PLATFORM_ARCH=amd64', f'BLDENV={BLDENV}',
               'SLAVE_BASE_TAG=test', 'SONIC_DPKG_CACHE_METHOD=rwcache',
               'SONIC_BUILD_QUIETER=1', 'INSHOOKS=', *targets]
        p = subprocess.run(cmd, cwd=self.tree, text=True, env=GIT_ENV,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, f'{" ".join(cmd)} failed:\n{p.stdout}\n{p.stderr}')

    def dep_hash(self):
        """Recompute <pkg>.dep.sha the way a fresh build does."""
        for suffix in ('.dep', '.dep.sha'):
            (self.debs / (PKG + suffix)).unlink(missing_ok=True)
        self.make(f'target/debs/{BLDENV}/{PKG}.dep')
        return (self.debs / (PKG + '.dep')).read_text(), (self.debs / (PKG + '.dep.sha')).read_text()

    def smdep_hash(self):
        for suffix in ('.smdep', '.smdep.smsha', '.dep', '.dep.sha'):
            (self.wheels / (WHEEL + suffix)).unlink(missing_ok=True)
        self.make(f'target/python-wheels/{BLDENV}/{WHEEL}.dep')
        return ((self.wheels / (WHEEL + '.smdep')).read_text(),
                (self.wheels / (WHEEL + '.smdep.smsha')).read_text())

    def touch(self, relpath):
        with open(self.tree / relpath, 'a') as f:
            f.write('\n# changed by test_dpkg_cache_key\n')

    def test_dep_sha_is_the_content_hash_of_dep_files(self):
        listing, sha = self.dep_hash()
        self.assertIn(PLATFORM_FILE, listing.split())
        self.assertIn(DEVICE_FILE, listing.split(), 'device-side sonic_platform is not in DEP_FILES')
        self.assertNotEqual(sha.strip(), EMPTY_BLOB, 'dep hash does not depend on the sources')
        self.assertEqual(sha, content_hashes(self.tree, listing))

    def test_platform_module_change_invalidates_key(self):
        _, before = self.dep_hash()
        self.touch(PLATFORM_FILE)
        _, after = self.dep_hash()
        self.assertNotEqual(before, after, f'changing {PLATFORM_FILE} left the dep hash unchanged')

    def test_device_sonic_platform_change_invalidates_key(self):
        _, before = self.dep_hash()
        self.touch(DEVICE_FILE)
        _, after = self.dep_hash()
        self.assertNotEqual(before, after, f'changing {DEVICE_FILE} left the dep hash unchanged')

    def test_submodule_change_invalidates_key(self):
        listing, before = self.smdep_hash()
        self.assertEqual(listing.split(), ['scripts/caclmgrd', 'setup.py'])
        self.assertNotEqual(before.strip(), EMPTY_BLOB, 'submodule hash does not depend on the sources')
        self.assertEqual(before, content_hashes(self.tree / SUBMODULE, listing))
        self.touch(f'{SUBMODULE}/scripts/caclmgrd')
        _, after = self.smdep_hash()
        self.assertNotEqual(before, after, 'changing a submodule file left the smdep hash unchanged')


if __name__ == '__main__':
    unittest.main()
