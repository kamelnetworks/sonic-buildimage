"""Apply the complete sairedis quilt to its pin and execute the changed methods.

The small C++ harness uses the real method bodies with dependency fakes. It
checks statuses, successful RID/VID mappings, discovery and Redis calls without
building syncd or requiring a vendor SAI. It is not a full syncd/SAI integration
test; the image and refused-breakout hardware gates remain required.
"""
from pathlib import Path
import re
import resource
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH_DIR = ROOT / 'src/sonic-sairedis.patch'
SUBMODULE = ROOT / 'src/sonic-sairedis'


def run(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def method(text, signature):
    start = text.index(signature)
    opening = text.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


class SairedisPatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='sairedis-quilt-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        pin = run(['git', 'rev-parse', 'HEAD:src/sonic-sairedis'], ROOT).stdout.strip()
        actual = run(['git', 'rev-parse', 'HEAD'], SUBMODULE).stdout.strip()
        if actual != pin:
            raise AssertionError('sonic-sairedis checkout differs from the gitlink pin')
        cls.series = [line.split('#', 1)[0].strip() for line in
                      (PATCH_DIR / 'series').read_text().splitlines()
                      if line.split('#', 1)[0].strip()]
        for filename in ('Syncd.cpp', 'RedisClient.cpp'):
            target = cls.root / 'syncd' / filename
            target.parent.mkdir(exist_ok=True)
            target.write_text(run(['git', 'show', 'HEAD:syncd/' + filename], SUBMODULE).stdout)
        for name in cls.series:
            run(['git', 'apply', '--whitespace=error', str(PATCH_DIR / name)], cls.root)
        cls.methods = '\n'.join((
            method((cls.root / 'syncd/Syncd.cpp').read_text(), 'sai_status_t Syncd::processBulkOidCreate('),
            method((cls.root / 'syncd/RedisClient.cpp').read_text(), 'void RedisClient::getVidsForRids(')))

    def test_series_is_complete_and_unique(self):
        self.assertTrue(self.series)
        self.assertEqual(len(self.series), len(set(self.series)))
        self.assertEqual(sorted(self.series), sorted(p.name for p in PATCH_DIR.glob('*.patch')))

    def execute(self, methods):
        (self.root / 'methods.inc').write_text(methods)
        run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
             '-I', str(self.root), str(ROOT / 'tests/sairedis_bulk_create_harness.cpp'),
             '-o', str(self.root / 'regression')], self.root)
        def no_core():
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        return subprocess.run([str(self.root / 'regression')], cwd=self.root,
                              capture_output=True, text=True, timeout=10, preexec_fn=no_core)

    def test_actual_patched_methods(self):
        result = self.execute(self.methods)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_discovery_guard_reproduces_failure(self):
        broken = self.methods.replace(' && !createdRids.empty()', '')
        self.assertNotEqual(broken, self.methods)
        self.assertNotEqual(self.execute(broken).returncode, 0)

    def test_missing_empty_lookup_guard_reproduces_failure(self):
        broken, count = re.subn(r'if \(count == 0\)\s*\{[^{}]*\}\s*', '', self.methods)
        self.assertEqual(count, 1)
        self.assertNotEqual(self.execute(broken).returncode, 0)


if __name__ == '__main__':
    unittest.main()
