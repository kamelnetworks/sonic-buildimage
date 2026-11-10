"""Check the SONIX HWSKUs that the image ships for the release platforms (#133).

A SONIX switch keeps DEVICE_METADATA.hwsku=SONIX across `sonic-installer`
upgrades, so the HWSKU directory must be in the image, not only installed by
sonix-ansible. Its port layout is the platform default (hwsku.json against
platform.json); sites set theirs with Dynamic Port Breakout (BREAKOUT_CFG).
Most files are symlinks to the stock HWSKU, which the sonic-device-data build
dereferences (`cp -r -L`). Stdlib only, so it runs in the fork unit-test
workflow; the sonic-cfggen rendering is checked in the slave image instead.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEVICE_DATA_TESTS = ROOT / 'src/sonic-device-data/tests'

# platform dir -> the stock HWSKU whose files SONIX links to
PLATFORMS = {
    'device/celestica/x86_64-cel_silverstone-x-r0': 'Silverstone-x',
    'device/dell/x86_64-dellemc_s5232f_c3538-r0': 'DellEMC-S5232f-C32',
    'device/celestica/x86_64-cel_seastone_2-r0': 'Seastone_2',
}

# Paths in sai.profile / ivm.sai.config.yaml that syncd opens from the HWSKU
HWSKU_PATH = re.compile(r'/usr/share/sonic/hwsku/([^"\s]+)')


def platforms():
    for rel, stock in PLATFORMS.items():
        platform = ROOT / rel
        yield platform, platform / 'SONIX', platform / stock


class SonixHwskuTest(unittest.TestCase):
    def test_symlinks_resolve_to_the_stock_hwsku(self):
        for platform, sonix, stock in platforms():
            self.assertTrue(sonix.is_dir(), f'{sonix} is missing')
            for f in sorted(sonix.iterdir()):
                if not f.is_symlink():
                    continue
                with self.subTest(file=str(f.relative_to(ROOT))):
                    self.assertEqual(os.readlink(f), f'../{stock.name}/{f.name}',
                                     'should link to the stock file of the same name')
                    self.assertTrue(f.resolve().is_file(), 'dangling symlink')

    def test_stock_default_sku_is_unchanged(self):
        for platform, sonix, stock in platforms():
            with self.subTest(platform=platform.name):
                self.assertEqual((platform / 'default_sku').read_text().split()[0], stock.name)

    def test_syncd_files_are_present(self):
        for platform, sonix, stock in platforms():
            with self.subTest(platform=platform.name):
                text = (sonix / 'sai.profile').read_text()
                if 'ivm.sai.config.yaml' in text:
                    text += (sonix / 'ivm.sai.config.yaml').read_text()
                names = HWSKU_PATH.findall(text)
                self.assertTrue(names, 'sai.profile names no HWSKU file')
                for name in names:
                    self.assertTrue((sonix / name).is_file(), f'SONIX lacks {name}')

    def test_hwsku_json_matches_platform_json(self):
        for platform, sonix, stock in platforms():
            with self.subTest(platform=platform.name):
                hwsku = json.loads((sonix / 'hwsku.json').read_text())['interfaces']
                platform_json = platform / 'platform.json'
                if not platform_json.is_file():
                    # Silverstone-X until #165: SONiC then reads port_config.ini.
                    self.assertTrue((sonix / 'port_config.ini').is_file(),
                                    'neither platform.json nor port_config.ini')
                    continue
                ports = json.loads(platform_json.read_text())['interfaces']
                self.assertEqual(sorted(hwsku), sorted(ports),
                                 'hwsku.json must list every platform.json port and no other')
                for name, attrs in hwsku.items():
                    self.assertIn(attrs['default_brkout_mode'], ports[name]['breakout_modes'],
                                  f'{name}: mode not offered by platform.json')

    def test_sonic_device_data_checkers(self):
        # sonic-device-data's `make test` runs these in the image build.
        checks = [('hwsku_json_checker', 'hwsku.json'), ('config_checker', '*.config.bcm')]
        for platform, sonix, stock in platforms():
            for checker, pattern in checks:
                for f in sorted(sonix.glob(pattern)):
                    with self.subTest(checker=checker, file=str(f.relative_to(ROOT))):
                        result = subprocess.run([sys.executable, checker, str(f)],
                                                cwd=DEVICE_DATA_TESTS, capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
