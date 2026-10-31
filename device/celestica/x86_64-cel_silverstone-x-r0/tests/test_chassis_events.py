"""Hardware-free regressions for Silverstone-X fan and SFP change events."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


def load_chassis():
    stubs = {}
    for name, symbol in (
        ('sonic_platform_base.chassis_base', 'ChassisBase'),
        ('sonic_platform.helper', 'APIHelper'),
        ('sonic_platform.fan_drawer', 'FanDrawer'),
    ):
        module = types.ModuleType(name)
        setattr(module, symbol, type(symbol, (), {}))
        stubs[name] = module
    path = Path(__file__).resolve().parents[1] / 'sonic_platform/chassis.py'
    spec = importlib.util.spec_from_file_location('silverstone_chassis', path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module.Chassis


Chassis = load_chassis()


class ChassisEventsTest(unittest.TestCase):
    def make_chassis(self, present=()):
        # Avoid chassis initialization's hardware access. The event path still
        # uses the real presence bitmap and event implementation.
        chassis = Chassis.__new__(Chassis)
        chassis.fan_module_initialized = True
        chassis.sfp_module_initialized = True
        chassis.fan_start = 0
        chassis.fan_end = 13
        chassis.port_start = 0
        chassis.port_end = 33
        chassis._fan_list = [
            Mock(get_presence=Mock(return_value=i in present))
            for i in range(14)
        ]
        chassis.get_all_sfps = Mock(return_value=[])
        chassis.modprs_register = 0
        chassis.fan_init_status = chassis.get_fan_status()
        return chassis

    def test_bitmap_matches_fan_indices(self):
        for index in range(14):
            with self.subTest(index=index):
                self.assertEqual(self.make_chassis([index]).get_fan_status(), 1 << index)

    def test_each_fan_insertion_and_removal(self):
        for index in range(14):
            with self.subTest(index=index):
                chassis = self.make_chassis()
                presence = chassis._fan_list[index].get_presence
                presence.return_value = True
                self.assertEqual(chassis.get_change_event(1),
                                 (True, {'fan': {index: '1'}, 'sfp': {}}))
                presence.return_value = False
                self.assertEqual(chassis.get_change_event(1),
                                 (True, {'fan': {index: '0'}, 'sfp': {}}))
                self.assertEqual(chassis.fan_init_status, 0)

    def test_last_tray_and_sfp_change_together(self):
        chassis = self.make_chassis(range(14))
        for index in (12, 13):
            chassis._fan_list[index].get_presence.return_value = False
        chassis.get_all_sfps.return_value = [Mock(get_presence=Mock(return_value=True))]
        self.assertEqual(chassis.get_change_event(1),
                         (True, {'fan': {12: '0', 13: '0'}, 'sfp': {0: '1'}}))
        self.assertEqual(chassis.modprs_register, 1)
        self.assertEqual(chassis.fan_init_status, (1 << 12) - 1)
        self.assertEqual(chassis.get_change_event(1), (True, {'fan': {}, 'sfp': {}}))


if __name__ == '__main__':
    unittest.main()
