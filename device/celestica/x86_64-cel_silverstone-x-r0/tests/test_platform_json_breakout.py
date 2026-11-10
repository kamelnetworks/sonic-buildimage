"""Dynamic Port Breakout files for the Silverstone-X (kamelnetworks/sonic-buildimage#103).

`config interface breakout` needs `platform.json` in the platform directory
and `hwsku.json` in the HWSKU directory. sonic-config-engine's portconfig.py
then derives the child ports of a breakout mode from the parent's lane list:
child `n` is `Ethernet<base + n * lanes_per_child>` and gets the next alias of
the mode. Integration checks call real portconfig and sonic-cfggen with SONiC's
compiled dependencies, and pin the files to layouts already run on Teralynx 7
hardware: the stock 32x400G `Silverstone-x` HWSKU and the static SONIX
breakouts (4x100G on QSFP25/26, 4x10G on QSFP3 of ixp-lab-sw1).
"""
import json
import copy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLATFORM_DIR = Path(__file__).resolve().parents[1]
PLATFORM_JSON = PLATFORM_DIR / 'platform.json'
HWSKU_DIR = PLATFORM_DIR / 'Silverstone-x'
HWSKU_JSON = HWSKU_DIR / 'hwsku.json'
PORT_CONFIG_INI = HWSKU_DIR / 'port_config.ini'

REPO_ROOT = PLATFORM_DIR.parents[2]
CONFIG_ENGINE = REPO_ROOT / 'src' / 'sonic-config-engine'
DEVICE_DATA_TESTS = REPO_ROOT / 'src' / 'sonic-device-data' / 'tests'
sys.path.insert(0, str(CONFIG_ENGINE))
try:
    import portconfig
except ImportError:
    portconfig = None

SONIC_DEPS = 'requires real SONiC swsscommon and sonic_py_common packages'

# The modes every QSFP-DD cage offers, with the lane count per child port.
# All of them run in production on SONIX Silverstone-X switches (sonix-ansible
# `.agents/skills/silverstone-x-breakout`), except 4x25G which upstream lists
# for the Teralynx 7 Wistron 6512-32R.
QSFPDD_MODES = {
    '1x400G[200G]': (1, 8),
    '2x200G': (2, 4),
    '4x100G': (4, 2),
    '1x100G[40G](4)': (1, 4),
    '4x25G[10G](4)': (4, 1),
    '4x10G[25G](4)': (4, 1),
}
SFP_MODES = {'1x10G': (1, 1)}


def child_ports(name, mode):
    """Call the production parser; project only fields asserted by these fixtures."""
    ports = portconfig.get_child_ports(name, mode, str(PLATFORM_JSON))
    return {name: {key: row.get(key) for key in ('alias', 'lanes', 'speed', 'fec')}
            for name, row in ports.items()}


def read_port_config_ini():
    rows = {}
    for line in PORT_CONFIG_INI.read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        name, lanes, alias, speed, index, mtu, fec = line.split()
        rows[name] = dict(lanes=lanes, alias=alias, speed=speed, index=index, mtu=mtu, fec=fec)
    return rows


class PlatformJsonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.interfaces = json.loads(PLATFORM_JSON.read_text())['interfaces']
        cls.hwsku = json.loads(HWSKU_JSON.read_text())['interfaces']
        cls.ini = read_port_config_ini()

    def test_every_cage_and_sfp_is_a_parent_port(self):
        self.assertEqual(sorted(self.interfaces), sorted(self.ini))
        self.assertEqual(len(self.interfaces), 34)

    def test_lanes_and_index_match_the_stock_hwsku(self):
        for name, intf in self.interfaces.items():
            lanes = intf['lanes'].split(',')
            self.assertEqual(intf['lanes'], self.ini[name]['lanes'], name)
            self.assertEqual(intf['index'], ','.join([self.ini[name]['index']] * len(lanes)), name)
            # A cage is one serdes group of 8 consecutive lanes, 8g+1 .. 8g+8.
            if len(lanes) == 8:
                first = int(lanes[0])
                self.assertEqual((first - 1) % 8, 0, name)
                self.assertEqual([int(l) for l in lanes], list(range(first, first + 8)), name)

    def test_no_lane_is_used_twice(self):
        seen = []
        for intf in self.interfaces.values():
            seen += intf['lanes'].split(',')
        self.assertEqual(len(seen), len(set(seen)))

    def test_breakout_modes_and_aliases(self):
        for name, intf in self.interfaces.items():
            alias = self.ini[name]['alias']
            modes = intf['breakout_modes']
            expected = QSFPDD_MODES if alias.startswith('QSFP') else SFP_MODES
            self.assertEqual(sorted(modes), sorted(expected), name)
            for mode, (num_ports, per_port) in expected.items():
                self.assertEqual(len(modes[mode]), num_ports, (name, mode))
                if num_ports == 1:
                    self.assertEqual(modes[mode], [alias], (name, mode))
                else:
                    self.assertEqual(modes[mode], ['%s/%d' % (alias, n + 1) for n in range(num_ports)], (name, mode))
                self.assertLessEqual(num_ports * per_port, len(intf['lanes'].split(',')), (name, mode))

    @unittest.skipIf(portconfig is None, SONIC_DEPS)
    def test_real_parser_accepts_every_offered_mode(self):
        for name, interface in self.interfaces.items():
            for mode, aliases in interface['breakout_modes'].items():
                with self.subTest(port=name, mode=mode):
                    ports = portconfig.get_child_ports(name, mode, str(PLATFORM_JSON))
                    self.assertEqual([row['alias'] for row in ports.values()], aliases)
                    if mode in ('4x25G[10G](4)', '4x10G[25G](4)'):
                        speed = '25000' if mode.startswith('4x25G') else '10000'
                        self.assertEqual({row['speed'] for row in ports.values()}, {speed})

    @unittest.skipIf(portconfig is None, SONIC_DEPS)
    def test_hwsku_json_reproduces_port_config_ini(self):
        legacy = portconfig.parse_port_config_file(str(PORT_CONFIG_INI))[0]
        generated = portconfig.parse_platform_json_file(str(HWSKU_JSON), str(PLATFORM_JSON))[0]
        # The sole additional field is the DPB position for an unbroken cage.
        for row in generated.values():
            self.assertEqual(row.pop('subport'), '0')
        self.assertEqual(generated, legacy)

    @unittest.skipIf(portconfig is None, SONIC_DEPS)
    def test_optional_attributes_remain_explicit_or_omitted(self):
        hwsku = copy.deepcopy(self.hwsku)
        hwsku['Ethernet0']['mtu'] = '1600'
        hwsku['Ethernet0']['fec'] = 'none'
        del hwsku['Ethernet8']['mtu']
        del hwsku['Ethernet256']['fec']
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'hwsku.json'
            path.write_text(json.dumps({'interfaces': hwsku}))
            ports = portconfig.parse_platform_json_file(str(path), str(PLATFORM_JSON))[0]
        self.assertEqual(ports['Ethernet0']['mtu'], '1600')
        self.assertEqual(ports['Ethernet0']['fec'], 'none')
        self.assertNotIn('mtu', ports['Ethernet8'])
        self.assertNotIn('fec', ports['Ethernet256'])

    @unittest.skipIf(portconfig is None, SONIC_DEPS)
    def test_sonic_cfggen_default_ports_are_equivalent(self):
        # Redirect only the HWSKU lookup to fixture files. Execute production
        # sonic-cfggen without CONFIG_DB writes or a running switch's HWSKU.
        wrapper = (
            'import portconfig, runpy, sys; '
            'portconfig.HWSKU_ROOT_PATH = sys.argv.pop(1); '
            'script = sys.argv.pop(1); sys.argv[0] = script; '
            'runpy.run_path(script, run_name="__main__")'
        )
        env = dict(os.environ, CFGGEN_UNIT_TESTING='1')
        env['PYTHONPATH'] = str(CONFIG_ENGINE) + os.pathsep + env.get('PYTHONPATH', '')
        def generate(portfile, extra=(), preset=None):
            output = subprocess.check_output([
                sys.executable, '-c', wrapper, str(HWSKU_DIR),
                str(CONFIG_ENGINE / 'sonic-cfggen'), '-k', 'Silverstone-x',
                '-p', str(portfile), *extra,
                *(['--preset', preset] if preset else ['--print-data'])], env=env)
            return json.loads(output)['PORT']
        for preset in (None, 'l1', 'l2'):
            with self.subTest(preset=preset):
                legacy = generate(PORT_CONFIG_INI, preset=preset)
                generated = generate(PLATFORM_JSON, ('-S', str(HWSKU_JSON)), preset=preset)
                for row in generated.values():
                    self.assertEqual(row.pop('subport'), '0')
                    # l1 overrides MTU; l2 preserves an explicit source value.
                    self.assertEqual(row['mtu'], '9100' if preset == 'l1' else '9126')
                self.assertEqual(generated, legacy)

    @unittest.skipIf(portconfig is None, SONIC_DEPS)
    def test_child_ports_match_the_static_sonix_breakouts(self):
        """The names, lanes and speeds DPB produces equal what the static SONIX
        HWSKU of ixp-lab-sw1 defines by hand for the same cages."""
        qsfp25 = self.interfaces['Ethernet192']
        self.assertEqual(
            child_ports('Ethernet192', '4x100G'),
            {
                'Ethernet192': {'alias': 'QSFP25/1', 'lanes': '89,90', 'speed': '100000', 'fec': 'rs'},
                'Ethernet194': {'alias': 'QSFP25/2', 'lanes': '91,92', 'speed': '100000', 'fec': 'rs'},
                'Ethernet196': {'alias': 'QSFP25/3', 'lanes': '93,94', 'speed': '100000', 'fec': 'rs'},
                'Ethernet198': {'alias': 'QSFP25/4', 'lanes': '95,96', 'speed': '100000', 'fec': 'rs'},
            })
        qsfp3 = self.interfaces['Ethernet16']
        self.assertEqual(
            child_ports('Ethernet16', '4x10G[25G](4)'),
            {
                'Ethernet16': {'alias': 'QSFP3/1', 'lanes': '201', 'speed': '10000', 'fec': None},
                'Ethernet17': {'alias': 'QSFP3/2', 'lanes': '202', 'speed': '10000', 'fec': None},
                'Ethernet18': {'alias': 'QSFP3/3', 'lanes': '203', 'speed': '10000', 'fec': None},
                'Ethernet19': {'alias': 'QSFP3/4', 'lanes': '204', 'speed': '10000', 'fec': None},
            })
        qsfp1 = self.interfaces['Ethernet0']
        self.assertEqual(
            child_ports('Ethernet0', '2x200G'),
            {
                'Ethernet0': {'alias': 'QSFP1/1', 'lanes': '217,218,219,220', 'speed': '200000', 'fec': 'rs'},
                'Ethernet4': {'alias': 'QSFP1/2', 'lanes': '221,222,223,224', 'speed': '200000', 'fec': 'rs'},
            })


class DeviceDataCheckerTest(unittest.TestCase):
    """sonic-device-data's `make test` runs these checkers over every file in
    device/, and a failure stops every image build at sonic-device-data.deb."""

    def run_checker(self, checker, filename, own_file):
        files = sorted(str(f) for f in (REPO_ROOT / 'device').rglob(filename))
        self.assertIn(str(own_file), files)
        result = subprocess.run([sys.executable, str(DEVICE_DATA_TESTS / checker)] + files,
                                capture_output=True, text=True)
        failures = [line for line in result.stdout.splitlines() if 'passed validity check' not in line]
        self.assertEqual(result.returncode, 0, '\n'.join(failures) + result.stderr)

    def test_every_hwsku_json_passes_hwsku_json_checker(self):
        self.run_checker('hwsku_json_checker', 'hwsku.json', HWSKU_JSON)

    def test_every_platform_json_passes_platform_json_checker(self):
        self.run_checker('platform_json_checker', 'platform.json', PLATFORM_JSON)

if __name__ == '__main__':
    unittest.main()
