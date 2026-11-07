"""Seastone 2 platform API against textual ipmitool output (issues #137, #143).

`subprocess.getstatusoutput` hands back ipmitool's stdout with only the
final newline removed, so `ipmitool raw ...` replies keep their leading
space: ``' 00'``, not ``'00'``.  These tests feed such text through the
real `APIHelper` and the real consumers (Fan, FanDrawer, Psu, Thermal,
Chassis, Component) and check that every comparison copes with it.  The
#143 cases make ipmitool fail (or answer too little) and check that the
readers return 'N/A'/False instead of raising into thermalctld and psud.

They need no hardware and no SONiC packages: `sonic_py_common` and the
`sonic_platform_base` base classes are stubbed with the attributes the
Seastone 2 code uses.
"""

import importlib
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

PLATFORM_DIR = Path(__file__).resolve().parents[1] / 'sonic_platform'


class _DeviceBase:
    STATUS_LED_COLOR_GREEN = 'green'
    STATUS_LED_COLOR_AMBER = 'amber'
    STATUS_LED_COLOR_RED = 'red'
    STATUS_LED_COLOR_OFF = 'off'


class _FanBase(_DeviceBase):
    FAN_DIRECTION_INTAKE = 'intake'
    FAN_DIRECTION_EXHAUST = 'exhaust'
    FAN_DIRECTION_NOT_APPLICABLE = 'N/A'


class _PsuBase(_DeviceBase):
    def __init__(self):
        self._fan_list = []
        self._thermal_list = []

    def get_fan(self, index):
        return self._fan_list[index]


class _FanDrawerBase(_DeviceBase):
    def __init__(self):
        self._fan_list = []


class _ThermalBase(_DeviceBase):
    pass


class _ChassisBase(_DeviceBase):
    REBOOT_CAUSE_POWER_LOSS = 'Power Loss'
    REBOOT_CAUSE_THERMAL_OVERLOAD_CPU = 'Thermal Overload: CPU'
    REBOOT_CAUSE_THERMAL_OVERLOAD_ASIC = 'Thermal Overload: ASIC'
    REBOOT_CAUSE_WATCHDOG = 'Watchdog'
    REBOOT_CAUSE_HARDWARE_OTHER = 'Hardware - Other'
    REBOOT_CAUSE_NON_HARDWARE = 'Non-Hardware'

    def __init__(self):
        for name in ('component', 'module', 'fan', 'fan_drawer', 'psu',
                     'thermal', 'sfp'):
            setattr(self, '_{}_list'.format(name), [])


class _SfpBase(_DeviceBase):
    pass


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    return mod


def _install_stubs():
    device_info = _module('sonic_py_common.device_info')
    device_info.get_platform_and_hwsku = lambda: ('x86_64-cel_seastone_2-r0', 'Seastone_2')
    stubs = {
        'sonic_py_common': _module('sonic_py_common', device_info=device_info),
        'sonic_py_common.device_info': device_info,
        'sonic_platform_base': _module('sonic_platform_base'),
        'sonic_platform_base.device_base': _module('sonic_platform_base.device_base', DeviceBase=_DeviceBase),
        'sonic_platform_base.fan_base': _module('sonic_platform_base.fan_base', FanBase=_FanBase),
        'sonic_platform_base.psu_base': _module('sonic_platform_base.psu_base', PsuBase=_PsuBase),
        'sonic_platform_base.fan_drawer_base': _module('sonic_platform_base.fan_drawer_base', FanDrawerBase=_FanDrawerBase),
        'sonic_platform_base.thermal_base': _module('sonic_platform_base.thermal_base', ThermalBase=_ThermalBase),
        'sonic_platform_base.chassis_base': _module('sonic_platform_base.chassis_base', ChassisBase=_ChassisBase),
        'sonic_platform_base.sfp_base': _module('sonic_platform_base.sfp_base', SfpBase=_SfpBase),
    }
    # A package shell for `sonic_platform` whose __path__ points at the
    # Seastone 2 sources, so both `from .helper import APIHelper` and
    # `from sonic_platform.fan import Fan` resolve to this checkout without
    # executing sonic_platform/__init__.py (which imports the whole tree).
    pkg = _module('sonic_platform')
    pkg.__path__ = [str(PLATFORM_DIR)]
    stubs['sonic_platform'] = pkg
    for name in list(sys.modules):
        if name == 'sonic_platform' or name.startswith('sonic_platform.'):
            del sys.modules[name]
    sys.modules.update(stubs)


