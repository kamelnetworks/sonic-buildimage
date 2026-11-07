"""Hardware-free regression for kamelnetworks/sonic-buildimage#135:
PSU fan direction on the Dell S5232F-ON.

Fan.get_direction() read PSU_FAN_DIRECTION_OFFSET (47) of the PSU FRU as a
direction byte. The PSU FRU is a standard IPMI board info area, so on the
750W Delta PSU that byte is a space inside the "Board Product" string
("PWR SPLY,750W,AC,PS/IO,DELTA"), the read is rejected as invalid and the
code returned the string 'NA'. thermalctld/psud publish that as-is, and
system-health only skips the "direction is not aligned" check for the literal
'N/A', so every S5232F reported Hardware Not OK with
"PSU1 Fan direction NA is not aligned with FanTray1-Fan1 direction intake".

The airflow is the IO/PS (exhaust) or PS/IO (intake) token of the product
string. The canned ipmitool output below is from ixp-lab-sw4 on rc1.

Needs sonic-platform-common on PYTHONPATH, e.g. from the repository root:

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

PSU_FRU_PRINT = """\
 Board Mfg Date        : Sun 31 Mar 2019 08:22:00 AM UTC UTC
 Board Mfg             : DELL
 Board Product         : PWR SPLY,750W,AC,PS/IO,DELTA  
 Board Serial          : CNDED0093I02EU
 Board Part Number     : 0R17R1A00
"""

FAN_FRU_PRINT = """\
 Board Mfg Date        : Thu 09 May 2019 06:50:00 AM UTC UTC
 Board Serial          : CN07R5RFCES0094D0105
 Board Part Number     : 07R5RFA00
