"""Hardware-free regressions for kamelnetworks/sonic-buildimage#101.

Every process that builds a Chassis (any `show` command that goes through
is_rj45_port, `show platform summary`, decode-syseeprom, system-health, ...)
builds the 34 Sfp objects with it. Their constructors must not write sysfs,
spawn processes or even read transceiver EEPROMs. Re-binding the optoe
driver to the inserted media type belongs to the change-event path, which
only xcvrd drives.
"""
import importlib.util
import io
from pathlib import Path
import re
import sys
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1] / 'sonic_platform'
NUM_SFP = 34
SFP_I2C_START = 12
QSFP28_ID = 0x11
QSFP_DD_ID = 0x18
SFP_ID = 0x03


class FakeSysfs:
    """sysfs as the platform init script leaves it: optoe1 on the 32 QSFP
    buses, optoe2 on the two SFP buses, every cage populated with its native
    media. Reads are served, writes are recorded, nothing else exists."""

    def __init__(self):
        self.reads = []
        self.writes = []
        self.eeprom_reads = []
        self.present = set(range(NUM_SFP))
        self.module_ids = {i: QSFP28_ID if i < 32 else SFP_ID for i in range(NUM_SFP)}
        self.drivers = {i: 'optoe1' if i < 32 else 'optoe2' for i in range(NUM_SFP)}
        # port_name as the init script leaves it (qsfpN / sfpN).
        self.port_names = {i: 'qsfp{}'.format(i + 1) if i < 32 else 'sfp{}'.format(i - 31)
                           for i in range(NUM_SFP)}
        self.os = types.SimpleNamespace(
            path=types.SimpleNamespace(isfile=self.isfile),
            open=self.os_open, fdopen=self.os_fdopen, close=lambda fd: None,
            O_RDONLY=0, O_RDWR=2)

    def content(self, path):
        for index in range(NUM_SFP):
            if index < 32:
                presence = '/sys/class/SFF/QSFP{}/qsfp_modprs'.format(index + 1)
            else:
                presence = '/sys/devices/platform/fpga-xcvr/SFP{}/sfp_modabs'.format(index - 31)
            if path == presence:
                return '0\n' if index in self.present else '1\n'
            if path == '/sys/bus/i2c/devices/{}-0050/name'.format(index + SFP_I2C_START):
                return self.drivers[index] + '\n'
            if path == '/sys/bus/i2c/devices/{}-0050/port_name'.format(index + SFP_I2C_START):
                return self.port_names[index] + '\n'
        raise FileNotFoundError(path)

    def rebind(self, command):
        """Apply an 'echo ... > .../new_device' shell command the way the
        kernel would: a fresh optoe client with the driver default port_name."""
        m = re.match(r'echo (optoe\d) 0x50 >/sys/bus/i2c/devices/i2c-(\d+)/new_device$', command)
        if m:
            index = int(m.group(2)) - SFP_I2C_START
            self.drivers[index] = m.group(1)
            self.port_names[index] = 'unitialized'  # sic, optoe.c

    def isfile(self, path):
        try:
            self.content(path)
        except FileNotFoundError:
            return False
        return True

    def open(self, path, mode='r', **kwargs):
        if any(flag in mode for flag in 'wa+x'):
            self.writes.append(path)
            return self.Writer(self, path)
        self.reads.append(path)
        return io.StringIO(self.content(path))

    def os_open(self, path, flags, *args):
        if flags & 2:
            self.writes.append(path)
        self.reads.append(path)
        return path

    def os_fdopen(self, path, *args, **kwargs):
        return io.StringIO(self.content(path))

    def module_id(self, sfp):
        self.eeprom_reads.append(sfp.index)
        return self.module_ids[sfp.index] if sfp.index in self.present else None

    class Writer(io.StringIO):
        """A sysfs attribute opened for writing; stores port_name on close."""

        def __init__(self, fs, path):
            super().__init__()
            self.fs, self.path = fs, path

        def close(self):
            for index in range(NUM_SFP):
                if self.path == '/sys/bus/i2c/devices/{}-0050/port_name'.format(index + SFP_I2C_START):
                    self.fs.port_names[index] = self.getvalue().strip()
            super().close()


class ChassisBase:
    def __init__(self):
        for name in ('component', 'module', 'fan', 'fan_drawer', 'psu', 'thermal', 'sfp'):
            setattr(self, '_{}_list'.format(name), [])
        self._eeprom = None


