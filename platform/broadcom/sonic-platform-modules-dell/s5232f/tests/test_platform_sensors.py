"""Hardware-free regression for kamelnetworks/sonic-buildimage#135:
`show environment` printed an empty "PSUs:" section on the Dell S5232F-ON.

platform_sensors.py (run as `docker exec pmon /usr/bin/platform_sensors.py`
by the `sensors` wrapper that `show environment` calls) checks the PSU state
with getstatusoutput_noshell_pipe(ipmitool raw ..., awk ...). That helper
returns one exit code per command of the pipeline, a list such as [0, 0],
and the script tested it with `if ret_status:`, which is true for any
non-empty list. So the first PSU lookup always logged
"ERROR:root:Failed to execute ipmitool :ipmitool raw 0x04 0x2d 0x31" and
sys.exit(0)ed before printing a PSU.

The script is straight-line code, so the test runs it with runpy against
fake sonic_py_common.general helpers serving ipmitool output recorded on
ixp-lab-sw4 (rc1), and checks what it prints.
"""
import io
import logging
import runpy
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'platform_sensors.py'
KIPMID_PARAM = '/sys/module/ipmi_si/parameters/kipmid_max_busy_us'

SDR_LIST = """\
PT_Mid_temp      | 34 degrees C      | ok
NPU_Near_temp    | 32 degrees C      | ok
PT_Left_temp     | 29 degrees C      | ok
PT_Right_temp    | 30 degrees C      | ok
ILET_AF_temp     | 29 degrees C      | ok
PSU1_AF_temp     | 28 degrees C      | ok
PSU2_AF_temp     | 27 degrees C      | ok
PSU1_temp        | 32 degrees C      | ok
PSU2_temp        | 40 degrees C      | ok
CPU_temp         | 30 degrees C      | ok
FAN1_Rear_rpm    | 11400 RPM         | ok
FAN2_Rear_rpm    | 11280 RPM         | ok
FAN3_Rear_rpm    | 11280 RPM         | ok
FAN4_Rear_rpm    | 11400 RPM         | ok
FAN1_Front_rpm   | 12720 RPM         | ok
FAN2_Front_rpm   | 12720 RPM         | ok
FAN3_Front_rpm   | 12600 RPM         | ok
FAN4_Front_rpm   | 12600 RPM         | ok
PSU1_rpm         | 12120 RPM         | ok
PSU2_rpm         | 12120 RPM         | ok
PSU_Total_watt   | 140 Watts         | ok
PSU1_stat        | 0x01              | ok
PSU2_stat        | 0x01              | ok
PSU1_In_watt     | 80 Watts          | ok
PSU1_In_volt     | 228.80 Volts      | ok
PSU1_In_amp      | 0.32 Amps         | ok
PSU1_Out_watt    | 60 Watts          | ok
PSU1_Out_volt    | 12.40 Volts       | ok
PSU1_Out_amp     | 5 Amps            | ok
PSU2_In_watt     | 60 Watts          | ok
PSU2_In_volt     | 228.80 Volts      | ok
PSU2_In_amp      | 0.16 Amps         | ok
PSU2_Out_watt    | 40 Watts          | ok
PSU2_Out_volt    | 12.40 Volts       | ok
PSU2_Out_amp     | 3.50 Amps         | ok
ACPI_stat        | 0x01              | ok
FAN1_prsnt       | 0x01              | ok
FAN2_prsnt       | 0x01              | ok
FAN3_prsnt       | 0x01              | ok
FAN4_prsnt       | 0x01              | ok
FAN1_Rear_stat   | 0x00              | ok
FAN2_Rear_stat   | 0x00              | ok
FAN3_Rear_stat   | 0x00              | ok
FAN4_Rear_stat   | 0x00              | ok
FAN1_Front_stat  | 0x00              | ok
FAN2_Front_stat  | 0x00              | ok
FAN3_Front_stat  | 0x00              | ok
FAN4_Front_stat  | 0x00              | ok
INTER_5.0V_volt  | 4.90 Volts        | ok
INTER_3.3V_volt  | 3.20 Volts        | ok
FPGA_1.0V_volt   | 0.99 Volts        | ok
FPGA_1.2V_volt   | 1.18 Volts        | ok
FPGA_1.8V_volt   | 1.78 Volts        | ok
FPGA_3.3V_volt   | 3.20 Volts        | ok
BMC_2.5V_volt    | 2.40 Volts        | ok
BMC_1.15V_volt   | 1.15 Volts        | ok
BMC_1.2V_volt    | 1.21 Volts        | ok
SWITCH_6.8V_volt | 6.90 Volts        | ok
SWITCH_3.3V_volt | 3.40 Volts        | ok
SWITCH_1.8V_volt | 1.77 Volts        | ok
USB_5.0V_volt    | 4.90 Volts        | ok
NPU_1.2V_volt    | 1.21 Volts        | ok
NPU_VDDCORE_volt | 0.83 Volts        | ok
NPU_VDDANLG_volt | 0.81 Volts        | ok
BMC boot         | 0x01              | ok
SEL_sensor       | 0x00              | ok
"""

