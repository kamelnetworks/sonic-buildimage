"""Dynamic Port Breakout files for the Silverstone-X (kamelnetworks/sonic-buildimage#103).

`config interface breakout` needs `platform.json` in the platform directory
and `hwsku.json` in the HWSKU directory. sonic-config-engine's portconfig.py
then derives the child ports of a breakout mode from the parent's lane list:
child `n` is `Ethernet<base + n * lanes_per_child>` and gets the next alias of
the mode. These checks mirror that derivation (portconfig itself needs
swsscommon) and pin the files to the layouts that already run on Teralynx 7
hardware: the stock 32x400G `Silverstone-x` HWSKU and the static SONIX
breakouts (4x100G on QSFP25/26, 4x10G on QSFP3 of ixp-lab-sw1).
"""
import json
import re
import unittest
from pathlib import Path

PLATFORM_DIR = Path(__file__).resolve().parents[1]
PLATFORM_JSON = PLATFORM_DIR / 'platform.json'
HWSKU_DIR = PLATFORM_DIR / 'Silverstone-x'
HWSKU_JSON = HWSKU_DIR / 'hwsku.json'
PORT_CONFIG_INI = HWSKU_DIR / 'port_config.ini'

# portconfig.BRKOUT_PATTERN
BRKOUT_PATTERN = r'(\d{1,6})x(\d{1,6}G?)(\[(\d{1,6}G?,?)*\])?(\((\d{1,6})\))?'

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


def speed_to_int(speed):
    return int(speed[:-1]) * 1000 if speed.endswith('G') else int(speed)


def child_ports(name, lanes, aliases, mode):
    """The PORT entries portconfig.BreakoutCfg.get_config derives for `mode`."""
    num_ports, speed, _, _, _, assigned = re.fullmatch(BRKOUT_PATTERN, mode).groups()
    num_ports = int(num_ports)
    assigned = int(assigned) if assigned else len(lanes)
    per_port = assigned // num_ports
    base = int(name[len('Ethernet'):])
    ports = {}
    for n in range(num_ports):
        lane_id = n * per_port
        ports['Ethernet%d' % (base + lane_id)] = {
            'alias': aliases[n],
            'lanes': ','.join(lanes[lane_id:lane_id + per_port]),
            'speed': str(speed_to_int(speed)),
            'fec': 'rs' if speed_to_int(speed) // per_port >= 50000 else None,
        }
    return ports


def read_port_config_ini():
    rows = {}
    for line in PORT_CONFIG_INI.read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        name, lanes, alias, speed, index, mtu, fec = line.split()
        rows[name] = dict(lanes=lanes, alias=alias, speed=speed, index=index, fec=fec)
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
                self.assertRegex(mode, '^' + BRKOUT_PATTERN + '$')
                self.assertEqual(len(modes[mode]), num_ports, (name, mode))
                if num_ports == 1:
                    self.assertEqual(modes[mode], [alias], (name, mode))
                else:
                    self.assertEqual(modes[mode], ['%s/%d' % (alias, n + 1) for n in range(num_ports)], (name, mode))
                self.assertLessEqual(num_ports * per_port, len(intf['lanes'].split(',')), (name, mode))

    def test_hwsku_json_reproduces_port_config_ini(self):
        """sonic-cfggen prefers platform.json + hwsku.json over port_config.ini,
        so the default breakout of every port must give the stock 32x400G
        (+ 2x10G SFP) layout, FEC included."""
        self.assertEqual(sorted(self.hwsku), sorted(self.interfaces))
        generated = {}
        for name, entry in self.hwsku.items():
            intf = self.interfaces[name]
            mode = entry['default_brkout_mode']
            self.assertIn(mode, intf['breakout_modes'], name)
            ports = child_ports(name, intf['lanes'].split(','), intf['breakout_modes'][mode], mode)
            for port in ports.values():
                if 'fec' in entry:
                    port['fec'] = entry['fec']
            generated.update(ports)
        self.assertEqual(sorted(generated), sorted(self.ini))
        for name, port in generated.items():
            row = self.ini[name]
            self.assertEqual(port['alias'], row['alias'], name)
            self.assertEqual(port['lanes'], row['lanes'], name)
            self.assertEqual(port['speed'], row['speed'], name)
            self.assertEqual(port['fec'] or 'none', row['fec'], name)

    def test_child_ports_match_the_static_sonix_breakouts(self):
        """The names, lanes and speeds DPB produces equal what the static SONIX
        HWSKU of ixp-lab-sw1 defines by hand for the same cages."""
        qsfp25 = self.interfaces['Ethernet192']
        self.assertEqual(
            child_ports('Ethernet192', qsfp25['lanes'].split(','), qsfp25['breakout_modes']['4x100G'], '4x100G'),
            {
                'Ethernet192': {'alias': 'QSFP25/1', 'lanes': '89,90', 'speed': '100000', 'fec': 'rs'},
                'Ethernet194': {'alias': 'QSFP25/2', 'lanes': '91,92', 'speed': '100000', 'fec': 'rs'},
                'Ethernet196': {'alias': 'QSFP25/3', 'lanes': '93,94', 'speed': '100000', 'fec': 'rs'},
                'Ethernet198': {'alias': 'QSFP25/4', 'lanes': '95,96', 'speed': '100000', 'fec': 'rs'},
            })
        qsfp3 = self.interfaces['Ethernet16']
        self.assertEqual(
            child_ports('Ethernet16', qsfp3['lanes'].split(','), qsfp3['breakout_modes']['4x10G[25G](4)'], '4x10G[25G](4)'),
            {
                'Ethernet16': {'alias': 'QSFP3/1', 'lanes': '201', 'speed': '10000', 'fec': None},
                'Ethernet17': {'alias': 'QSFP3/2', 'lanes': '202', 'speed': '10000', 'fec': None},
                'Ethernet18': {'alias': 'QSFP3/3', 'lanes': '203', 'speed': '10000', 'fec': None},
                'Ethernet19': {'alias': 'QSFP3/4', 'lanes': '204', 'speed': '10000', 'fec': None},
            })
        qsfp1 = self.interfaces['Ethernet0']
        self.assertEqual(
            child_ports('Ethernet0', qsfp1['lanes'].split(','), qsfp1['breakout_modes']['2x200G'], '2x200G'),
            {
                'Ethernet0': {'alias': 'QSFP1/1', 'lanes': '217,218,219,220', 'speed': '200000', 'fec': 'rs'},
                'Ethernet4': {'alias': 'QSFP1/2', 'lanes': '221,222,223,224', 'speed': '200000', 'fec': 'rs'},
            })


if __name__ == '__main__':
    unittest.main()
