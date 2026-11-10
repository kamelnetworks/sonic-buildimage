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
from unittest.mock import MagicMock, patch
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
IPMIHELPER = MODULE_DIR.parent / 'common' / 'ipmihelper.py'
SYSTEM_HEALTH = Path(__file__).resolve().parents[5] / 'src' / 'system-health'

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
    """Stands in for the IpmiSensor instance: no ipmitool, canned thresholds.
    failed=True is a BMC that doesn't answer (ipmitool fails)."""

    def __init__(self, thresholds, reading=32, failed=False):
        self.thresholds = dict(thresholds)
        self.reading = reading
        self.failed = failed

    def get_threshold(self, threshold_type):
        if not self.failed and threshold_type in self.thresholds:
            return True, self.thresholds[threshold_type]
        return False, 0

    def is_threshold_readable(self, threshold_type):
        if self.failed:
            return None
        return threshold_type in self.thresholds

    def get_reading(self):
        if self.failed:
            return False, 0
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
        return ipmihelper, sonic_platform.thermal


ipmihelper, thermal = load_thermal()
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


class FailedBmcReadTest(unittest.TestCase):
    """A failed BMC read must not pass as fine (PR #155 review): it is N/A,
    never the platform default and never 0 C."""

    def test_failed_threshold_read_is_not_the_default(self):
        asic = make_thermal('ASIC On-board')
        asic.sensor.failed = True
        self.assertIsNone(asic.get_high_threshold())
        self.assertIsNone(asic.get_high_critical_threshold())

    def test_failed_temperature_read_is_not_zero(self):
        for name in SENSOR_NAMES:
            with self.subTest(sensor=name):
                obj = make_thermal(name)
                obj.sensor.failed = True
                self.assertIsNone(obj.get_temperature())

    def test_failed_read_on_sensor_with_bmc_thresholds(self):
        cpu = make_thermal('CPU On-board')
        cpu.sensor.failed = True
        self.assertIsNone(cpu.get_high_threshold())
        self.assertIsNone(cpu.get_high_critical_threshold())

    def test_failed_read_on_sensor_without_default(self):
        left = make_thermal('System Front Left')
        left.sensor.failed = True
        self.assertIsNone(left.get_high_threshold())


# Raw replies recorded on ixp-lab-sw4 (BMC firmware 0.55), keyed by the
# ipmitool arguments ipmihelper sends (decimal, as it formats them).
# M=1, B=0, exponents 0: the converted reading is the raw value.
LINEAR_FACTORS = ' 00 01 00 00 00 00 00'
RAW_REPLIES = {
    # ASIC On-board (0x2): reading 31 C, no thresholds at all.
    'raw 4 45 2': ' 1f c0 c0 00',
    'raw 4 35 2 31': LINEAR_FACTORS,
    'raw 4 39 2': ' 00 00 00 00 00 00 00',
    # CPU On-board (0xe): 29 C, upper non-critical 90, critical 94.
    'raw 4 45 14': ' 1d c0 c0 00',
    'raw 4 35 14 29': LINEAR_FACTORS,
    'raw 4 39 14': ' 18 00 00 00 5a 5e 00',
    'raw 4 35 14 90': LINEAR_FACTORS,
    'raw 4 35 14 94': LINEAR_FACTORS,
}


class FakeProc:
    def __init__(self, output, returncode=0):
        self.output, self.returncode = output, returncode

    def communicate(self):
        return self.output, None

    def wait(self):
        return self.returncode


class FakeIpmitool:
    """subprocess.Popen replacement: RAW_REPLIES minus the commands in
    failing, which fail like ipmitool does when the BMC doesn't answer."""

    def __init__(self, failing=()):
        self.failing = set(failing)

    def __call__(self, argv, **kwargs):
        key = ' '.join(argv[1:])
        if key in self.failing or key not in RAW_REPLIES:
            return FakeProc('Unable to send RAW command (channel=0x0 netfn=0x4 '
                            'lun=0x0 cmd=0x27 rsp=0xff): Unspecified error', 1)
        return FakeProc(RAW_REPLIES[key])


