"""Check the committed pcie.yaml the way pcied and pcieutil read it (#120)."""
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import yaml

from sonic_platform_base.sonic_pcie.pcie_common import PcieUtil

PLATFORM_DIR = Path(__file__).resolve().parents[1]
PCIE_YAML = PLATFORM_DIR / 'pcie.yaml'

# Devices whose absence would mean the switch is broken, not a different board.
TERALYNX_7_ASIC = ('04', '00', '0', '1b58')
I210_MANAGEMENT_NIC = ('09', '00', '0', '1533')
SWITCH_BOARD_FPGA = ('0a', '00', '0', '7021')


def bdf(item):
    return (item['bus'], item['dev'], item['fn'], item['id'])


class PcieYamlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PCIE_YAML) as f:
            cls.devices = yaml.safe_load(f)

    def test_is_a_list_of_devices_with_the_fields_pcied_reads(self):
        self.assertIsInstance(self.devices, list)
        self.assertGreater(len(self.devices), 50)
        for item in self.devices:
            self.assertEqual(set(item), {'bus', 'dev', 'fn', 'id', 'name'}, item)
            self.assertRegex(item['bus'], r'^[0-9a-f]{2}$')
            self.assertRegex(item['dev'], r'^[0-9a-f]{2}$')
            self.assertRegex(item['fn'], r'^[0-7]$')
            self.assertRegex(item['id'], r'^[0-9a-f]{4}$')
            self.assertTrue(item['name'].strip(), item)

    def test_no_duplicate_addresses(self):
        addresses = [(i['bus'], i['dev'], i['fn']) for i in self.devices]
        self.assertEqual(len(addresses), len(set(addresses)))

    def test_lists_the_asic_management_nic_and_fpga(self):
        found = {bdf(i) for i in self.devices}
        for device in (TERALYNX_7_ASIC, I210_MANAGEMENT_NIC, SWITCH_BOARD_FPGA):
            self.assertIn(device, found)

    def test_pcieutil_loads_it_and_passes_when_sysfs_has_every_device(self):
        util = PcieUtil(str(PLATFORM_DIR))
        with patch.object(PcieUtil, 'check_pcie_sysfs', return_value=True):
            util.get_pcie_check()
        self.assertEqual({i['result'] for i in util.confInfo}, {'Passed'})
        self.assertEqual(len(util.confInfo), len(self.devices))

    def test_pcieutil_reports_a_missing_asic(self):
        def sysfs(self, domain=0, bus=0, device=0, func=0):
            return (bus, device, func) != (0x04, 0x00, 0)

        util = PcieUtil(str(PLATFORM_DIR))
        with patch.object(PcieUtil, 'check_pcie_sysfs', sysfs):
            util.get_pcie_check()
        failed = [bdf(i) for i in util.confInfo if i['result'] == 'Failed']
        self.assertEqual(failed, [TERALYNX_7_ASIC])


if __name__ == '__main__':
    unittest.main()
