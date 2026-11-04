"""Every package built from source must take part in the DPKG cache (#56).

Makefile.cache prints "[ DPKG ] Cache is not enabled for <pkg> package" at
parse time for every target whose <pkg>_CACHE_MODE is empty or none, i.e. for
which no .dep file sets GIT_CONTENT_SHA. Such a package is rebuilt on every
CI run even on a warm cache. On the 2026-11-04 rc1 run these were, per job:
sonic-device-data (5.5-6.6 min on every platform), sonic-platform-nokia-chassis
(11.6 min), sonic-platform-arista-all (7.3 min), sonic-platform-nexthop-common,
nexthop_utils, iproute2, sonic-platform-vs and two gearbox/syncd dockers.

This test parses slave.mk for each release platform, without a build, in a
scratch worktree of this checkout (plus its uncommitted changes) and checks
that only packages that are downloaded or copied rather than built (the vendor
SAI debs, sonic_version.yml) are left out of the cache.

Needs git and make. Run with: python3 -m pytest tests/test_dpkg_cache_coverage.py
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
PLATFORMS = ('broadcom', 'marvell-teralynx', 'vs')
# Not built from source, so there is nothing to cache.
ALLOWED = (
    re.compile(r'^libsai'),                 # vendor SAI debs (SONIC_ONLINE_DEBS / SONIC_COPY_DEBS)
    re.compile(r'^sonic_version\.yml$'),    # rendered per build (BUILD_TIMESTAMP)
)
GIT_ENV = dict(os.environ,
               GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@example.invalid',
               GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@example.invalid')


def run(cmd, cwd, **kw):
    return subprocess.run(cmd, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=GIT_ENV, **kw)


class DpkgCacheCoverageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ('git', 'make'):
            if shutil.which(tool) is None:
                raise unittest.SkipTest(f'{tool} is not installed')
        cls.temp = tempfile.TemporaryDirectory(prefix='dpkg-cache-coverage-')
        cls.tree = Path(cls.temp.name) / 'tree'
        run(['git', 'worktree', 'add', '--detach', '--quiet', str(cls.tree), 'HEAD'], ROOT)
        diff = run(['git', 'diff', '--binary', 'HEAD'], ROOT).stdout
        if diff.strip():
            run(['git', 'apply', '--allow-empty', '-'], cls.tree, input=diff)
        # slave.mk prepares the slave container's buildinfo at parse time;
        # this is not a slave container.
        (cls.tree / 'scripts/prepare_slave_container_buildinfo.sh').write_text('#!/bin/bash\nexit 0\n')
        (cls.tree / '.arch').write_text('amd64\n')

    @classmethod
    def tearDownClass(cls):
        try:
            run(['git', 'worktree', 'remove', '--force', str(cls.tree)], ROOT)
        except subprocess.CalledProcessError:
            shutil.rmtree(cls.tree, ignore_errors=True)
            subprocess.run(['git', 'worktree', 'prune'], cwd=ROOT)
        cls.temp.cleanup()

    def uncached(self, platform):
        """Packages slave.mk reports as not cache-enabled when configured for platform."""
        (self.tree / '.platform').write_text(platform + '\n')
        for sub in ('debs', 'python-wheels'):
            (self.tree / 'target' / sub / BLDENV).mkdir(parents=True, exist_ok=True)
        cmd = ['make', '-f', 'slave.mk', '--no-print-directory', '-n',
               f'PLATFORM={platform}', 'PLATFORM_ARCH=amd64', f'BLDENV={BLDENV}',
               'SLAVE_BASE_TAG=test', 'SONIC_DPKG_CACHE_METHOD=rwcache',
               'SONIC_BUILD_QUIETER=1', 'INSHOOKS=', '.platform']
        p = subprocess.run(cmd, cwd=self.tree, text=True, env=GIT_ENV,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, f'{" ".join(cmd)} failed:\n{p.stdout}\n{p.stderr}')
        return sorted(set(re.findall(r'Cache is not enabled for (\S+) package', p.stdout + p.stderr)))

    def check(self, platform, expect=()):
        uncached = self.uncached(platform)
        # The packages that can never be cached prove that the parse-time
        # report is working (an empty list would otherwise pass trivially).
        for pkg in expect:
            self.assertTrue(any(re.match(pkg, u) for u in uncached),
                            f'{platform}: expected {pkg!r} in the uncached report, got {uncached}')
        offenders = [pkg for pkg in uncached if not any(a.match(pkg) for a in ALLOWED)]
        self.assertEqual(offenders, [], f'{platform}: packages built from source but not cached: {offenders}')

    def test_broadcom(self):
        self.check('broadcom', expect=(r'^libsaibcm_',))

    def test_marvell_teralynx(self):
        # Nothing is left out here; the mechanism is proven by the other two.
        self.check('marvell-teralynx')

    def test_vs(self):
        self.check('vs', expect=(r'^sonic_version\.yml$',))


if __name__ == '__main__':
    unittest.main()