_install_stubs()
helper = importlib.import_module('sonic_platform.helper')
fan_module = importlib.import_module('sonic_platform.fan')
fan_drawer_module = importlib.import_module('sonic_platform.fan_drawer')
psu_module = importlib.import_module('sonic_platform.psu')
thermal_module = importlib.import_module('sonic_platform.thermal')
chassis_module = importlib.import_module('sonic_platform.chassis')
component_module = importlib.import_module('sonic_platform.component')


class FakeIpmitool:
    """Stand-in for subprocess.getstatusoutput.

    `responses` maps a full command line to the text ipmitool prints for
    it, exactly as getstatusoutput returns it (leading space kept, final
    newline dropped).  Unknown commands fail like a missing BMC would.
    """

    def __init__(self):
        self.responses = {}
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd in self.responses:
            return 0, self.responses[cmd]
        return 1, 'Unable to send RAW command (channel=0x0 netfn=0x3a lun=0x0 cmd=0x3 rsp=0xcc): Invalid data field in request'


@pytest.fixture
def ipmi():
    fake = FakeIpmitool()
    with patch.object(subprocess, 'getstatusoutput', fake):
        yield fake


PRESENCE_CMD = 'ipmitool raw 0x3a 0x03 0x03 {}'
SPEED_CMD = 'ipmitool raw 0x04 0x2d {}'
CPLD_CMD = 'ipmitool raw 0x3a 0x0c 0x00 0x02 {}'
FAN_LED_CMD = 'ipmitool raw 0x3a 0x0b {}'
FRU_PART_CMD = "ipmitool fru list {} | grep 'Board Part Number'"
FRU_SERIAL_CMD = "ipmitool fru list {} | grep 'Board Serial'"
PSU_MODEL_CMD = "ipmitool fru list {} | grep 'Product Name'"
PSU_STATUS_CMD = 'ipmitool raw 0x04 0x2D {}'
SENSOR_READ_CMD = 'ipmitool raw 0x04 0x2D {}'
SENSOR_THRESH_CMD = 'ipmitool raw 0x04 0x27 {}'
SYS_LED_CMD = 'ipmitool raw 0x3A 0x0C 0x00 0x02 0x62'


# --------------------------------------------------------------- helper

def test_run_command_keeps_ipmitool_leading_space(ipmi):
    """The premise of #137: the helper hands back ' 00', not '00'."""
    ipmi.responses[PRESENCE_CMD.format(0)] = ' 00'
    assert helper.APIHelper().run_command(PRESENCE_CMD.format(0)) == (True, ' 00')
    assert helper.APIHelper().ipmi_raw('0x3a', '0x03 0x03 0') == (True, ' 00')


def test_run_command_failure_is_false_and_empty(ipmi):
    assert helper.APIHelper().run_command('ipmitool raw 0x3a 0x03 0x03 9') == (False, '')


# ------------------------------------------------------------------ fan

@pytest.mark.parametrize('tray', range(fan_module.NUM_FAN_TRAYS))
@pytest.mark.parametrize('fan_index', range(fan_module.NUM_FANS_PER_TRAY))
def test_chassis_fan_present(ipmi, tray, fan_index):
    ipmi.responses[PRESENCE_CMD.format(tray)] = ' 00'
    fan = fan_module.Fan(tray, fan_index)
    assert fan.get_presence() is True
    assert ipmi.calls == [PRESENCE_CMD.format(tray)]


