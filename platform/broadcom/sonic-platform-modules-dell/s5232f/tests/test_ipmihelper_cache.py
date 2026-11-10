"""The ipmitool reply cache in common/ipmihelper.py (issue #163).

On the S5232F every sensor attribute is an `ipmitool` subprocess, one BMC
round trip takes 0.3-0.8 s and a reading costs two of them (Get Sensor
Reading plus Get Sensor Reading Factors). thermalctld asks the same
question several times per cycle, so a cycle took 32-36 s, over its 30 s
budget. ipmihelper now answers read-only ipmitool commands from a
short-lived per-process cache. These tests pin down what is cached, for
how long, that cached replies are not shared by reference, and that
anything else is never cached and invalidates the cache.

Needs sonic-platform-common on PYTHONPATH for the Fan/Thermal cases, e.g.
from the repository root:

    PYTHONPATH=src/sonic-platform-common python3 -m pytest \
        platform/broadcom/sonic-platform-modules-dell/s5232f/tests
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
IPMIHELPER = MODULE_DIR.parent / 'common' / 'ipmihelper.py'

FAN_FRU_PRINT = """\
 Board Mfg Date        : Thu 09 May 2019 06:50:00 AM UTC UTC
 Board Serial          : CN07R5RFCES0094D0105
 Board Part Number     : 07R5RFA00
