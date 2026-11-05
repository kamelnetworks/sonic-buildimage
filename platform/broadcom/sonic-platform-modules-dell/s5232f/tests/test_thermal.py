"""Hardware-free regression for #123: thermal thresholds the BMC doesn't have.

The S5232F BMC (Celestica, firmware 0.55) reports no thresholds at all for
the 'ASIC On-board' sensor (NPU_Near_temp, 0x2): 'ipmitool raw 0x04 0x27 0x2'
answers an all-zero readable mask, so IpmiSensor.get_threshold() is
(False, 0) for every type. Thermal.get_high_threshold() then fell through to
ThermalBase, which raises NotImplementedError; thermalctld wrote 'N/A' to
STATE_DB and system-health stayed Not OK with 'Invalid ASIC On-board
temperature data, temperature=32.0, threshold=N/A'. Thermal must return the
platform default for such sensors, and a threshold the BMC does report must
still win.

Needs sonic-platform-common on PYTHONPATH, e.g. from the repository root:

    PYTHONPATH=src/sonic-platform-common python3 -m pytest \
        platform/broadcom/sonic-platform-modules-dell/s5232f/tests/test_thermal.py
"""
import importlib.util
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
IPMIHELPER = MODULE_DIR.parent / 'common' / 'ipmihelper.py'

# What the lab S5232F-ON (ixp-lab-sw4, BMC firmware 0.55) reports on
# 2026-11-05, from 'ipmitool raw 0x04 0x27 <id>': { name: {type: value} }.
# Only three of the eight sensors carry thresholds at all.
BMC_THRESHOLDS = {
    'CPU On-board': {'UpperNonCritical': 90, 'UpperCritical': 94},
    'ASIC On-board': {},
    'System Front Left': {},
    'System Front Middle': {'UpperNonCritical': 78, 'UpperCritical': 80,
                            'UpperNonRecoverable': 85},
    'System Front Right': {},
    'Inlet Airflow Sensor': {},
    'PSU1 Airflow Sensor': {'UpperNonCritical': 62, 'UpperCritical': 65},
    'PSU2 Airflow Sensor': {},
}


class FakeIpmiSensor:
    """Stands in for the IpmiSensor instance: no ipmitool, canned thresholds."""

    def __init__(self, thresholds, reading=32):
        self.thresholds = dict(thresholds)
        self.reading = reading

    def get_threshold(self, threshold_type):
        if threshold_type in self.thresholds:
            return True, self.thresholds[threshold_type]
        return False, 0

    def get_reading(self):
        return True, self.reading


def load_thermal():
    """Import sonic_platform.thermal from the source tree.

    The Debian build copies common/ipmihelper.py into the package, so do the
    same here. Loaded under patch.dict so nothing leaks into sys.modules.
    """
    package = types.ModuleType('sonic_platform')
    package.__path__ = [str(MODULE_DIR / 'sonic_platform')]
    with patch.dict(sys.modules, {'sonic_platform': package}):
        spec = importlib.util.spec_from_file_location('sonic_platform.ipmihelper', IPMIHELPER)
        ipmihelper = importlib.util.module_from_spec(spec)
        sys.modules['sonic_platform.ipmihelper'] = ipmihelper
        spec.loader.exec_module(ipmihelper)
        import sonic_platform.thermal
        return sonic_platform.thermal


thermal = load_thermal()
Thermal = thermal.Thermal
SENSOR_NAMES = [entry[0] for entry in Thermal.SENSOR_MAPPING]


def make_thermal(name, thresholds=None):
    """A Thermal for the named sensor whose BMC reports the given thresholds
    (default: what the lab switch reports)."""
    with patch.object(thermal, 'IpmiSensor') as ipmi_sensor:
        obj = Thermal(SENSOR_NAMES.index(name))
    sensor_id = Thermal.SENSOR_MAPPING[SENSOR_NAMES.index(name)][1]
    ipmi_sensor.assert_called_once_with(sensor_id)
    if thresholds is None:
        thresholds = BMC_THRESHOLDS[name]
    obj.sensor = FakeIpmiSensor(thresholds)
    return obj


