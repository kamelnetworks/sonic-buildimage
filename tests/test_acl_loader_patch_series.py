"""Ensure the carried ACL fix stays in a complete, applicable quilt series.

The separate NTP PR also edits this series. A merge must preserve every patch
file and list each one exactly once. No sonic-utilities runtime dependencies or
build are needed here; its ACL behavior tests run in the slave environment.
"""
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PATCH_DIR = ROOT / 'src/sonic-utilities.patch'
SUBMODULE = ROOT / 'src/sonic-utilities'
ACL_PATCH = '0002-acl-loader-Convert-openconfig-source-mac-destination.patch'


def run(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class AclLoaderPatchSeriesTest(unittest.TestCase):
    def series(self):
        # The series currently uses plain filenames, without quilt options.
        return [line.split('#', 1)[0].strip()
                for line in (PATCH_DIR / 'series').read_text().splitlines()
                if line.split('#', 1)[0].strip()]

    def test_every_patch_is_listed_once(self):
        series = self.series()
        self.assertIn(ACL_PATCH, series, 'the ACL MAC fix was dropped from series')
        self.assertEqual(len(series), len(set(series)), 'duplicate patch in series')
        self.assertEqual(sorted(series), sorted(p.name for p in PATCH_DIR.glob('*.patch')),
                         'series must include every carried patch, including after a merge')

    def test_whole_series_applies_in_order_to_the_pin(self):
        self.assertTrue((SUBMODULE / 'setup.py').is_file(),
                        'initialise src/sonic-utilities before running this test')
        pin = run(['git', 'rev-parse', 'HEAD:src/sonic-utilities'], ROOT).stdout.strip()
        actual = run(['git', 'rev-parse', 'HEAD'], SUBMODULE).stdout.strip()
        self.assertEqual(actual, pin, 'sonic-utilities checkout differs from the pin')
        # Export the committed tree, not local work or an already-patched copy.
        # A full archive includes original files while permitting patches to add
        # new fixtures; checking each patch separately would miss dependencies.
        archive = run(['git', 'archive', '--format=tar', 'HEAD'], SUBMODULE).stdout
        with tempfile.TemporaryDirectory(prefix='acl-loader-quilt-') as temporary:
            with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                source.extractall(temporary, filter='data')
            for name in self.series():
                result = subprocess.run(
                    ['git', 'apply', '--whitespace=nowarn', str(PATCH_DIR / name)],
                    cwd=temporary, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, f'{name}: {result.stderr}')
            self.assertTrue((Path(temporary) / 'tests/acl_input/acl_l2.json').is_file())


if __name__ == '__main__':
    unittest.main()