class IpmitoolReplyTest(unittest.TestCase):
    """The real IpmiSensor against fake ipmitool replies: an all-zero
    readable mask (BMC has no threshold) is the default, a failed command is
    N/A."""

    def setUp(self):
        ipmihelper.clear_cache()
        self.addCleanup(ipmihelper.clear_cache)

    def thermal(self, name, failing=()):
        popen = patch.object(ipmihelper.subprocess, 'Popen', FakeIpmitool(failing))
        popen.start()
        self.addCleanup(popen.stop)
        return Thermal(SENSOR_NAMES.index(name))

    def test_no_bmc_threshold_is_the_default(self):
        asic = self.thermal('ASIC On-board')
        self.assertIs(asic.sensor.is_threshold_readable('UpperNonCritical'), False)
        self.assertEqual(asic.get_temperature(), 31.0)
        self.assertEqual(asic.get_high_threshold(), 78.0)
        self.assertEqual(asic.get_high_critical_threshold(), 80.0)

    def test_failed_threshold_command_is_none(self):
        asic = self.thermal('ASIC On-board', failing={'raw 4 39 2'})
        self.assertIsNone(asic.sensor.is_threshold_readable('UpperNonCritical'))
        self.assertEqual(asic.get_temperature(), 31.0)
        self.assertIsNone(asic.get_high_threshold())
        self.assertIsNone(asic.get_high_critical_threshold())

    def test_failed_reading_command_is_none(self):
        asic = self.thermal('ASIC On-board', failing={'raw 4 45 2'})
        self.assertIsNone(asic.get_temperature())

    def test_failed_factors_command_is_none(self):
        cpu = self.thermal('CPU On-board', failing={'raw 4 35 14 90'})
        self.assertIs(cpu.sensor.is_threshold_readable('UpperNonCritical'), True)
        self.assertIsNone(cpu.get_high_threshold())
        self.assertEqual(cpu.get_high_critical_threshold(), 94.0)

    def test_bmc_thresholds(self):
        cpu = self.thermal('CPU On-board')
        self.assertEqual(cpu.get_temperature(), 29.0)
        self.assertEqual(cpu.get_high_threshold(), 90.0)
        self.assertEqual(cpu.get_high_critical_threshold(), 94.0)

    def test_threshold_mask_costs_no_extra_bmc_call(self):
        asic = self.thermal('ASIC On-board')
        with patch.object(ipmihelper, '_run_ipmitool',
                          wraps=ipmihelper._run_ipmitool) as run:
            asic.get_high_threshold()
            asic.get_high_critical_threshold()
        self.assertEqual(run.call_count, 1)


def try_get(callback, default='N/A'):
    """thermalctld's try_get (sonic-platform-daemons sonic-thermalctld),
    which turns None and NotImplementedError into 'N/A'."""
    try:
        ret = callback()
        if ret is None:
            ret = default
    except NotImplementedError:
        ret = default
    return ret


def load_hardware_checker():
    """system-health's HardwareChecker with stand-ins for swsscommon."""
    swsscommon = types.ModuleType('swsscommon')
    swsscommon.swsscommon = types.ModuleType('swsscommon.swsscommon')
    swsscommon.swsscommon.SonicV2Connector = MagicMock()
    natsort = types.ModuleType('natsort')
    natsort.natsorted = sorted
    # The package's __init__ imports every checker (docker, ...), so load
    # just the two modules HardwareChecker needs, under an empty package.
    package = types.ModuleType('health_checker')
    package.__path__ = [str(SYSTEM_HEALTH / 'health_checker')]
    stubs = {'swsscommon': swsscommon, 'swsscommon.swsscommon': swsscommon.swsscommon,
             'natsort': natsort, 'health_checker': package}
    with patch.dict(sys.modules, stubs):
        for name in ('health_checker', 'hardware_checker'):
            spec = importlib.util.spec_from_file_location(
                'health_checker.' + name, SYSTEM_HEALTH / 'health_checker' / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            sys.modules['health_checker.' + name] = module
            spec.loader.exec_module(module)
        return module.HardwareChecker


class FakeStateDb:
    STATE_DB = 'STATE_DB'

    def __init__(self, tables):
        self.tables = tables

    def keys(self, db, pattern):
        prefix = pattern.rstrip('*')
        return [key for key in self.tables if key.startswith(prefix)]

    def get(self, db, key, field):
        return self.tables.get(key, {}).get(field)


class SystemHealthTest(unittest.TestCase):
    """What system-health makes of the 'ASIC On-board' entry thermalctld
    writes: OK with the default, Not OK (no traceback) after a failed read."""

    @classmethod
    def setUpClass(cls):
        cls.HardwareChecker = load_hardware_checker()

    def asic_status(self, asic):
        temperature = try_get(asic.get_temperature)
        high_threshold = try_get(asic.get_high_threshold) if temperature != 'N/A' else 'N/A'
        checker = self.HardwareChecker.__new__(self.HardwareChecker)
        checker._info = {}
        checker._db = FakeStateDb({'TEMPERATURE_INFO|ASIC On-board': {
            'temperature': str(temperature), 'high_threshold': str(high_threshold)}})
        checker._check_asic_status(MagicMock(ignore_devices=[]))
        return checker._info['ASIC On-board']

    def test_default_threshold_is_ok(self):
        status = self.asic_status(make_thermal('ASIC On-board'))
        self.assertEqual(status['status'], 'OK')

    def test_over_default_threshold_is_not_ok(self):
        asic = make_thermal('ASIC On-board')
        asic.sensor.reading = 79
        status = self.asic_status(asic)
        self.assertEqual(status['status'], 'Not OK')
        self.assertIn('too hot', status['message'])

    def test_failed_threshold_read_is_not_ok(self):
        asic = make_thermal('ASIC On-board')
        asic.sensor.is_threshold_readable = lambda threshold_type: None
        status = self.asic_status(asic)
        self.assertEqual(status['status'], 'Not OK')
        self.assertIn('threshold=N/A', status['message'])

    def test_failed_temperature_read_is_not_ok(self):
        asic = make_thermal('ASIC On-board')
        asic.sensor.failed = True
        status = self.asic_status(asic)
        self.assertEqual(status['status'], 'Not OK')
        self.assertIn('temperature=N/A', status['message'])


if __name__ == '__main__':
    unittest.main()