class AsicOnBoardDefaultTest(unittest.TestCase):
    """The sensor from #123: no BMC thresholds, so the platform default."""

    def setUp(self):
        self.asic = make_thermal('ASIC On-board')

    def test_sensor_has_no_bmc_thresholds(self):
        self.assertEqual(self.asic.sensor.get_threshold('UpperNonCritical'), (False, 0))
        self.assertEqual(self.asic.sensor.get_threshold('UpperCritical'), (False, 0))

    def test_high_threshold_is_the_platform_default(self):
        high = self.asic.get_high_threshold()
        self.assertIsInstance(high, float)
        self.assertEqual(high, 78.0)

    def test_high_critical_threshold_is_the_platform_default(self):
        crit = self.asic.get_high_critical_threshold()
        self.assertIsInstance(crit, float)
        self.assertEqual(crit, 80.0)

    def test_defaults_are_ordered_and_above_normal_operation(self):
        high = self.asic.get_high_threshold()
        crit = self.asic.get_high_critical_threshold()
        self.assertLess(high, crit)
        # The lab switch idles at about 32 C; thermalctld raises a warning
        # (and system-health flags the ASIC) once temperature > high.
        self.assertGreater(high, 60.0)
        self.assertEqual(self.asic.get_temperature(), 32.0)
        self.assertLess(self.asic.get_temperature(), high)

    def test_low_threshold_unchanged(self):
        self.assertEqual(self.asic.get_low_threshold(), 0.0)

    def test_thresholds_are_read_only(self):
        self.assertFalse(self.asic.set_high_threshold(70.0))
        self.assertFalse(self.asic.set_low_threshold(5.0))
        self.assertEqual(self.asic.get_high_threshold(), 78.0)


class BmcThresholdPrecedenceTest(unittest.TestCase):

    def test_bmc_thresholds_win_over_the_default(self):
        asic = make_thermal('ASIC On-board',
                            {'UpperNonCritical': 70, 'UpperCritical': 75})
        self.assertEqual(asic.get_high_threshold(), 70.0)
        self.assertEqual(asic.get_high_critical_threshold(), 75.0)

    def test_fallback_is_per_threshold(self):
        asic = make_thermal('ASIC On-board', {'UpperNonCritical': 70})
        self.assertEqual(asic.get_high_threshold(), 70.0)
        self.assertEqual(asic.get_high_critical_threshold(), 80.0)

    def test_sensors_with_bmc_thresholds_are_unchanged(self):
        for name in ('CPU On-board', 'System Front Middle', 'PSU1 Airflow Sensor'):
            with self.subTest(sensor=name):
                obj = make_thermal(name)
                expected = BMC_THRESHOLDS[name]
                self.assertEqual(obj.get_high_threshold(),
                                 float(expected['UpperNonCritical']))
                self.assertEqual(obj.get_high_critical_threshold(),
                                 float(expected['UpperCritical']))


class OtherSensorsTest(unittest.TestCase):
    """Sensors without a BMC threshold and without a default keep the old
    behaviour: ThermalBase raises, thermalctld shows N/A. system-health only
    checks the TEMPERATURE_INFO|ASIC* keys, so they don't affect it."""

    def test_no_default_defers_to_the_base_class(self):
        for name in ('System Front Left', 'System Front Right',
                     'Inlet Airflow Sensor', 'PSU2 Airflow Sensor'):
            with self.subTest(sensor=name):
                obj = make_thermal(name)
                self.assertNotIn(name, Thermal.DEFAULT_THRESHOLDS)
                with self.assertRaises(NotImplementedError):
                    obj.get_high_threshold()
                with self.assertRaises(NotImplementedError):
                    obj.get_high_critical_threshold()
                self.assertEqual(obj.get_low_threshold(), 0.0)


class DefaultTableTest(unittest.TestCase):

    def test_table_names_exist_and_values_are_sane(self):
        self.assertIn('ASIC On-board', Thermal.DEFAULT_THRESHOLDS)
        for name, (high, crit) in Thermal.DEFAULT_THRESHOLDS.items():
            with self.subTest(sensor=name):
                self.assertIn(name, SENSOR_NAMES)
                self.assertIsInstance(high, float)
                self.assertIsInstance(crit, float)
                self.assertLess(high, crit)

    def test_every_sensor_builds_and_reports(self):
        for index, (name, _sensor_id) in enumerate(Thermal.SENSOR_MAPPING):
            with self.subTest(sensor=name):
                obj = make_thermal(name)
                self.assertEqual(obj.index, index + 1)
                self.assertEqual(obj.get_name(), name)
                self.assertTrue(obj.get_presence())
                self.assertTrue(obj.get_status())
                self.assertEqual(obj.get_temperature(), 32.0)


if __name__ == '__main__':
    unittest.main()