"""

# ipmitool raw 4 45 <id>: PSU1_stat / FAN1_prsnt, present and OK.
STATE_PRESENT = ' 00 c0 01 80'
STATE_ABSENT = ' 00 c0 00 80'


def load_platform():
    """Import the S5232F sonic_platform package from the source tree.

    The Debian build copies common/ipmihelper.py into the package, so do the
    same here. Everything is loaded under patch.dict so nothing leaks into
    sys.modules for other test modules.
    """
    package = types.ModuleType('sonic_platform')
    package.__path__ = [str(MODULE_DIR / 'sonic_platform')]
    with patch.dict(sys.modules, {'sonic_platform': package}):
        spec = importlib.util.spec_from_file_location('sonic_platform.ipmihelper', IPMIHELPER)
        ipmihelper = importlib.util.module_from_spec(spec)
        sys.modules['sonic_platform.ipmihelper'] = ipmihelper
        spec.loader.exec_module(ipmihelper)
        import sonic_platform.fan
        import sonic_platform.psu
        return ipmihelper, sonic_platform.fan, sonic_platform.psu


ipmihelper, fan_module, psu_module = load_platform()


class FakeProc:
    def __init__(self, output, returncode=0):
        self.output, self.returncode = output, returncode

    def communicate(self):
        return self.output, None

    def wait(self):
        return self.returncode


class FakeIpmitool:
    """subprocess.Popen replacement serving canned ipmitool output.

    responses maps the argv after 'ipmitool' (as one string) to either the
    output text or (text, returncode). Anything else fails like ipmitool does
    for an unsupported command, and every call is recorded in self.calls.
    """

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


def psu_responses(product_line=PSU_FRU_PRINT, present=True):
    return {
        'raw 4 45 49': STATE_PRESENT if present else STATE_ABSENT,  # PSU1_stat (0x31)
        'fru print 1': product_line,
    }


def fan_responses(direction_byte, present=True):
    responses = {
        'raw 4 45 83': STATE_PRESENT if present else STATE_ABSENT,  # FAN1_prsnt (0x53)
        'fru print 3': FAN_FRU_PRINT,
    }
    if direction_byte is not None:
        # ipmitool raw 0x0a 0x11 <fru> <offset> 0 1 -> count, byte
        responses['raw 10 17 3 69 0 1'] = ' 01 {:02x}'.format(direction_byte)
    return responses


class FanDirectionTest(unittest.TestCase):
    def use_ipmitool(self, responses):
        self.ipmitool = FakeIpmitool(responses)
        self.enterContext(patch.object(ipmihelper.subprocess, 'Popen', self.ipmitool))
        # These tests change the replies between calls, so run them without
        # the helper's reply cache (#163, test_ipmihelper_cache.py).
        ipmihelper.clear_cache()
        self.enterContext(patch.object(ipmihelper, 'CACHE_TTLS', {}))

    def psu_fan(self):
        psu = psu_module.Psu(0)
        fans = psu.get_all_fans()
        self.assertEqual(len(fans), 1)
        self.assertEqual(fans[0].get_name(), 'PSU1 Fan')
        return fans[0]

    def tray_fan(self):
        fan = fan_module.Fan(fantray_index=0, fan_index=0)
        self.assertEqual(fan.get_name(), 'FanTray1-Fan1')
        return fan

    def test_psu_fan_reverse_airflow_is_intake(self):
        self.use_ipmitool(psu_responses())
        self.assertEqual(self.psu_fan().get_direction(), fan_module.Fan.FAN_DIRECTION_INTAKE)
        # The direction comes from the product string, not from the bogus
        # direction byte at FRU offset 47.
        self.assertNotIn('raw 10 17 1 47 0 1', self.ipmitool.calls)

    def test_psu_fan_normal_airflow_is_exhaust(self):
        self.use_ipmitool(psu_responses(PSU_FRU_PRINT.replace('PS/IO', 'IO/PS')))
        self.assertEqual(self.psu_fan().get_direction(), fan_module.Fan.FAN_DIRECTION_EXHAUST)

    def test_psu_fan_direction_matches_fan_tray(self):
        """On ixp-lab-sw4 the trays read intake (FRU byte 1) and the PSUs say PS/IO."""
        self.use_ipmitool({**psu_responses(), **fan_responses(1)})
        self.assertEqual(self.psu_fan().get_direction(), self.tray_fan().get_direction())

    def test_psu_fan_without_airflow_token_is_not_applicable(self):
        self.use_ipmitool(psu_responses(PSU_FRU_PRINT.replace('PS/IO,', '')))
        self.assertEqual(self.psu_fan().get_direction(), 'N/A')

    def test_psu_fan_unreadable_fru_is_not_applicable(self):
        responses = psu_responses()
        responses['fru print 1'] = ('Device not present (Requested sensor, data, or record not found)', 1)
        self.use_ipmitool(responses)
        direction = self.psu_fan().get_direction()
        # system-health (hardware_checker.py) compares against the literal
        # 'N/A'; any other placeholder is checked as a real direction.
        self.assertEqual(direction, fan_module.FanBase.FAN_DIRECTION_NOT_APPLICABLE)
        self.assertEqual(direction, 'N/A')

    def test_absent_psu_fan_is_not_applicable(self):
        self.use_ipmitool(psu_responses(present=False))
        fan = self.psu_fan()
        self.assertFalse(fan.get_presence())
        self.assertEqual(fan.get_direction(), 'N/A')
        self.assertNotIn('fru print 1', self.ipmitool.calls)

    def test_psu_fan_direction_is_read_once_while_present(self):
        """ipmitool fru print costs about a second over KCS per call, and
        thermalctld polls every minute: read it once, again after a swap."""
        self.use_ipmitool(psu_responses())
        fan = self.psu_fan()
        self.assertEqual(fan.get_direction(), 'intake')
        self.assertEqual(fan.get_direction(), 'intake')
        self.assertEqual(self.ipmitool.calls.count('fru print 1'), 1)
        # PSU pulled: direction is N/A and the cache is dropped.
        self.ipmitool.responses.update(psu_responses(present=False))
        self.assertEqual(fan.get_direction(), 'N/A')
        # A normal-airflow PSU is plugged in.
        self.ipmitool.responses.update(psu_responses(PSU_FRU_PRINT.replace('PS/IO', 'IO/PS')))
        self.assertEqual(fan.get_direction(), 'exhaust')
        self.assertEqual(self.ipmitool.calls.count('fru print 1'), 2)

    def test_psu_fan_unreadable_fru_is_retried(self):
        responses = psu_responses()
        responses['fru print 1'] = ('Unable to read FRU', 1)
        self.use_ipmitool(responses)
        fan = self.psu_fan()
        self.assertEqual(fan.get_direction(), 'N/A')
        self.ipmitool.responses['fru print 1'] = PSU_FRU_PRINT
        self.assertEqual(fan.get_direction(), 'intake')

    def test_fan_tray_direction_byte(self):
        self.use_ipmitool(fan_responses(1))
        self.assertEqual(self.tray_fan().get_direction(), 'intake')
        self.use_ipmitool(fan_responses(0))
        self.assertEqual(self.tray_fan().get_direction(), 'exhaust')

    def test_fan_tray_unreadable_fru_is_not_applicable(self):
        self.use_ipmitool(fan_responses(None))
        self.assertEqual(self.tray_fan().get_direction(), 'N/A')
        self.use_ipmitool(fan_responses(7))
        self.assertEqual(self.tray_fan().get_direction(), 'N/A')

    def test_absent_fan_tray_is_not_applicable(self):
        self.use_ipmitool(fan_responses(1, present=False))
        self.assertEqual(self.tray_fan().get_direction(), 'N/A')

    def test_no_fan_reports_na_placeholder(self):
        """The old 'NA' string must not come back from any code path."""
        for responses in (psu_responses(), psu_responses(present=False),
                          psu_responses(PSU_FRU_PRINT.replace('PS/IO,', '')),
                          fan_responses(1), fan_responses(None), fan_responses(1, present=False)):
            self.use_ipmitool(responses)
            for fan in (self.psu_fan(), self.tray_fan()):
                self.assertNotEqual(fan.get_direction(), 'NA')


if __name__ == '__main__':
    unittest.main()