@pytest.mark.parametrize('psu_index', (0, 1))
def test_psu_fan_present(ipmi, psu_index):
    # PSU fan presence uses the slots after the four fan trays.
    ipmi.responses[PRESENCE_CMD.format(fan_module.NUM_FAN_TRAYS + psu_index)] = ' 00'
    psu = psu_module.Psu(psu_index)
    assert psu.get_fan(0).get_presence() is True
    assert ipmi.calls == [PRESENCE_CMD.format(fan_module.NUM_FAN_TRAYS + psu_index)]


@pytest.mark.parametrize('reply', (' 01', '01', ' 00 01', ''))
def test_fan_absent_or_odd_reply(ipmi, reply):
    ipmi.responses[PRESENCE_CMD.format(0)] = reply
    assert fan_module.Fan(0, 0).get_presence() is False


def test_fan_absent_when_ipmitool_fails(ipmi):
    assert fan_module.Fan(0, 0).get_presence() is False


def test_fan_drawer_presence_follows_its_fans(ipmi):
    ipmi.responses[PRESENCE_CMD.format(2)] = ' 00'
    fans = [fan_module.Fan(2, i) for i in range(fan_module.NUM_FANS_PER_TRAY)]
    drawer = fan_drawer_module.FanDrawer(2, fans)
    assert drawer.get_presence() is True
    assert drawer.get_name() == 'Drawer 3'


@pytest.mark.parametrize('reply, direction', ((' 00', 'exhaust'), (' 08', 'intake'),
                                              (' 04', 'N/A'), (' 0c', 'N/A')))
def test_fan_direction(ipmi, reply, direction):
    fan = fan_module.Fan(0, 0)
    ipmi.responses[CPLD_CMD.format(fan._fan_dir_offset)] = reply
    assert fan.get_direction() == direction


@pytest.mark.parametrize('reply, direction', ((' Product Name          : DPS-550AB-39 B', 'intake'),
                                              (' Product Name          : DPS-550AB-39 A', 'exhaust'),
                                              (' Product Name          : DPS-550AB-39 B ', 'intake')))
def test_psu_fan_direction_from_fru_model(ipmi, reply, direction):
    psu_fan = psu_module.Psu(0).get_fan(0)
    ipmi.responses[PSU_MODEL_CMD.format(fan_module.PSU_FRU_BASE)] = reply
    assert psu_fan.get_direction() == direction


def test_fan_speed_percent(ipmi):
    # 0x46 * 150 = 10500 RPM, the front fan reading from the #137 report;
    # 10500 / 23000 (front max) = 45 %.
    front = fan_module.Fan(0, 0)
    rear = fan_module.Fan(0, 1)
    ipmi.responses[SPEED_CMD.format(front._fan_sid_offset)] = ' 46 c0 00 00'
    ipmi.responses[SPEED_CMD.format(rear._fan_sid_offset)] = ' 3c c0 00 00'
    assert front.get_speed() == 45
    assert rear.get_speed() == int(0x3c * 150 / fan_module.MAX_RPM_REAR * 100)
    ipmi.responses[PRESENCE_CMD.format(0)] = ' 00'
    assert front.get_status() is True


def test_psu_fan_speed_percent(ipmi):
    psu_fan = psu_module.Psu(1).get_fan(0)
    ipmi.responses[SPEED_CMD.format(psu_fan._fan_sid_offset)] = ' 64 c0 00 00'
    assert psu_fan.get_speed() == int(0x64 * 100 / fan_module.MAX_RPM_PSU * 100)
    assert psu_fan.get_target_speed() == psu_fan.get_speed()


@pytest.mark.parametrize('reply, percent', ((' ff', 100), (' 80', 50), (' 00', 0)))
def test_fan_target_speed(ipmi, reply, percent):
    fan = fan_module.Fan(1, 0)
    ipmi.responses[CPLD_CMD.format(fan._fan_speed_offset)] = reply
    assert fan.get_target_speed() == percent


@pytest.mark.parametrize('reply, color', ((' 00', 'off'), (' 01', 'amber'), (' 02', 'green'),
                                          (' 03', 'N/A'), (' ff', 'N/A')))
