"""Hardware-free checks for xcvrd sff_mgr on Silverstone-X, S5232F-ON and Seastone 2.

Runs the pinned xcvrd ``SffManagerTask`` (src/sonic-platform-daemons) against
each platform's real ``Sfp`` class with sysfs, the Dell PCI BAR and the module
EEPROM replaced by temp files, and checks the pmon plumbing that turns
``enable_xcvrd_sff_mgr`` into ``xcvrd --enable_sff_mgr``.

Needs sonic-platform-common, sonic-py-common and sonic-xcvrd importable
(the src/ submodules are added to sys.path when they are checked out).
swsscommon is stubbed when it is not installed.
"""
import contextlib
import importlib
import io
import json
import struct
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
for sub in ('src/sonic-platform-common', 'src/sonic-py-common',
            'src/sonic-platform-daemons/sonic-xcvrd'):
    path = ROOT / sub
    if path.is_dir() and str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    import swsscommon.swsscommon  # noqa: F401
except ImportError:
    _stub = types.ModuleType('swsscommon')
    _stub.swsscommon = MagicMock(name='swsscommon')
    sys.modules['swsscommon'] = _stub
    sys.modules['swsscommon.swsscommon'] = _stub.swsscommon

try:
    import sonic_platform_base.sonic_sfp.sfputilhelper  # noqa: F401
except ImportError:
    # Only used by Seastone 2's get_name(); it needs sonic-config-engine.
    _stub = types.ModuleType('sonic_platform_base.sonic_sfp.sfputilhelper')
    _stub.SfpUtilHelper = type('SfpUtilHelper', (), {})
    sys.modules[_stub.__name__] = _stub

from sonic_platform_base.sonic_xcvr.api.public.cmis import CmisApi  # noqa: E402
from sonic_platform_base.sonic_xcvr.api.public.sff8636 import Sff8636Api  # noqa: E402
from xcvrd.sff_mgr import SffManagerTask  # noqa: E402
from xcvrd.xcvrd_utilities.port_event_helper import PortChangeEvent  # noqa: E402

DEVICE = ROOT / 'device'
SILVERSTONE_X = DEVICE / 'celestica/x86_64-cel_silverstone-x-r0'
S5232F = DEVICE / 'dell/x86_64-dellemc_s5232f_c3538-r0'
SEASTONE_2 = DEVICE / 'celestica/x86_64-cel_seastone_2-r0'
SFP_MODULES = {
    'silverstone_x': SILVERSTONE_X / 'sonic_platform',
    's5232f': ROOT / 'platform/broadcom/sonic-platform-modules-dell/s5232f/sonic_platform',
    'seastone2': ROOT / 'platform/broadcom/sonic-platform-modules-cel/seastone2/sonic_platform',
}

# SFF-8636 byte 93 (power control) bits.
POWER_OVERRIDE = 1 << 0
POWER_SET = 1 << 1
HIGH_POWER_CLASS_5_TO_7 = 1 << 2
HIGH_POWER_CLASS_8 = 1 << 3
TX_DISABLE_ALL = 0x0F
# SFF-8636 byte 129 extended identifier for each power class.
EXT_ID_BY_POWER_CLASS = {1: 0x00, 4: 0xC0, 5: 0xC1, 6: 0xC2, 7: 0xC3}


def load_sfp_module(name, stubs=None):
    """Import <platform>/sonic_platform/sfp.py as its own package."""
    pkg_name = 'sff_mgr_test_' + name
    package = types.ModuleType(pkg_name)
    package.__path__ = [str(SFP_MODULES[name])]
    sys.modules[pkg_name] = package
    for stub_name, module in (stubs or {}).items():
        sys.modules['{}.{}'.format(pkg_name, stub_name)] = module
    return importlib.import_module(pkg_name + '.sfp')


class FakeAPIHelper:
    """Seastone 2 helper.APIHelper without device_info / DB access."""
    platform = 'x86_64-cel_seastone_2-r0'
    hwsku = 'Seastone_2'

    def read_one_line_file(self, file_path):
        try:
            with open(file_path, 'r') as fd:
                return fd.readline().strip()
        except IOError:
            return None

    def is_host(self):
        return False


seastone2_helper = types.ModuleType('sff_mgr_test_seastone2.helper')
seastone2_helper.APIHelper = FakeAPIHelper

silverstone_sfp = load_sfp_module('silverstone_x')
s5232f_sfp = load_sfp_module('s5232f')
seastone2_sfp = load_sfp_module('seastone2', {'helper': seastone2_helper})