FRU_ALL = """\
FRU Device Description : Builtin FRU Device (ID 0)
 Board Mfg Date        : Thu 09 May 2019 06:47:00 AM UTC UTC
 Board Mfg             : CES00   
 Board Product         : S5232F-ON
 Board Serial          : CN0RC7V6CES0094Q0024
 Board Part Number     : 0RC7V6A00
 Product Manufacturer  : CES00   
 Product Name          : S5232F-ON
 Product Asset Tag     : H5X00Q2

FRU Device Description : PSU1_fru (ID 1)
 Board Mfg Date        : Sun 31 Mar 2019 08:22:00 AM UTC UTC
 Board Mfg             : DELL
 Board Product         : PWR SPLY,750W,AC,PS/IO,DELTA  
 Board Serial          : CNDED0093I02EU
 Board Part Number     : 0R17R1A00

FRU Device Description : PSU2_fru (ID 2)
 Board Mfg Date        : Sun 31 Mar 2019 08:22:00 AM UTC UTC
 Board Mfg             : DELL
 Board Product         : PWR SPLY,750W,AC,PS/IO,DELTA  
 Board Serial          : CNDED0093I03MM
 Board Part Number     : 0R17R1A00

FRU Device Description : FAN1_fru (ID 3)
 Board Mfg Date        : Thu 09 May 2019 06:50:00 AM UTC UTC
 Board Serial          : CN07R5RFCES0094D0105
 Board Part Number     : 07R5RFA00

FRU Device Description : FAN2_fru (ID 4)
 Board Mfg Date        : Thu 09 May 2019 06:50:00 AM UTC UTC
 Board Serial          : CN07R5RFCES0094D0106
 Board Part Number     : 07R5RFA00

FRU Device Description : FAN3_fru (ID 5)
 Board Mfg Date        : Thu 09 May 2019 06:50:00 AM UTC UTC
 Board Serial          : CN07R5RFCES0094D0098
 Board Part Number     : 07R5RFA00

FRU Device Description : FAN4_fru (ID 6)
 Board Mfg Date        : Thu 09 May 2019 06:50:00 AM UTC UTC
 Board Serial          : CN07R5RFCES0094D0099
 Board Part Number     : 07R5RFA00

"""

SENSOR_GET_FAN1_PRSNT = """\
Locating sensor record...
Sensor ID              : FAN1_prsnt (0x53)
 Entity ID             : 29.1
 Sensor Type (Discrete): Entity Presence
 States Asserted       : Entity Presence
                         [Present]

"""

FAN_FRU_DUMP = """\
 a0 01 01 00 0a 00 00 00 f4 01 44 45 4c 4c 00 00
 00 02 02 00 00 20 20 9f 02 d2 20 00 d1 28 00 d6
 38 00 00 00 00 00 00 00 00 d2 08 00 12 01 13 00
 00 d1 10 00 cd 01 19 00 1c 00 1c 00 00 00 00 00
 00 d6 10 00 81 01 01 02 d8 59 14 50 00 00 00 00
 00 01 06 00 3a 69 bb c0 c0 d4 43 4e 30 37 52 35
 52 46 43 45 53 30 30 39 34 44 30 31 30 35 c9 30
 37 52 35 52 46 41 30 30 c0 c1 00 00 00 00 00 c1
 4c 00 00 00 00 19 7d 00 00 e0 03 60 b4 e0 03 60
 b4 10 0e 60 b4 10 0e 60 b4 00 00 00 00 00 00 00
 00
"""


# ipmitool raw 0x04 0x2d 0x31 | awk '{print substr($0,9,1)}': low nibble of
# the PSU state byte (" 00 c0 01 80" -> "1": present, OK).
PSU_STATE_NIBBLE = '1'


