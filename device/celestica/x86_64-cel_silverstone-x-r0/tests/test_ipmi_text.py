"""Exercise textual IPMI responses through the real helper and API callers."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


class DeviceBase:
    FAN_DIRECTION_INTAKE = 'intake'
    FAN_DIRECTION_EXHAUST = 'exhaust'
    STATUS_LED_COLOR_OFF = 'off'
    STATUS_LED_COLOR_GREEN = 'green'
    STATUS_LED_COLOR_AMBER = 'amber'
    STATUS_LED_COLOR_RED = 'red'


def load_modules():
    modules = {}
    for name, attrs in (
        ('sonic_py_common', {'device_info': Mock(), 'logger': Mock()}),
        ('sonic_platform_base.fan_base', {'FanBase': DeviceBase}),
        ('sonic_platform_base.psu_base', {'PsuBase': DeviceBase}),
    ):
        modules[name] = types.ModuleType(name)
        modules[name].__dict__.update(attrs)
    root = Path(__file__).resolve().parents[1] / 'sonic_platform'
    with patch.dict(sys.modules, modules):
        for name in ('helper', 'fan', 'psu'):
            fullname = 'sonic_platform.' + name
            spec = importlib.util.spec_from_file_location(fullname, root / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            sys.modules[fullname] = module
            modules[name] = module
            spec.loader.exec_module(module)
    return modules['helper'], modules['fan'], modules['psu']


helper, fan_module, psu_module = load_modules()


class IpmiTextTest(unittest.TestCase):
    def setUp(self):
        self.helper = helper.APIHelper.__new__(helper.APIHelper)
        self.process = Mock()
        self.process.communicate.return_value = (b' 01\n', b'')
        popen = patch.object(helper.subprocess, 'Popen', return_value=self.process)
        popen.start()
        self.addCleanup(popen.stop)

    def make_device(self, cls):
        device = cls.__new__(cls)
        device._api_helper = self.helper
        device.index = 0
        device.fan_tray_index = 0
        device.is_psu_fan = False
        device.refresh_direction_time = 0
        device.refresh_status_led_time = 0
        device.refresh_time = 0
        device.refresh_target_speed_time = 0
        return device

    def test_helper_returns_stripped_text(self):
        self.assertEqual(self.helper.ipmi_raw('0x3a', '0x62 0'), (True, '01'))
        self.process.communicate.return_value = (b'', b'')
        self.assertEqual(self.helper.ipmi_raw('0x3a', '0x62 0'), (True, ''))

    def test_helper_error_retains_empty_text(self):
        self.process.communicate.return_value = (b'01', b'failed')
        self.assertEqual(self.helper.ipmi_raw('0x3a', '0x62 0'), (False, ''))

    def test_airflow_and_presence(self):
        for raw, direction, present in ((b'01', 'intake', False),
                                        (b'00', 'exhaust', True)):
            with self.subTest(raw=raw):
                self.process.communicate.return_value = (raw + b'\n', b'')
                fan = self.make_device(fan_module.Fan)
                self.assertEqual(fan.get_direction(), direction)
                self.assertEqual(fan.get_presence(), present)

    def test_fan_and_psu_led_colors(self):
        for raw, fan_color, psu_color in ((b'00', 'off', 'off'),
                                          (b'01', 'green', 'green'),
                                          (b'02', 'red', 'amber')):
            with self.subTest(raw=raw):
                self.process.communicate.return_value = (raw, b'')
                self.assertEqual(self.make_device(fan_module.Fan).get_status_led(), fan_color)
                self.assertEqual(self.make_device(psu_module.Psu).get_status_led(), psu_color)

    def test_numeric_consumers_accept_text(self):
        self.process.communicate.return_value = (b'ff\n', b'')
        self.assertEqual(self.make_device(fan_module.Fan).get_target_speed(), 100)
        self.process.communicate.return_value = (b'78 00 00\n', b'')
        psu = self.make_device(psu_module.Psu)
        self.assertEqual(psu.get_voltage(), 12.0)
        self.assertEqual(psu.get_current(), 60.0)
        self.assertEqual(psu.get_power(), 720.0)


if __name__ == '__main__':
    unittest.main()
