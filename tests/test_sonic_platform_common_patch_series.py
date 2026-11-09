"""Check the carried sonic-platform-common quilt series against the pin (#22).

The series is applied by slave.mk when the sonic_platform_common wheel is
built. This applies it to the committed pinned tree, without a build, and
runs the patched XcvrApiFactory on the EEPROM of the HUA-SFP-10G-DWDM1A
(identifier 0x0B, DWDM-SFP/SFP+) that made sfputil show no data in #22.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
PATCH_DIR = ROOT / 'src/sonic-platform-common.patch'
SUBMODULE = ROOT / 'src/sonic-platform-common'
PY_COMMON = ROOT / 'src/sonic-py-common'
DWDM_PATCH = '0001-sonic_xcvr-Use-the-SFF-8472-API-for-DWDM-SFP-0x0B.patch'

# HUA-SFP-10G-DWDM1A from #22. Offsets 0x000-0x0FF are A0h and 0x100-0x1FF
# are A2h, as optoe2 exposes them; omitted rows are zero, 0x180-0x1F7 is 0xff.
EEPROM_ROWS = {
    0x000: '0b 04 07 80 00 00 00 00 00 00 00 03 67 00 50 ff',
    0x010: '00 00 00 00 50 72 6f 20 31 30 20 4f 70 74 69 78',
    0x020: '20 20 20 20 00 00 00 00 48 55 41 2d 53 46 50 2d',
    0x030: '31 30 47 2d 44 57 44 4d 31 41 20 20 06 07 49 df',
    0x040: '06 1a 00 00 49 4e 45 42 41 30 30 36 30 30 36 31',
    0x050: '20 20 20 20 31 36 30 36 32 31 20 20 68 f0 05 29',
    0x100: '4e 00 f8 00 4b 00 fb 00 90 88 71 70 8c 70 75 48',
    0x110: 'f4 24 1d 4c ea 60 27 10 7b 87 13 94 62 1f 1f 07',
    0x120: '0c 5a 00 19 07 cb 00 20 ff ff ff ff ff ff ff ff',
    0x130: 'ff ff ff ff ff ff ff ff 00 00 00 00 00 00 00 00',
    0x140: '00 00 00 00 3f 80 00 00 00 00 00 00 01 00 00 00',
    0x150: '01 00 00 00 01 00 00 00 01 00 00 00 00 00 00 b4',
    0x160: '22 83 83 ba a8 b4 37 aa 01 4b 00 00 00 00 30 00',
    0x170: '00 00 00 00 00 00 00 00 00 41 24 00 00 00 00 00',
}

# Runs in a child process so the patched tree, not the pinned one on
# PYTHONPATH, provides sonic_platform_base.
PARSE = textwrap.dedent('''
    import json, sys
    from sonic_platform_base.sonic_xcvr.xcvr_api_factory import XcvrApiFactory
    rows = json.loads(sys.argv[1])
    eeprom = bytearray(0x200)
    eeprom[0x180:0x1f8] = b'\\xff' * (0x1f8 - 0x180)
    for offset, row in rows.items():
        eeprom[int(offset):int(offset) + 16] = bytes.fromhex(row)
    api = XcvrApiFactory(lambda start, length: bytes(eeprom[start:start + length]),
                         lambda *args: True).create_xcvr_api()
    out = {'api': type(api).__name__ if api else None}
    if api:
        out['info'] = api.get_transceiver_info()
        out['dom'] = api.get_transceiver_dom_real_value()
    print(json.dumps(out))
''')


def run(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class SonicPlatformCommonPatchSeriesTest(unittest.TestCase):
    def series(self):
        return [line.split('#', 1)[0].strip()
                for line in (PATCH_DIR / 'series').read_text().splitlines()
                if line.split('#', 1)[0].strip()]

    def test_every_patch_is_listed_once(self):
        series = self.series()
        self.assertIn(DWDM_PATCH, series)
        self.assertEqual(len(series), len(set(series)), 'duplicate patch in series')
        self.assertEqual(sorted(series), sorted(p.name for p in PATCH_DIR.glob('*.patch')))

    def test_patches_are_in_the_cache_key(self):
        # Otherwise a cached wheel built without the series is reused.
        dep = (ROOT / 'rules/sonic-platform-common.dep').read_text()
        self.assertIn('DEP_FILES   += $(shell git ls-files $(SPATH).patch/)', dep)

    def patched_tree(self, temporary):
        self.assertTrue((SUBMODULE / 'setup.py').is_file(),
                        'initialise src/sonic-platform-common before running this test')
        pin = run(['git', 'rev-parse', 'HEAD:src/sonic-platform-common'], ROOT).stdout.strip()
        actual = run(['git', 'rev-parse', 'HEAD'], SUBMODULE).stdout.strip()
        self.assertEqual(actual, pin, 'sonic-platform-common checkout differs from the pin')
        archive = run(['git', 'archive', '--format=tar', 'HEAD'], SUBMODULE).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(temporary, filter='data')
        for name in self.series():
            result = subprocess.run(
                ['git', 'apply', '--whitespace=nowarn', str(PATCH_DIR / name)],
                cwd=temporary, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f'{name}: {result.stderr}')

    def parse(self, tree):
        env = dict(os.environ, PYTHONPATH=f'{tree}:{PY_COMMON}', PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run(
            [sys.executable, '-c', PARSE, json.dumps(EEPROM_ROWS)],
            env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_pin_has_no_api_for_dwdm_sfp(self):
        # Documents the bug: the unpatched factory returns None for 0x0B.
        with tempfile.TemporaryDirectory(prefix='platform-common-pin-') as temporary:
            archive = run(['git', 'archive', '--format=tar', 'HEAD'], SUBMODULE).stdout
            with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                source.extractall(temporary, filter='data')
            self.assertIsNone(self.parse(temporary)['api'])

    def test_patched_factory_decodes_dwdm_sfp(self):
        with tempfile.TemporaryDirectory(prefix='platform-common-quilt-') as temporary:
            self.patched_tree(temporary)
            out = self.parse(temporary)
        self.assertEqual(out['api'], 'Sff8472Api')
        info, dom = out['info'], out['dom']
        self.assertEqual(info['type'], 'DWDM-SFP/SFP+')
        self.assertEqual(info['model'], 'HUA-SFP-10G-DWDM')
        self.assertEqual(info['serial'].strip(), 'INEBA0060061')
        self.assertAlmostEqual(dom['temperature'], 34.512, places=3)
        self.assertAlmostEqual(dom['voltage'], 3.372, places=3)
        self.assertAlmostEqual(dom['tx1power'], 1.538, places=3)
        self.assertAlmostEqual(dom['rx1power'], -14.815, places=3)


if __name__ == '__main__':
    unittest.main()