class FakeGeneral:
    """Stand-in for sonic_py_common.general with the real return shapes:
    getstatusoutput_noshell -> (int, str),
    getstatusoutput_noshell_pipe -> (list of int, str)."""

    def __init__(self, pipe_exit_codes=(0, 0), psu_state=PSU_STATE_NIBBLE):
        self.pipe_exit_codes = list(pipe_exit_codes)
        self.psu_state = psu_state
        self.pipes = []
        self.commands = []

    def getstatusoutput_noshell(self, cmd):
        self.commands.append(list(cmd))
        if cmd[:3] == ['ipmitool', 'sdr', 'list']:
            return 0, SDR_LIST
        if cmd[:3] == ['ipmitool', 'sensor', 'get'] and cmd[3].endswith('_prsnt'):
            return 0, SENSOR_GET_FAN1_PRSNT.replace('FAN1_prsnt', cmd[3])
        if cmd == ['ipmitool', 'fru']:
            return 0, FRU_ALL
        if cmd[:4] == ['ipmitool', 'raw', '0x0a', '0x11'] and cmd[4] in ('3', '4', '5', '6'):
            return 0, FAN_FRU_DUMP
        return 1, 'unexpected command: {}'.format(' '.join(cmd))

    def getstatusoutput_noshell_pipe(self, cmd0, *args):
        self.pipes.append([list(cmd0)] + [list(a) for a in args])
        assert cmd0[:4] == ['ipmitool', 'raw', '0x04', '0x2d'], cmd0
        assert args[0][0] == 'awk', args
        return self.pipe_exit_codes, self.psu_state

    def module(self):
        general = types.ModuleType('sonic_py_common.general')
        general.getstatusoutput_noshell = self.getstatusoutput_noshell
        general.getstatusoutput_noshell_pipe = self.getstatusoutput_noshell_pipe
        package = types.ModuleType('sonic_py_common')
        package.general = general
        return {'sonic_py_common': package, 'sonic_py_common.general': general}


class PlatformSensorsTest(unittest.TestCase):
    def run_script(self, general):
        """Run platform_sensors.py; returns (stdout, exit code or None, log records)."""
        sysfs = {}
        real_open = open

        def fake_open(path, mode='r', *args, **kwargs):
            if str(path) == KIPMID_PARAM:
                handle = io.StringIO()
                handle.close = lambda: sysfs.__setitem__(path, handle.getvalue())
                return handle
            return real_open(path, mode, *args, **kwargs)

        stdout = io.StringIO()
        exit_code = None
        with patch.dict(sys.modules, general.module()), \
                patch('builtins.open', fake_open), \
                self.assertLogs(level=logging.ERROR) if general.pipe_exit_codes != [0, 0] \
                else _no_logs(self), \
                redirect_stdout(stdout):
            try:
                runpy.run_path(str(SCRIPT), run_name='__main__')
            except SystemExit as e:
                exit_code = e.code
        # The script lowers kipmid's busy-wait while it polls and restores it.
        self.assertEqual(sysfs.get(KIPMID_PARAM), '1000\n' if exit_code is None else '0\n')
        return stdout.getvalue(), exit_code

    def test_psu_section_lists_both_psus(self):
        general = FakeGeneral()
        output, exit_code = self.run_script(general)
        self.assertIsNone(exit_code, output)
        psus = output[output.index('PSUs:'):]
        self.assertIn('    PSU1:', psus)
        self.assertIn('    PSU2:', psus)
        self.assertEqual(psus.count('Airflow:                       Intake'), 2)
        self.assertIn('FAN RPM:                        12120 RPM', psus)
        self.assertIn('Input Voltage:                  228.80 Volts', psus)
        self.assertRegex(psus, r'Total Power:\s+140 Watts')
        self.assertNotIn('NOT PRESENT', psus)
        self.assertNotIn('NOT OK', psus)
        # presence + status for each of the two PSUs
        self.assertEqual(len(general.pipes), 4)
        self.assertEqual(general.pipes[0][0], ['ipmitool', 'raw', '0x04', '0x2d', '0x31'])
        self.assertEqual(general.pipes[2][0], ['ipmitool', 'raw', '0x04', '0x2d', '0x32'])

    def test_fan_trays_and_temperatures_still_print(self):
        output, _ = self.run_script(FakeGeneral())
        self.assertIn('Onboard Temperature Sensors:', output)
        self.assertIn('Broadcom Temp:                    32 degrees C', output)
        self.assertEqual(output.count('Airflow:                       Intake'), 6)  # 4 trays + 2 PSUs
        self.assertIn('Fan Tray 4:', output)

    def test_absent_psu(self):
        output, exit_code = self.run_script(FakeGeneral(psu_state='0'))
        self.assertIsNone(exit_code)
        self.assertIn('PSU1:                      NOT PRESENT', output)
        self.assertIn('PSU2:                      NOT PRESENT', output)

    def test_failed_pipeline_is_still_an_error(self):
        """A real ipmitool failure (non-zero exit code in the list) must still
        be reported, not treated as success."""
        output, exit_code = self.run_script(FakeGeneral(pipe_exit_codes=(1, 0)))
        self.assertEqual(exit_code, 0)
        self.assertTrue(output.rstrip().endswith('PSUs:'), output)


class _no_logs:
    """assertNoLogs() as a context manager usable in a with-statement chain."""

    def __init__(self, case):
        self.case = case

    def __enter__(self):
        self.cm = self.case.assertNoLogs(level=logging.ERROR)
        return self.cm.__enter__()

    def __exit__(self, *exc):
        return self.cm.__exit__(*exc)


if __name__ == '__main__':
    unittest.main()
