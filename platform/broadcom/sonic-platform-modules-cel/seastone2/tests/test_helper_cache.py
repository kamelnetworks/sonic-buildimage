"""The ipmitool reply cache in sonic_platform.helper (issue #163).

On the Seastone 2 every platform API attribute is an `ipmitool` subprocess
and one BMC round trip takes 0.3-0.8 s. thermalctld asks the same question
several times per cycle, so with ~130 round trips a cycle took 45-50 s,
over its 30 s budget. The helper now answers read-only ipmitool commands
from a short-lived per-process cache. These tests pin down what is cached,
for how long, and that writes are never cached and invalidate the cache.

They reuse the stubs and the fake BMC of test_ipmi_text.py and run without
hardware or SONiC packages.
"""

import subprocess
from unittest.mock import patch

import pytest

from test_ipmi_text import (CPLD_CMD, FAN_LED_CMD, FRU_PART_CMD, FRU_SERIAL_CMD,
                            PRESENCE_CMD, PSU_STATUS_CMD, SENSOR_READ_CMD,
                            SENSOR_THRESH_CMD, SPEED_CMD, SYS_LED_CMD, FakeIpmitool,
                            chassis_module, fan_module, helper, psu_module,
                            thermal_module)


class Clock:
    """A monotonic clock the tests move by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def bmc():
    fake = FakeIpmitool()
    fake.clock = Clock()
    helper.clear_cache()
    with patch.object(subprocess, 'getstatusoutput', fake), \
            patch.object(helper.time, 'monotonic', fake.clock):
        yield fake
    helper.clear_cache()


def api():
    return helper.APIHelper()


# ------------------------------------------------------------ the cache

def test_repeated_read_hits_the_bmc_once(bmc):
    bmc.responses[PRESENCE_CMD.format(0)] = ' 00'
    for _ in range(3):
        assert api().run_command(PRESENCE_CMD.format(0)) == (True, ' 00')
    assert bmc.calls == [PRESENCE_CMD.format(0)]


def test_cache_key_ignores_case_and_spacing(bmc):
    # thermal.py reads PSU 1's temperature as 0x2D 0x0D, psu.py as 0x2D 0x0d.
    bmc.responses['ipmitool raw 0x04 0x2D 0x0D'] = ' 1e c0 00 00'
    assert api().ipmi_raw('0x04', '0x2D 0x0D') == (True, ' 1e c0 00 00')
    assert api().ipmi_raw('0x04', '0x2D 0x0d') == (True, ' 1e c0 00 00')
    assert api().run_command('ipmitool  raw 0x04 0x2d 0x0d') == (True, ' 1e c0 00 00')
    assert len(bmc.calls) == 1


def test_reading_expires_after_the_read_ttl(bmc):
    bmc.responses[SPEED_CMD.format('0x81')] = ' 46 c0 00 00'
    assert api().run_command(SPEED_CMD.format('0x81')) == (True, ' 46 c0 00 00')
    bmc.responses[SPEED_CMD.format('0x81')] = ' 50 c0 00 00'
    bmc.clock.advance(helper.CACHE_TTL_READ - 1)
    assert api().run_command(SPEED_CMD.format('0x81')) == (True, ' 46 c0 00 00')
    bmc.clock.advance(2)
    assert api().run_command(SPEED_CMD.format('0x81')) == (True, ' 50 c0 00 00')
    assert len(bmc.calls) == 2


@pytest.mark.parametrize('cmd', (FRU_PART_CMD.format(5), "ipmitool fru print 3 | grep 'Product Name' ",
                                 SENSOR_THRESH_CMD.format('0x01')))
def test_fru_and_thresholds_are_static(bmc, cmd):
    bmc.responses[cmd] = ' something'
    assert api().run_command(cmd) == (True, ' something')
    bmc.clock.advance(helper.CACHE_TTL_READ + 1)
    assert api().run_command(cmd) == (True, ' something')
    assert len(bmc.calls) == 1
    bmc.clock.advance(helper.CACHE_TTL_STATIC)
    assert api().run_command(cmd) == (True, ' something')
    assert len(bmc.calls) == 2


def test_static_ttl_outlives_a_thermalctld_cycle():
    assert helper.CACHE_TTL_STATIC > 60 >= helper.CACHE_TTL_READ


def test_a_failure_is_retried_after_the_read_ttl(bmc):
    # No reply configured: the fake fails like a BMC that doesn't answer.
    assert api().run_command(FRU_PART_CMD.format(5)) == (False, '')
    assert api().run_command(FRU_PART_CMD.format(5)) == (False, '')
    assert len(bmc.calls) == 1
    bmc.clock.advance(helper.CACHE_TTL_READ + 1)
    bmc.responses[FRU_PART_CMD.format(5)] = ' Board Part Number     : R1241-F0001-01'
    assert api().run_command(FRU_PART_CMD.format(5)) == (True, ' Board Part Number     : R1241-F0001-01')
    assert len(bmc.calls) == 2


def test_writes_are_never_cached_and_drop_the_cache(bmc):
    fan = fan_module.Fan(1, 0)
    bmc.responses[FAN_LED_CMD.format(fan._fan_led_offset)] = ' 01'
    assert fan.get_status_led() == 'amber'
    assert fan.get_status_led() == 'amber'
    set_cmd = fan_module.IPMI_SET_STATUS_LED.format(fan._fan_led_offset, 2)
    bmc.responses[set_cmd] = ''
    assert fan.drawer_set_status_led('green') is True
    assert fan.drawer_set_status_led('green') is True
    bmc.responses[FAN_LED_CMD.format(fan._fan_led_offset)] = ' 02'
    assert fan.get_status_led() == 'green'
    assert bmc.calls == [FAN_LED_CMD.format(fan._fan_led_offset), set_cmd, set_cmd,
                         FAN_LED_CMD.format(fan._fan_led_offset)]


def test_system_led_and_threshold_writes_drop_the_cache(bmc):
    chassis = chassis_module.Chassis.__new__(chassis_module.Chassis)
    chassis._api_helper = api()
    bmc.responses[SYS_LED_CMD] = ' 10'
    assert chassis.get_status_led() == 'green'
    set_cmd = chassis_module.IPMI_SET_SYS_STATUS_LED.format('0x20')
    bmc.responses[set_cmd] = ''
    assert chassis.set_status_led('amber') is True
    bmc.responses[SYS_LED_CMD] = ' 20'
    assert chassis.get_status_led() == 'amber'

    bmc.responses[SENSOR_READ_CMD.format('0x01')] = ' 1c c0 00 00'
    bmc.responses[SENSOR_THRESH_CMD.format('0x01')] = ' 38 00 00 00 3a 3f 46'
    thermal = thermal_module.Thermal(0)
    assert thermal.get_high_threshold() == 58.0
    bmc.responses["ipmitool sensor thresh 'Base_Temp_U5' unc 60.0"] = ''
    assert thermal.set_high_threshold(60.0) is True
    bmc.responses[SENSOR_THRESH_CMD.format('0x01')] = ' 38 00 00 00 3c 3f 46'
    assert thermal.get_high_threshold() == 60.0


def test_unknown_ipmitool_commands_are_not_cached(bmc):
    cmd = "ipmitool mc info | grep 'Firmware Revision'"
    bmc.responses[cmd] = ' Firmware Revision         : 0.60'
    api().run_command(cmd)
    api().run_command(cmd)
    assert bmc.calls == [cmd, cmd]


def test_other_commands_pass_through_without_touching_the_cache(bmc):
    bmc.responses[PRESENCE_CMD.format(0)] = ' 00'
    bmc.responses['cat /sys/class/dmi/id/bios_version'] = 'v1'
    api().run_command(PRESENCE_CMD.format(0))
    api().run_command('cat /sys/class/dmi/id/bios_version')
    api().run_command('cat /sys/class/dmi/id/bios_version')
    api().run_command(PRESENCE_CMD.format(0))
    assert bmc.calls == [PRESENCE_CMD.format(0), 'cat /sys/class/dmi/id/bios_version',
                         'cat /sys/class/dmi/id/bios_version']


def test_cache_can_be_switched_off(bmc):
    bmc.responses[PRESENCE_CMD.format(0)] = ' 00'
    with patch.object(helper, 'CACHE_TTLS', ()):
        api().run_command(PRESENCE_CMD.format(0))
        api().run_command(PRESENCE_CMD.format(0))
    assert len(bmc.calls) == 2


def test_clear_cache(bmc):
    bmc.responses[PRESENCE_CMD.format(0)] = ' 00'
    api().run_command(PRESENCE_CMD.format(0))
    helper.clear_cache()
    api().run_command(PRESENCE_CMD.format(0))
    assert len(bmc.calls) == 2


# ------------------------------------- what thermalctld costs per cycle

def test_fan_refresh_is_one_round_trip_per_attribute(bmc):
    """thermalctld's _refresh_fan_status reads a fan's presence three times
    and its speed and target four times (get_status, is_under_speed,
    is_over_speed); each attribute must cost one BMC round trip."""
    fan = fan_module.Fan(0, 0)
    bmc.responses[PRESENCE_CMD.format(0)] = ' 00'
    bmc.responses[SPEED_CMD.format(fan._fan_sid_offset)] = ' 46 c0 00 00'
    bmc.responses[CPLD_CMD.format(fan._fan_speed_offset)] = ' 80'
    bmc.responses[CPLD_CMD.format(fan._fan_dir_offset)] = ' 08'
    assert fan.get_presence() is True
    assert fan.get_speed() == 45
    assert fan.get_target_speed() == 50
    assert fan.get_speed() == 45 and fan.get_target_speed() == 50   # is_under_speed
    assert fan.get_speed() == 45 and fan.get_target_speed() == 50   # is_over_speed
    assert fan.get_status() is True
    assert fan.get_direction() == 'intake'
    assert fan.get_presence() is True
    assert len(bmc.calls) == 4
    assert len(set(bmc.calls)) == 4


def test_thermal_refresh_is_two_round_trips(bmc):
    bmc.responses[SENSOR_READ_CMD.format('0x03')] = ' 25 c0 00 00'
    bmc.responses[SENSOR_THRESH_CMD.format('0x03')] = ' 38 00 00 00 4d 50 55'
    thermal = thermal_module.Thermal(2)      # reads the temperature in __init__
    assert thermal.get_presence() is True
    assert thermal.get_temperature() == 37.0
    assert thermal.get_high_threshold() == 77.0
    assert thermal.get_high_critical_threshold() == 80.0
    assert thermal.get_status() is True
    assert sorted(bmc.calls) == sorted([SENSOR_READ_CMD.format('0x03'), SENSOR_THRESH_CMD.format('0x03')])


def test_psu_status_is_one_round_trip(bmc):
    psu = psu_module.Psu(0)
    bmc.responses[PSU_STATUS_CMD.format('0x72')] = ' 00 c0 01 00'
    assert psu.get_presence() is True
    assert psu.get_status() is True
    assert psu.get_powergood_status() is True
    assert psu.get_status_led() == 'green'
    assert bmc.calls == [PSU_STATUS_CMD.format('0x72')]


def test_drawer_fru_is_read_once_per_static_ttl(bmc):
    fan = fan_module.Fan(1, 0)
    bmc.responses[FRU_PART_CMD.format(fan._fan_fru_offset)] = ' Board Part Number     : R1241-F0001-01'
    bmc.responses[FRU_SERIAL_CMD.format(fan._fan_fru_offset)] = ' Board Serial          : R1241F0001012A0021'
    for _ in range(3):      # three thermalctld cycles
        assert fan.get_model() == 'R1241-F0001-01'
        assert fan.get_serial() == 'R1241F0001012A0021'
        bmc.clock.advance(60)
    assert len(bmc.calls) == 2