class SfpOptoeBase:
    SFP_STATUS_OK = 'OK'
    SFP_STATUS_UNPLUGGED = 'Unplugged'

    def __init__(self, bank=0):
        self._xcvr_api = None

    def read_eeprom(self, offset, num_bytes):
        raise AssertionError('raw EEPROM access must go through the fake sysfs')


class APIHelper:
    platform = 'x86_64-cel_silverstone-x-r0'

    def is_host(self):
        return False

    def read_one_line_file(self, path):
        return None


class FanDrawer:
    def __init__(self, index):
        self._fan_list = []


def stub_module(name, **attrs):
    module = types.ModuleType(name)
    module.__path__ = []  # a package, so lazy 'from sonic_platform.x import' resolves
    module.__dict__.update(attrs)
    return module


def load_platform(fs):
    """Load the real sfp.py and chassis.py over a fake sysfs. Everything the
    chassis builds besides SFPs is stubbed; those parts have their own tests."""
    class ModuleEepromLowerMemoryInfo:
        def __init__(self, reader):
            self.sfp = reader.__self__

        def get_id(self):
            return fs.module_id(self.sfp)

    stubs = {
        'sonic_platform_base': stub_module('sonic_platform_base'),
        'sonic_platform_base.chassis_base': stub_module('sonic_platform_base.chassis_base', ChassisBase=ChassisBase),
        'sonic_platform_base.sonic_xcvr': stub_module('sonic_platform_base.sonic_xcvr'),
        'sonic_platform_base.sonic_xcvr.sfp_optoe_base': stub_module(
            'sonic_platform_base.sonic_xcvr.sfp_optoe_base', SfpOptoeBase=SfpOptoeBase),
        'sonic_platform_base.sonic_xcvr.eeprom_rw': stub_module(
            'sonic_platform_base.sonic_xcvr.eeprom_rw', ModuleEepromLowerMemoryInfo=ModuleEepromLowerMemoryInfo),
        'sonic_platform': stub_module('sonic_platform'),
        'sonic_platform.helper': stub_module('sonic_platform.helper', APIHelper=APIHelper),
        'sonic_platform.fan': stub_module('sonic_platform.fan', Fan=Mock),
        'sonic_platform.fan_drawer': stub_module('sonic_platform.fan_drawer', FanDrawer=FanDrawer),
        'sonic_platform.psu': stub_module('sonic_platform.psu', Psu=Mock),
        'sonic_platform.thermal': stub_module('sonic_platform.thermal', Thermal=Mock),
        'sonic_platform.eeprom': stub_module('sonic_platform.eeprom', Tlv=Mock),
        'sonic_platform.component': stub_module('sonic_platform.component', Component=Mock),
    }
    modules = {}
    for name in ('sfp', 'chassis'):
        fullname = 'sonic_platform.' + name
        spec = importlib.util.spec_from_file_location(fullname, ROOT / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        stubs[fullname] = module
        modules[name] = module
        with patch.dict(sys.modules, stubs):
            spec.loader.exec_module(module)
    sfp, chassis = modules['sfp'], modules['chassis']
    sfp.open = fs.open
    sfp.os = fs.os
    sfp.subprocess = types.SimpleNamespace(Popen=Mock(side_effect=lambda cmd, **kw: fs.rebind(cmd)), PIPE=-1)
    sfp.time = types.SimpleNamespace(sleep=Mock())
    chassis.time = types.SimpleNamespace(time=time.time, sleep=Mock())
    return stubs, sfp, chassis


class SfpMediaInitTest(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSysfs()
        # A QSA adapter with an SFP+ behind it sits in QSFP port 1 (index 0),
        # the one case where the bound optoe class does not match the media.
        self.fs.module_ids[0] = SFP_ID
        stubs, self.sfp, self.chassis = load_platform(self.fs)
        modules = patch.dict(sys.modules, stubs)
        modules.start()
        self.addCleanup(modules.stop)
        self.popen = self.sfp.subprocess.Popen

    def shell_commands(self):
        return [call.args[0] for call in self.popen.call_args_list]

    def test_chassis_init_leaves_sysfs_alone(self):
        self.chassis.Chassis()

        self.assertEqual(self.shell_commands(), [])
        self.assertEqual(self.fs.writes, [])
        self.assertEqual(self.fs.eeprom_reads, [])

    def test_sfp_init_does_no_io(self):
        self.sfp.Sfp(0)

        self.assertEqual(self.shell_commands(), [])
        self.assertEqual(self.fs.reads, [])
        self.assertEqual(self.fs.writes, [])
        self.assertEqual(self.fs.eeprom_reads, [])

    def test_first_change_event_reports_present_ports_and_binds_media(self):
        self.fs.present -= {7, 33}
        chassis = self.chassis.Chassis()

        status, events = chassis.get_change_event(1000)

        self.assertTrue(status)
        self.assertEqual(events['sfp'], {i: '1' for i in self.fs.present})
        self.assertEqual(sorted(self.fs.eeprom_reads), sorted(self.fs.present))
        self.assertEqual(self.shell_commands(), [
            'echo 0x50 >/sys/bus/i2c/devices/i2c-12/delete_device',
            'echo optoe2 0x50 >/sys/bus/i2c/devices/i2c-12/new_device',
        ])
        self.assertEqual(chassis.get_sfp(0).sfp_type, 'SFP')
        self.assertEqual(chassis.get_sfp(1).sfp_type, 'QSFP')

    def test_later_events_bind_media_only_on_insertion(self):
        chassis = self.chassis.Chassis()
        chassis.get_change_event(1000)
        self.popen.reset_mock()
        self.fs.eeprom_reads.clear()

        self.fs.present.discard(5)
        status, events = chassis.get_change_event(1000)
        self.assertEqual((status, events['sfp']), (True, {5: '0'}))
        self.assertEqual(self.fs.eeprom_reads, [])
        self.assertEqual(self.shell_commands(), [])

        self.fs.present.add(5)
        self.fs.module_ids[5] = SFP_ID
        status, events = chassis.get_change_event(1000)
        self.assertEqual((status, events['sfp']), (True, {5: '1'}))
        self.assertEqual(self.fs.eeprom_reads, [5])
        self.assertEqual(self.shell_commands(), [
            'echo 0x50 >/sys/bus/i2c/devices/i2c-17/delete_device',
            'echo optoe2 0x50 >/sys/bus/i2c/devices/i2c-17/new_device',
        ])

    # kamelnetworks/sonic-buildimage#149: new_device creates the optoe client
    # with port_name 'unitialized' (sic); the rebind must put the init
    # script's name back.
    def test_rebind_restores_port_name(self):
        self.fs.module_ids[4] = QSFP_DD_ID   # QSFP5, a QSFP-DD DAC
        self.fs.module_ids[33] = QSFP28_ID   # SFP2, odd but exercises the sfpN branch
        chassis = self.chassis.Chassis()

        chassis.get_change_event(1000)

        self.assertEqual(self.fs.drivers[4], 'optoe3')
        self.assertEqual(self.fs.drivers[33], 'optoe1')
        self.assertEqual(self.fs.port_names, {
            i: 'qsfp{}'.format(i + 1) if i < 32 else 'sfp{}'.format(i - 31) for i in range(NUM_SFP)})
        self.assertEqual(self.fs.writes, [
            '/sys/bus/i2c/devices/12-0050/port_name',
            '/sys/bus/i2c/devices/16-0050/port_name',
            '/sys/bus/i2c/devices/45-0050/port_name',
        ])

    def test_no_rebind_leaves_sysfs_alone(self):
        # Native media everywhere: nothing is rebound and port_name is not touched.
        self.fs.module_ids[0] = QSFP28_ID
        chassis = self.chassis.Chassis()

        chassis.get_change_event(1000)

        self.assertEqual(self.shell_commands(), [])
        self.assertEqual(self.fs.writes, [])

    def test_port_name_write_failure_is_reported_not_raised(self):
        sfp = self.sfp.Sfp(4)
        sfp.sfp_type = 'QSFP_DD'

        def deny(path, mode='r', **kwargs):
            if path.endswith('/port_name'):
                raise PermissionError(13, 'Permission denied', path)
            return self.fs.open(path, mode, **kwargs)
        self.sfp.open = deny

        with patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertFalse(sfp._reinit_sfp_driver())
        self.assertEqual(self.fs.drivers[4], 'optoe3')
        self.assertIn('port_name', out.getvalue())


if __name__ == '__main__':
    unittest.main()