"""

# M=1, B=0, exponents 0: the converted reading is the raw value.
LINEAR_FACTORS = ' 00 01 00 00 00 00 00'


def load_platform():
    """Import ipmihelper, fan and thermal from the source tree, the way the
    Debian build lays them out (common/ipmihelper.py inside the package)."""
    package = types.ModuleType('sonic_platform')
    package.__path__ = [str(MODULE_DIR / 'sonic_platform')]
    with patch.dict(sys.modules, {'sonic_platform': package}):
        spec = importlib.util.spec_from_file_location('sonic_platform.ipmihelper', IPMIHELPER)
        ipmihelper = importlib.util.module_from_spec(spec)
        sys.modules['sonic_platform.ipmihelper'] = ipmihelper
        spec.loader.exec_module(ipmihelper)
        import sonic_platform.fan
        import sonic_platform.thermal
        import sonic_platform.psu
        return ipmihelper, sonic_platform.fan, sonic_platform.thermal, sonic_platform.psu


ipmihelper, fan_module, thermal_module, psu_module = load_platform()


class FakeProc:
    def __init__(self, output, returncode=0):
        self.output, self.returncode = output, returncode

    def communicate(self):
        return self.output, None

    def wait(self):
        return self.returncode


class FakeIpmitool:
    """subprocess.Popen replacement serving canned ipmitool output and
    recording every call (argv after 'ipmitool', as one string)."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, argv, **kwargs):
        assert argv[0] == 'ipmitool', argv
        key = ' '.join(argv[1:])
        self.calls.append(key)
        response = self.responses.get(key, ('Unable to send RAW command', 1))
        if isinstance(response, tuple):
            return FakeProc(*response)
        return FakeProc(response)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def sensor_responses(sensor_id=1, raw=0x1e):
    return {
        'raw 4 45 {}'.format(sensor_id): ' {:02x} c0 00 00'.format(raw),
        'raw 4 35 {} {}'.format(sensor_id, raw): LINEAR_FACTORS,
        # Readable mask 0x38: upper non-critical 78, critical 80, non-recoverable 85.
        'raw 4 39 {}'.format(sensor_id): ' 38 00 00 00 4e 50 55',
        'raw 4 35 {} 78'.format(sensor_id): LINEAR_FACTORS,
        'raw 4 35 {} 80'.format(sensor_id): LINEAR_FACTORS,
    }


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.enterContext(patch.object(ipmihelper.time, 'monotonic', self.clock))
        ipmihelper.clear_cache()
        self.addCleanup(ipmihelper.clear_cache)

    def use_ipmitool(self, responses):
        self.ipmitool = FakeIpmitool(responses)
        self.enterContext(patch.object(ipmihelper.subprocess, 'Popen', self.ipmitool))

    # -------------------------------------------------------- readings

    def test_reading_twice_is_one_pair_of_round_trips(self):
        self.use_ipmitool(sensor_responses())
        sensor = ipmihelper.IpmiSensor(1)
        self.assertEqual(sensor.get_reading(), (True, 30))
        self.assertEqual(sensor.get_reading(), (True, 30))
        self.assertEqual(self.ipmitool.calls, ['raw 4 45 1', 'raw 4 35 1 30'])

    def test_reading_expires_after_the_read_ttl(self):
        self.use_ipmitool(sensor_responses())
        sensor = ipmihelper.IpmiSensor(1)
        self.assertEqual(sensor.get_reading(), (True, 30))
        self.ipmitool.responses.update(sensor_responses(raw=0x1f))
        self.clock.advance(ipmihelper.CACHE_TTL_READ - 1)
        self.assertEqual(sensor.get_reading(), (True, 30))
        self.clock.advance(2)
        self.assertEqual(sensor.get_reading(), (True, 31))
        self.assertEqual(self.ipmitool.calls.count('raw 4 45 1'), 2)

    def test_factors_are_static_per_raw_value(self):
        """Get Sensor Reading Factors depends only on the sensor and the raw
        value, so the factors of a raw value already seen are not fetched
        again even after the reading itself expired."""
        self.use_ipmitool(sensor_responses())
        sensor = ipmihelper.IpmiSensor(1)
        for _ in range(3):
            self.assertEqual(sensor.get_reading(), (True, 30))
            self.clock.advance(ipmihelper.CACHE_TTL_READ + 1)
        self.assertEqual(self.ipmitool.calls.count('raw 4 45 1'), 3)
        self.assertEqual(self.ipmitool.calls.count('raw 4 35 1 30'), 1)
        self.ipmitool.responses.update(sensor_responses(raw=0x1f))
        self.assertEqual(sensor.get_reading(), (True, 31))
        self.assertEqual(self.ipmitool.calls.count('raw 4 35 1 31'), 1)

    def test_discrete_reading_has_no_factors_and_is_cached(self):
        self.use_ipmitool({'raw 4 45 83': ' 00 c0 01 80'})
        sensor = ipmihelper.IpmiSensor(0x53, is_discrete=True)
        self.assertEqual(sensor.get_reading(), (True, 1))
        self.assertEqual(sensor.get_reading(), (True, 1))
        self.assertEqual(self.ipmitool.calls, ['raw 4 45 83'])

    def test_cached_reply_is_not_shared_by_reference(self):
        """get_threshold() pops the readable mask off the byte list; a second
        call must see the full reply again."""
        self.use_ipmitool(sensor_responses())
        sensor = ipmihelper.IpmiSensor(1)
        self.assertEqual(sensor.get_threshold('UpperNonCritical'), (True, 78))
        self.assertEqual(sensor.get_threshold('UpperNonCritical'), (True, 78))
        self.assertEqual(sensor.get_threshold('UpperCritical'), (True, 80))
        self.assertEqual(self.ipmitool.calls.count('raw 4 39 1'), 1)

    # ---------------------------------------------------- static data

    def test_thresholds_are_static(self):
        self.use_ipmitool(sensor_responses())
        sensor = ipmihelper.IpmiSensor(1)
        for _ in range(3):      # three thermalctld cycles
            self.assertEqual(sensor.get_threshold('UpperNonCritical'), (True, 78))
            self.assertEqual(sensor.get_threshold('UpperCritical'), (True, 80))
            self.clock.advance(60)
        self.assertEqual(self.ipmitool.calls.count('raw 4 39 1'), 1)
        self.clock.advance(ipmihelper.CACHE_TTL_STATIC)
        self.assertEqual(sensor.get_threshold('UpperNonCritical'), (True, 78))
        self.assertEqual(self.ipmitool.calls.count('raw 4 39 1'), 2)

    def test_fru_inventory_refreshes_next_poll(self):
        self.use_ipmitool({'fru print 3': FAN_FRU_PRINT})
        fru = ipmihelper.IpmiFru(3)
        self.assertEqual(fru.get_board_serial(), 'CN07R5RFCES0094D0105')
        self.assertEqual(fru.get_board_part_number(), '07R5RFA00')
        self.assertEqual(self.ipmitool.calls, ['fru print 3'])
        self.ipmitool.responses['fru print 3'] = FAN_FRU_PRINT.replace('CN07R5RFCES0094D0105', 'NEW_SERIAL')
        self.clock.advance(3)
        self.assertEqual(fru.get_board_serial(), 'NEW_SERIAL')

    def test_fru_data_refreshes_next_poll(self):
        self.use_ipmitool({'raw 10 17 3 69 0 1': ' 01 01'})
        fru = ipmihelper.IpmiFru(3)
        self.assertEqual(fru.get_fru_data(69), (True, [1]))
        self.ipmitool.responses['raw 10 17 3 69 0 1'] = ' 01 00'
        self.clock.advance(3)
        self.assertEqual(fru.get_fru_data(69), (True, [0]))

    def test_fru_offset_has_two_byte_encoding(self):
        self.use_ipmitool({'raw 10 17 3 35 1 1': ' 01 ab'})
        self.assertEqual(ipmihelper.IpmiFru(3).get_fru_data(0x123), (True, [0xab]))
        self.assertEqual(self.ipmitool.calls, ['raw 10 17 3 35 1 1'])

    def test_cache_enabled_psu_swap_without_absence_and_recovery(self):
        self.use_ipmitool({'raw 4 45 49': ' 00 c0 01 80',
                          'fru print 1': 'Board Product : PWR SPLY,PS/IO,DELTA'})
        fan = psu_module.Psu(0).get_all_fans()[0]
        self.assertEqual(fan.get_direction(), 'intake')
        self.ipmitool.responses['fru print 1'] = 'Board Product : PWR SPLY,IO/PS,DELTA'
        self.clock.advance(3)
        self.assertEqual(fan.get_direction(), 'exhaust')
        self.ipmitool.responses['fru print 1'] = 'Board Product : UNKNOWN'
        self.clock.advance(3)
        self.assertEqual(fan.get_direction(), 'N/A')
        self.ipmitool.responses['fru print 1'] = 'Board Product : PWR SPLY,PS/IO,DELTA'
        self.clock.advance(3)
        self.assertEqual(fan.get_direction(), 'intake')

    def test_observed_absence_invalidates_fru_before_its_ttl(self):
        self.use_ipmitool({'raw 4 45 49': ' 00 c0 01 80',
                          'fru print 1': 'Board Product : PWR SPLY,PS/IO,DELTA'})
        psu = psu_module.Psu(0)
        fan = psu.get_all_fans()[0]
        self.assertTrue(psu.get_presence())
        self.clock.advance(2)
        self.assertEqual(fan.get_direction(), 'intake')
        self.ipmitool.responses['raw 4 45 49'] = ' 00 c0 00 80'
        self.clock.advance(1)
        self.assertEqual(fan.get_direction(), 'N/A')
        self.ipmitool.responses['fru print 1'] = 'Board Product : PWR SPLY,IO/PS,DELTA'
        # Removal was observed at t=3; cached FRU from t=2 must be invalid.
        self.assertEqual(fan.fru.get_board_product(), 'PWR SPLY,IO/PS,DELTA')
        self.ipmitool.responses['raw 4 45 49'] = ' 00 c0 01 80'
        self.clock.advance(3)
        self.assertEqual(fan.get_direction(), 'exhaust')

    def test_static_ttl_outlives_a_thermalctld_cycle(self):
        self.assertGreater(ipmihelper.CACHE_TTL_STATIC, 60)
        self.assertLessEqual(ipmihelper.CACHE_TTL_READ, 60)

    # -------------------------------------------------------- failures

    def test_failures_are_retried_after_the_read_ttl(self):
        self.use_ipmitool({})
        fru = ipmihelper.IpmiFru(3)
        sensor = ipmihelper.IpmiSensor(1)
        self.assertEqual(fru.get_board_serial(), 'NA')
        self.assertEqual(fru.get_board_serial(), 'NA')
        self.assertEqual(sensor.get_threshold('UpperCritical'), (False, 0))
        self.assertEqual(sensor.get_threshold('UpperCritical'), (False, 0))
        self.assertEqual(self.ipmitool.calls, ['fru print 3', 'raw 4 39 1'])
        self.clock.advance(ipmihelper.CACHE_TTL_READ + 1)
        self.ipmitool.responses.update({'fru print 3': FAN_FRU_PRINT, **sensor_responses()})
        self.assertEqual(fru.get_board_serial(), 'CN07R5RFCES0094D0105')
        self.assertEqual(sensor.get_threshold('UpperCritical'), (True, 80))

    # --------------------------------------------- everything else

    def test_other_raw_commands_are_not_cached_and_drop_the_cache(self):
        self.use_ipmitool({**sensor_responses(), 'raw 6 1': ' 20 01 00 55 02 bf 00 00 00 00 00 00 00 00 00'})
        sensor = ipmihelper.IpmiSensor(1)
        sensor.get_reading()
        self.assertEqual(len(ipmihelper.get_ipmitool_raw_output('6 1')), 15)
        self.assertEqual(len(ipmihelper.get_ipmitool_raw_output('6 1')), 15)
        sensor.get_reading()
        self.assertEqual(self.ipmitool.calls, ['raw 4 45 1', 'raw 4 35 1 30', 'raw 6 1', 'raw 6 1',
                                               'raw 4 45 1', 'raw 4 35 1 30'])

    def test_hex_and_decimal_arguments_share_an_entry(self):
        self.use_ipmitool({'raw 0x04 0x2d 0x01': ' 1e c0 00 00'})
        self.assertEqual(ipmihelper.get_ipmitool_raw_output('0x04 0x2d 0x01'), [0x1e, 0xc0, 0, 0])
        self.assertEqual(ipmihelper.get_ipmitool_raw_output('4 45 1'), [0x1e, 0xc0, 0, 0])
        self.assertEqual(len(self.ipmitool.calls), 1)

    def test_cache_can_be_switched_off(self):
        self.use_ipmitool(sensor_responses())
        with patch.object(ipmihelper, 'CACHE_TTLS', {}):
            ipmihelper.IpmiSensor(1).get_reading()
            ipmihelper.IpmiSensor(1).get_reading()
        self.assertEqual(len(self.ipmitool.calls), 4)

    def test_clear_cache(self):
        self.use_ipmitool(sensor_responses())
        ipmihelper.IpmiSensor(1).get_reading()
        ipmihelper.clear_cache()
        ipmihelper.IpmiSensor(1).get_reading()
        self.assertEqual(len(self.ipmitool.calls), 4)

    # ------------------------------------ what thermalctld costs per cycle

    def test_thermal_refresh_is_four_round_trips(self):
        """get_temperature, get_high_threshold, get_high_critical_threshold:
        one reading, one threshold block and the factors for two raw values
        (the reading's and the thresholds' differ; 78 and 80 share none here
        because the fake answers per raw value)."""
        self.use_ipmitool(sensor_responses(sensor_id=0xe, raw=0x1e))
        thermal = thermal_module.Thermal(0)             # CPU On-board, 0xe
        self.assertEqual(thermal.get_temperature(), 30.0)
        self.assertEqual(thermal.get_high_threshold(), 78.0)
        self.assertEqual(thermal.get_high_critical_threshold(), 80.0)
        self.assertEqual(thermal.get_temperature(), 30.0)
        self.assertEqual(sorted(self.ipmitool.calls),
                         sorted(['raw 4 45 14', 'raw 4 35 14 30', 'raw 4 39 14',
                                 'raw 4 35 14 78', 'raw 4 35 14 80']))

    def test_fan_refresh_is_one_round_trip_per_attribute(self):
        """thermalctld reads a fan's presence and status several times per
        cycle (get_presence, get_status, get_direction, is_under_speed...)."""
        responses = {
            'raw 4 45 83': ' 00 c0 01 80',          # FAN1_prsnt
            'raw 4 45 87': ' 00 c0 01 80',          # FAN1 state
            **sensor_responses(sensor_id=0x24, raw=0x50),   # FAN1 speed 80 -> 80 RPM-units
            'raw 10 17 3 71 0 2': ' 02 64 00',      # max speed 100
            'raw 10 17 3 69 0 1': ' 01 01',         # direction byte: intake
        }
        self.use_ipmitool(responses)
        fan = fan_module.Fan(fantray_index=0, fan_index=0)
        self.assertTrue(fan.get_presence())
        self.assertEqual(fan.get_speed(), 80)
        self.assertEqual(fan.get_speed(), 80)       # is_under_speed / is_over_speed
        self.assertTrue(fan.get_status())
        self.assertEqual(fan.get_direction(), 'intake')
        self.assertTrue(fan.get_presence())
        self.assertEqual(len(self.ipmitool.calls), len(set(self.ipmitool.calls)))
        self.assertEqual(len(self.ipmitool.calls), 6)


if __name__ == '__main__':
    unittest.main()