def qsfp28_eeprom(power_class=5, tx_disable=TX_DISABLE_ALL):
    """Page 00h (lower + upper) of an SFF-8636 QSFP28 optical module."""
    eeprom = bytearray(256)
    eeprom[0] = 0x11            # QSFP28
    eeprom[1] = 0x07            # SFF-8636 rev 2.5+
    eeprom[2] = 0x00            # paged memory
    eeprom[86] = tx_disable     # Tx1..Tx4 disable
    eeprom[93] = 0x00           # no power override, high power class disabled
    eeprom[128] = 0x11
    eeprom[129] = EXT_ID_BY_POWER_CLASS[power_class]
    eeprom[195] = 0x10          # options: Tx_Disable implemented
    return eeprom


def write_file(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def sysfs_normalize(path):
    """Present a value written by the platform code the way the driver's
    show() would: Seastone 2 writes hex(value), the FPGA prints "%d"."""
    path = Path(path)
    path.write_text('{}\n'.format(int(path.read_text().strip(), 0)))


class SffMgrPlatformTest(unittest.TestCase):
    """Shared fixture: temp sysfs/EEPROM and an sff_mgr worker pass."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='sff-mgr-'))
        self.addCleanup(self._rmtree, self.tmp)

    @staticmethod
    def _rmtree(path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    def start_patch(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def eeprom_file(self, data):
        path = self.tmp / 'eeprom'
        path.write_bytes(bytes(data))
        return path

    def eeprom(self):
        return (self.tmp / 'eeprom').read_bytes()

    def port_event(self, pport, **fields):
        port = {'type': 'QSFP28', 'subport': '0', 'lanes': '1,2,3,4'}
        port.update(fields)
        return PortChangeEvent('Ethernet0', pport, 0, PortChangeEvent.PORT_SET, port)

    def run_sff_mgr(self, sfp, pport, events, task=None):
        """One task_worker pass over `events`, the way xcvrd runs it."""
        if task is None:
            chassis = MagicMock()
            chassis.get_sfp = MagicMock(return_value=sfp)
            task = SffManagerTask([''], threading.Event(), chassis, MagicMock())
        for event in events:
            task.on_port_update_event(event)
        observer = MagicMock()
        observer.return_value.handle_port_update_event.return_value = True
        with patch('xcvrd.sff_mgr.PortChangeObserver', observer):
            # loop check, per-port check, loop exit
            task.task_stopping_event.is_set = MagicMock(side_effect=[False, False, True])
            task.task_worker()
        task.platform_chassis.get_sfp.assert_called_with(pport)
        return task

    # --- platform Sfp factories -------------------------------------------

    def silverstone_sfp(self, index, lpmode_pin='1'):
        Sfp = silverstone_sfp.Sfp
        sysfs = self.tmp / 'silverstone-x'
        if index < 32:
            write_file(sysfs / 'SFF/QSFP{}/qsfp_modprs'.format(index + 1), '0\n')
            write_file(sysfs / 'SFF/QSFP{}/qsfp_lpmode'.format(index + 1), lpmode_pin + '\n')
            write_file(sysfs / 'SFF/QSFP{}/qsfp_reset'.format(index + 1), '1\n')
        else:
            write_file(sysfs / 'fpga-xcvr/SFP{}/sfp_modabs'.format(index - 31), '0\n')
        for attr, rel in (('QSFP_PRS_PATH', 'SFF/QSFP{0}/qsfp_modprs'),
                          ('LP_PATH', 'SFF/QSFP{0}/qsfp_lpmode'),
                          ('RESET_PATH', 'SFF/QSFP{0}/qsfp_reset'),
                          ('SFP_PRS_PATH', 'fpga-xcvr/SFP{0}/sfp_modabs')):
            self.start_patch(patch.object(Sfp, attr, str(sysfs / rel)))
        self.start_patch(patch.object(Sfp, 'get_eeprom_path', return_value=str(self.tmp / 'eeprom')))
        self.start_patch(patch.object(Sfp, '_reinit_sfp_driver', return_value=None))
        self.silverstone_lpmode_pin = sysfs / 'SFF/QSFP{}/qsfp_lpmode'.format(index + 1)
        return Sfp(index)

    def s5232f_sfp(self, index, lpmode_pin=False):
        Sfp = s5232f_sfp.Sfp
        # Port control registers live at BAR offset 0x4000 + 16 * (port - 1).
        # s5232f_platform.sh writes 0x10 (ResetL high, LPMode low) to each.
        bar = bytearray(0x4000 + 34 * 16)
        for port in range(1, 35):
            struct.pack_into('I', bar, 0x4000 + (port - 1) * 16, 0x50 if lpmode_pin else 0x10)
        resource = self.tmp / 'resource0'
        resource.write_bytes(bytes(bar))
        self.start_patch(patch.object(Sfp, 'BASE_RES_PATH', str(resource)))
        self.start_patch(patch.object(Sfp, 'get_eeprom_path', return_value=str(self.tmp / 'eeprom')))
        self.start_patch(patch.object(Sfp, 'reinit_sfp_driver', return_value=None))
        self.s5232f_resource = resource
        return Sfp(index, 'QSFP' if index < 33 else 'SFP', str(self.tmp / 'eeprom'))

    def s5232f_port_reg(self, index):
        return struct.unpack_from('I', self.s5232f_resource.read_bytes(), 0x4000 + (index - 1) * 16)[0]

    def seastone2_sfp(self, index, lpmode_pin='0', with_lpmode_file=True):
        Sfp = seastone2_sfp.Sfp
        sysfs = self.tmp / 'seastone2_fpga'
        port = index + 1
        if port <= 32:
            name = 'QSFP{}'.format(port)
            write_file(sysfs / name / 'qsfp_modprs', '0\n')
            write_file(sysfs / name / 'qsfp_reset', '1\n')
        else:
            name = 'SFP{}'.format(port - 32)
            write_file(sysfs / name / 'sfp_modabs', '0\n')
        if with_lpmode_file:
            write_file(sysfs / name / 'qsfp_lpmode', lpmode_pin + '\n')
        self.start_patch(patch.object(seastone2_sfp, 'PORT_INFO_PATH', str(sysfs)))
        self.start_patch(patch.object(Sfp, 'get_eeprom_path', return_value=str(self.tmp / 'eeprom')))
        self.seastone2_lpmode_pin = sysfs / name / 'qsfp_lpmode'
        return Sfp(index)

    def check_sff_mgr_flow(self, sfp, pport, read_pin, pin_value):
        self.assertIsInstance(sfp.get_xcvr_api(), Sff8636Api)
        self.assertEqual(sfp.get_xcvr_api().get_power_class(), 5)
        # Insertion with the port admin up and host_tx_ready true: high power
        # class enabled, power override takes the module out of low power
        # mode, TX enabled on all four lanes.
        task = self.run_sff_mgr(sfp, pport, [self.port_event(pport, host_tx_ready='true',
                                                              admin_status='up')])
        eeprom = self.eeprom()
        self.assertEqual(eeprom[93] & 0x0F, POWER_OVERRIDE | HIGH_POWER_CLASS_5_TO_7)
        self.assertEqual(eeprom[86], 0x00)
        # sff_mgr drives the module through EEPROM byte 93; the platform's
        # LPMode pin is left where it was, so the platform get_lpmode() (what
        # `show interfaces transceiver lpmode` prints) still reports the pin.
        self.assertEqual(read_pin(), pin_value)
        self.assertFalse(sfp.get_xcvr_api().get_lpmode())
        # host_tx_ready going false disables TX and leaves power alone.
        self.run_sff_mgr(sfp, pport, [self.port_event(pport, host_tx_ready='false')], task)
        eeprom = self.eeprom()
        self.assertEqual(eeprom[86], TX_DISABLE_ALL)
        self.assertEqual(eeprom[93] & 0x0F, POWER_OVERRIDE | HIGH_POWER_CLASS_5_TO_7)
        self.assertEqual(read_pin(), pin_value)

    def check_cmis_skipped(self, sfp, pport):
        before = self.eeprom()
        sfp._xcvr_api = MagicMock(spec=CmisApi)
        self.run_sff_mgr(sfp, pport, [self.port_event(pport, type='QSFP-DD', host_tx_ready='true',
                                                       admin_status='up')])
        self.assertEqual(self.eeprom(), before)
        sfp._xcvr_api.set_lpmode.assert_not_called()
        sfp._xcvr_api.tx_disable_channel.assert_not_called()


class PmonDaemonControlTest(unittest.TestCase):
    """enable_xcvrd_sff_mgr is set and reaches the xcvrd command line."""

    expected = {
        SILVERSTONE_X: {'enable_xcvrd_sff_mgr': True},
        S5232F: {'skip_ledd': True, 'enable_xcvrd_sff_mgr': True},
        SEASTONE_2: {'skip_ledd': True, 'skip_fancontrol': True,
                     'skip_xcvrd_cmis_mgr': True, 'enable_xcvrd_sff_mgr': True},
    }

    def control(self, platform):
        with open(platform / 'pmon_daemon_control.json') as fd:
            return json.load(fd)

    def test_flag_set_and_other_defaults_kept(self):
        for platform, expected in self.expected.items():
            with self.subTest(platform=platform.name):
                self.assertEqual(self.control(platform), expected)

    def test_xcvrd_command_line(self):
        try:
            import jinja2
        except ImportError:
            self.skipTest('jinja2 not installed')
        template_path = ROOT / 'dockers/docker-platform-monitor/docker-pmon.supervisord.conf.j2'
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(template_path.parent)))
        template = env.get_template(template_path.name)
        for platform in self.expected:
            with self.subTest(platform=platform.name):
                # sonic-cfggen -j pmon_daemon_control.json exposes its keys as
                # top-level template variables.
                context = dict(API_VERSION=3, HAVE_SENSORS_CONF=0, HAVE_FANCONTROL_CONF=0,
                               IS_MODULAR_CHASSIS=0, IS_SWITCH_BMC=0, python2_daemons=[])
                context.update(self.control(platform))
                rendered = template.render(**context)
                xcvrd = rendered.partition('[program:xcvrd]')[2].partition('[program:')[0]
                command = [line for line in xcvrd.splitlines() if line.startswith('command=')]
                self.assertEqual(len(command), 1, xcvrd)
                self.assertIn('--enable_sff_mgr', command[0])
                self.assertEqual('--skip_cmis_mgr' in command[0],
                                 self.control(platform).get('skip_xcvrd_cmis_mgr', False))


class SilverstoneXLpmodeTest(SffMgrPlatformTest):
    def test_qsfp_lpmode_pin(self):
        self.eeprom_file(qsfp28_eeprom())
        sfp = self.silverstone_sfp(3, lpmode_pin='0')
        self.assertEqual(sfp.sfp_type, 'QSFP')
        self.assertFalse(sfp.get_lpmode())
        self.assertTrue(sfp.set_lpmode(True))
        self.assertEqual(self.silverstone_lpmode_pin.read_text().strip(), '1')
        self.assertTrue(sfp.get_lpmode())
        self.assertTrue(sfp.set_lpmode(False))
        self.assertEqual(self.silverstone_lpmode_pin.read_text().strip(), '0')
        self.assertFalse(sfp.get_lpmode())

    def test_sfp_ports_have_no_lpmode(self):
        # Ports 33/34 are SFP+ cages: no LPMode signal, and no QSFP33 sysfs node.
        self.eeprom_file(qsfp28_eeprom())
        sfp = self.silverstone_sfp(32)
        self.assertEqual(sfp.port_type, 'SFP')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(sfp.get_lpmode())
            self.assertFalse(sfp.set_lpmode(False))
        self.assertEqual(out.getvalue(), '')

    def test_sff_mgr_flow(self):
        self.eeprom_file(qsfp28_eeprom(power_class=5))
        sfp = self.silverstone_sfp(0, lpmode_pin='1')
        self.check_sff_mgr_flow(sfp, 0, lambda: self.silverstone_lpmode_pin.read_text().strip(), '1')

    def test_sff_mgr_skips_cmis(self):
        self.eeprom_file(qsfp28_eeprom())
        sfp = self.silverstone_sfp(0)
        self.check_cmis_skipped(sfp, 0)

class S5232FLpmodeTest(SffMgrPlatformTest):
    def test_qsfp_lpmode_pin(self):
        self.eeprom_file(qsfp28_eeprom())
        sfp = self.s5232f_sfp(5)
        self.assertEqual(self.s5232f_port_reg(5), 0x10)
        self.assertFalse(sfp.get_lpmode())
        self.assertTrue(sfp.set_lpmode(True))
        self.assertEqual(self.s5232f_port_reg(5), 0x50)
        self.assertTrue(sfp.get_lpmode())
        self.assertTrue(sfp.set_lpmode(False))
        self.assertEqual(self.s5232f_port_reg(5), 0x10)
        self.assertFalse(sfp.get_lpmode())
        # Only the lpmode bit of that port's register was touched.
        self.assertEqual(self.s5232f_port_reg(4), 0x10)
        self.assertEqual(self.s5232f_port_reg(6), 0x10)

    def test_sfp_ports_have_no_lpmode(self):
        self.eeprom_file(qsfp28_eeprom())
        sfp = self.s5232f_sfp(33)
        self.assertFalse(sfp.get_lpmode())
        self.assertFalse(sfp.set_lpmode(True))
        self.assertEqual(self.s5232f_port_reg(33), 0x10)

    def test_sff_mgr_flow(self):
        self.eeprom_file(qsfp28_eeprom(power_class=5))
        sfp = self.s5232f_sfp(1, lpmode_pin=True)
        self.check_sff_mgr_flow(sfp, 1, lambda: self.s5232f_port_reg(1), 0x50)

    def test_sff_mgr_skips_cmis(self):
        self.eeprom_file(qsfp28_eeprom())
        sfp = self.s5232f_sfp(1)
        self.check_cmis_skipped(sfp, 1)


class Seastone2LpmodeTest(SffMgrPlatformTest):
    def test_qsfp_lpmode_pin(self):
        self.eeprom_file(qsfp28_eeprom())
        sfp = self.seastone2_sfp(7, lpmode_pin='0')
        self.assertFalse(sfp.get_lpmode())
        self.assertTrue(sfp.set_lpmode(True))
        self.assertEqual(int(self.seastone2_lpmode_pin.read_text().strip(), 0), 1)
        sysfs_normalize(self.seastone2_lpmode_pin)
        self.assertTrue(sfp.get_lpmode())
        self.assertTrue(sfp.set_lpmode(False))
        sysfs_normalize(self.seastone2_lpmode_pin)
        self.assertFalse(sfp.get_lpmode())

    def test_sfp_port_has_no_lpmode(self):
        self.eeprom_file(qsfp28_eeprom())
        sfp = self.seastone2_sfp(32, with_lpmode_file=False)
        self.assertEqual(sfp.sfp_type, 'SFP')
        self.assertFalse(sfp.get_lpmode())
        self.assertFalse(sfp.set_lpmode(True))
        self.assertFalse(self.seastone2_lpmode_pin.exists())

    def test_unreadable_lpmode_reports_off(self):
        self.eeprom_file(qsfp28_eeprom())
        sfp = self.seastone2_sfp(0, with_lpmode_file=False)
        self.assertFalse(sfp.get_lpmode())

    def test_sff_mgr_flow(self):
        self.eeprom_file(qsfp28_eeprom(power_class=5))
        sfp = self.seastone2_sfp(0, lpmode_pin='1')
        self.check_sff_mgr_flow(sfp, 1, lambda: self.seastone2_lpmode_pin.read_text().strip(), '1')

    def test_sff_mgr_skips_cmis(self):
        self.eeprom_file(qsfp28_eeprom())
        sfp = self.seastone2_sfp(0)
        self.check_cmis_skipped(sfp, 1)


class PowerClassTest(SffMgrPlatformTest):
    """sff_mgr power handling by module power class (platform-independent)."""

    def test_class_4_gets_power_override_only(self):
        self.eeprom_file(qsfp28_eeprom(power_class=4))
        sfp = self.s5232f_sfp(1)
        self.run_sff_mgr(sfp, 1, [self.port_event(1, host_tx_ready='true', admin_status='up')])
        eeprom = self.eeprom()
        self.assertEqual(eeprom[93] & 0x0F, POWER_OVERRIDE)
        self.assertEqual(eeprom[86], 0x00)

    def test_class_1_is_left_alone(self):
        self.eeprom_file(qsfp28_eeprom(power_class=1))
        sfp = self.s5232f_sfp(1)
        self.run_sff_mgr(sfp, 1, [self.port_event(1, host_tx_ready='true', admin_status='up')])
        eeprom = self.eeprom()
        self.assertEqual(eeprom[93], 0x00)
        self.assertEqual(eeprom[86], 0x00)

    def test_admin_down_keeps_tx_disabled(self):
        self.eeprom_file(qsfp28_eeprom(power_class=5))
        sfp = self.s5232f_sfp(1)
        self.run_sff_mgr(sfp, 1, [self.port_event(1, host_tx_ready='true', admin_status='down')])
        eeprom = self.eeprom()
        self.assertEqual(eeprom[86], TX_DISABLE_ALL)
        # Insertion still enables the high power class so DOM works.
        self.assertEqual(eeprom[93] & 0x0F, POWER_OVERRIDE | HIGH_POWER_CLASS_5_TO_7)


if __name__ == '__main__':
    unittest.main()