def test_fan_status_led(ipmi, reply, color):
    # #143: an unknown LED value must be 'N/A', not the command's True.
    fan = fan_module.Fan(3, 1)
    ipmi.responses[FAN_LED_CMD.format(fan._fan_led_offset)] = reply
    assert fan.get_status_led() == color


def test_fan_status_led_when_ipmitool_fails(ipmi):
    assert fan_module.Fan(3, 1).get_status_led() == 'N/A'
    assert psu_module.Psu(0).get_fan(0).get_status_led() == 'N/A'


def test_fan_model_and_serial(ipmi):
    fan = fan_module.Fan(1, 0)
    ipmi.responses[FRU_PART_CMD.format(fan._fan_fru_offset)] = ' Board Part Number     : R1241-F0001-01'
    ipmi.responses[FRU_SERIAL_CMD.format(fan._fan_fru_offset)] = ' Board Serial          : R1241F0001012A0021'
    assert fan.get_model() == 'R1241-F0001-01'
    assert fan.get_serial() == 'R1241F0001012A0021'
    assert fan.get_name() == 'Fan 2 Front'


# ------------------------------------------------------------------ psu

@pytest.mark.parametrize('psu_index, status_key', ((0, '0x72'), (1, '0x73')))
def test_psu_presence_and_status(ipmi, psu_index, status_key):
    psu = psu_module.Psu(psu_index)
    ipmi.responses[PSU_STATUS_CMD.format(status_key)] = ' 00 c0 01 00'
    assert psu.get_presence() is True
    assert psu.get_status() is True
    assert psu.get_powergood_status() is True
    ipmi.responses[PSU_STATUS_CMD.format(status_key)] = ' 00 c0 08 00'
    assert psu.get_presence() is False
    assert psu.get_status() is False


def test_psu_readings(ipmi):
    psu = psu_module.Psu(0)
    ipmi.responses[SENSOR_READ_CMD.format('0x0f')] = ' 78 c0 00 00'   # 0x78 / 10 = 12.0 V
    ipmi.responses[SENSOR_READ_CMD.format('0x10')] = ' 0a c0 00 00'   # 0x0a * 3 / 10 = 3.0 A
    ipmi.responses[SENSOR_READ_CMD.format('0x11')] = ' 0c c0 00 00'   # 0x0c * 5 = 60 W
    ipmi.responses[SENSOR_READ_CMD.format('0x0d')] = ' 1e c0 00 00'   # 30 C
    assert psu.get_voltage() == 12.0
    assert psu.get_current() == 3.0
    assert psu.get_power() == 60
    assert psu.get_temperature() == 30.0


def test_psu_readings_when_ipmitool_fails(ipmi):
    # #143: Psu indexed output.split() before checking the command status,
    # so a failing ipmitool raised IndexError into psud.
    psu = psu_module.Psu(1)
    assert psu.get_voltage() == 'N/A'
    assert psu.get_current() == 'N/A'
    assert psu.get_power() == 'N/A'
    assert psu.get_temperature() == 'N/A'
    assert psu.get_presence() is False
    assert psu.get_status() is False
    assert psu.get_powergood_status() is False
    assert psu.get_status_led() == 'N/A'
    assert psu.get_model() == 'N/A'
    assert psu.get_serial() == 'N/A'


@pytest.mark.parametrize('reply', ('', ' 78', ' 00 c0'))
def test_psu_short_reply_is_not_available(ipmi, reply):
    psu = psu_module.Psu(0)
    for key in ('0x0f', '0x10', '0x11', '0x0d', '0x72'):
        ipmi.responses[SENSOR_READ_CMD.format(key)] = reply
    if len(reply.split()) < 1:
        assert psu.get_voltage() == 'N/A'
        assert psu.get_current() == 'N/A'
        assert psu.get_power() == 'N/A'
        assert psu.get_temperature() == 'N/A'
    assert psu.get_presence() is False
    assert psu.get_status() is False


def test_psu_model_and_serial_from_fru(ipmi):
    psu = psu_module.Psu(0)
    ipmi.responses[PSU_STATUS_CMD.format('0x72')] = ' 00 c0 01 00'
    ipmi.responses["ipmitool fru print 3 | grep 'Product Name' "] = ' Product Name          : DPS-550AB-39 B'
    ipmi.responses["ipmitool fru print 3 | grep 'Product Serial' "] = ' Product Serial        : TH8A1234567'
    assert psu.get_model() == 'DPS-550AB-39 B'
    assert psu.get_serial() == 'TH8A1234567'


# -------------------------------------------------------------- thermal

def test_thermal_readings(ipmi):
    # Thermal.__init__ takes a first reading, so answer before constructing.
    ipmi.responses[SENSOR_READ_CMD.format('0x01')] = ' 1e c0 00 00'
    ipmi.responses[SENSOR_THRESH_CMD.format('0x01')] = ' 3f 00 00 00 46 50 00'
    thermal = thermal_module.Thermal(0)
    assert thermal.get_temperature() == 30.0
    assert thermal.get_high_threshold() == 70.0
    assert thermal.get_high_critical_threshold() == 80.0


def test_thermal_defaults_when_ipmitool_fails(ipmi):
    thermal = thermal_module.Thermal(0)
    assert thermal.get_temperature() == 'N/A'
    assert thermal.get_high_threshold() == 'N/A'
    assert thermal.get_high_critical_threshold() == 'N/A'
    assert thermal.get_minimum_recorded() == 'N/A'
    assert thermal.get_maximum_recorded() == 'N/A'
    assert thermal.get_presence() is False
    assert thermal.get_status() is False


def test_thermal_recovers_after_ipmitool_fails_at_init(ipmi):
    # #143: the first reading is stored as min/max; when it was 'N/A' every
    # later successful get_temperature() raised TypeError (float > str) in
    # thermalctld.
    thermal = thermal_module.Thermal(3)
    assert thermal.get_minimum_recorded() == 'N/A'
    ipmi.responses[SENSOR_READ_CMD.format('0x04')] = ' 28 c0 00 00'   # 40 C
    assert thermal.get_temperature() == 40.0
    assert thermal.get_presence() is True
    assert thermal.get_minimum_recorded() == 40.0
    assert thermal.get_maximum_recorded() == 40.0
    ipmi.responses[SENSOR_READ_CMD.format('0x04')] = ' 2d c0 00 00'   # 45 C
    assert thermal.get_temperature() == 45.0
    ipmi.responses[SENSOR_READ_CMD.format('0x04')] = ' 23 c0 00 00'   # 35 C
    assert thermal.get_temperature() == 35.0
    assert thermal.get_minimum_recorded() == 35.0
    assert thermal.get_maximum_recorded() == 45.0


def test_thermal_keeps_min_max_across_a_failed_reading(ipmi):
    ipmi.responses[SENSOR_READ_CMD.format('0x01')] = ' 1e c0 00 00'
    thermal = thermal_module.Thermal(0)
    assert thermal.get_temperature() == 30.0
    del ipmi.responses[SENSOR_READ_CMD.format('0x01')]
    assert thermal.get_temperature() == 'N/A'
    assert thermal.get_presence() is False
    assert thermal.get_minimum_recorded() == 30.0
    assert thermal.get_maximum_recorded() == 30.0


# -------------------------------------------------------------- chassis

@pytest.mark.parametrize('reply, color', ((' 10', 'green'), (' 11', 'green_blink_1hz'),
                                          (' 20', 'amber'), (' 33', 'off'), (' 03', 'N/A')))
def test_chassis_status_led(ipmi, reply, color):
    chassis = chassis_module.Chassis.__new__(chassis_module.Chassis)
    chassis._api_helper = helper.APIHelper()
    ipmi.responses[SYS_LED_CMD] = reply
    assert chassis.get_status_led() == color


# ------------------------------------------------------------ component

def test_component_bmc_version(ipmi):
    bmc = component_module.Component(2)
    assert bmc.get_name() == 'BMC'
    ipmi.responses["ipmitool mc info | grep 'Firmware Revision'"] = 'Firmware Revision         : 1.09'
    assert bmc.get_firmware_version() == '1.09'
