"""Silverstone-X init script must wait for the mux buses (issue #139).

The transceiver EEPROM buses i2c-12..45 are registered by cls_pca954x
when it binds the five pca9548 switches that fpga_i2c_ocores creates on
i2c-11 (modules/fpga_device.c, om_muxes).  Whether that happens inside
"modprobe fpga_i2c_ocores" or some time later depends on whether the mux
driver was already loaded: if udev loads it on the i2c:pca9548 uevent, the
buses appear after modprobe has returned.  The init script used to loop
over i2c-12..45 straight away, so every bus that was not there yet got no
optoe device ("cat: /sys/bus/i2c/devices/i2c-12/name: No such file or
directory" through i2c-26 on ixp-lab-sw1, QSFP1-15 "Not present").

These tests run the script's device-creation function against a fake
sysfs tree.  They need bash and run without hardware.
"""

import os
import subprocess
import threading
import time
import unittest
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PLATFORM_DIR = os.path.dirname(TESTS_DIR)                      # silverstone-x/
PACKAGE_DIR = os.path.dirname(PLATFORM_DIR)                     # sonic-platform-modules-cel/
INIT_SCRIPT = os.path.join(PACKAGE_DIR, 'debian', 'platform-modules-silverstone-x.init')

QSFP_BUSES = range(12, 44)   # qsfp1..32
SFP_BUSES = range(44, 46)    # sfp1..2
ALL_BUSES = range(12, 46)

# i2c-mux-core names a channel adapter "i2c-<parent>-mux (chan_id <n>)".
MUX_NAME = 'i2c-11-mux (chan_id %d)\n'


class FakeSysfs:
    """A /sys/bus/i2c/devices look-alike with regular files for the knobs."""

    def __init__(self, root):
        self.root = root

    def bus_dir(self, bus):
        return os.path.join(self.root, 'i2c-%d' % bus)

    def add_bus(self, bus):
        d = self.bus_dir(bus)
        # The client directory exists before the script writes port_name;
        # in sysfs the kernel creates it when new_device is written.
        os.makedirs(os.path.join(d, '%d-0050' % bus))
        with open(os.path.join(d, 'name'), 'w') as f:
            f.write(MUX_NAME % ((bus - 12) % 8))
        open(os.path.join(d, 'new_device'), 'w').close()

    def _read(self, bus, *path):
        p = os.path.join(self.bus_dir(bus), *path)
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return f.read().strip()

    def new_device(self, bus):
        return self._read(bus, 'new_device')

    def port_name(self, bus):
        return self._read(bus, '%d-0050' % bus, 'port_name')


def run_create_optoe_devices(sysfs, timeout):
    """Source the init script and run create_optoe_devices against sysfs."""
    env = {
        'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
        'I2C_SYSFS': sysfs.root,
        'I2C_BUS_TIMEOUT': str(timeout),
    }
    return subprocess.run(
        ['bash', '-c', 'source "$1" && create_optoe_devices', 'bash', INIT_SCRIPT],
        env=env, capture_output=True, text=True, timeout=60)


class InitScriptBusWaitTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sysfs = FakeSysfs(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def assert_devices_on(self, buses):
        for bus in buses:
            if bus in QSFP_BUSES:
                self.assertEqual(self.sysfs.new_device(bus), 'optoe1 0x50', 'i2c-%d' % bus)
                self.assertEqual(self.sysfs.port_name(bus), 'qsfp%d' % (bus - 11))
            else:
                self.assertEqual(self.sysfs.new_device(bus), 'optoe2 0x50', 'i2c-%d' % bus)
                self.assertEqual(self.sysfs.port_name(bus), 'sfp%d' % (bus - 43))

    def test_all_buses_present(self):
        for bus in ALL_BUSES:
            self.sysfs.add_bus(bus)
        proc = run_create_optoe_devices(self.sysfs, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, '')
        self.assert_devices_on(ALL_BUSES)

    def test_buses_that_appear_late_get_devices(self):
        # What sw1 showed: when the script ran, i2c-27 onwards existed and
        # i2c-12..26 did not yet.  They turn up half a second later.
        late = range(12, 27)
        for bus in ALL_BUSES:
            if bus not in late:
                self.sysfs.add_bus(bus)

        def add_late_buses():
            time.sleep(0.5)
            for bus in late:
                self.sysfs.add_bus(bus)

        t = threading.Thread(target=add_late_buses)
        t.start()
        proc = run_create_optoe_devices(self.sysfs, timeout=10)
        t.join()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, '', 'no bus should be reported missing')
        self.assert_devices_on(ALL_BUSES)

    def test_timeout_is_bounded_and_names_the_missing_buses(self):
        never = (14, 44)
        for bus in ALL_BUSES:
            if bus not in never:
                self.sysfs.add_bus(bus)
        started = time.monotonic()
        proc = run_create_optoe_devices(self.sysfs, timeout=1)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertLess(elapsed, 5.0, 'the wait must give up after the timeout')
        self.assertIn('timed out after 1s waiting for i2c buses: i2c-14 i2c-44', proc.stderr)
        # The ports whose buses did appear are still set up.
        self.assert_devices_on([b for b in ALL_BUSES if b not in never])
        for bus in never:
            self.assertIsNone(self.sysfs.new_device(bus))

    def test_start_loads_mux_driver_before_fpga_i2c_adapters(self):
        # With cls_pca954x registered first, the pca9548 clients bind (and
        # register their buses) inside "modprobe fpga_i2c_ocores".
        with open(INIT_SCRIPT) as f:
            script = f.read()
        start = script[script.index('\nstart)'):script.index('\nstop)')]
        self.assertLess(start.index('modprobe cls-i2c-mux-pca954x'),
                        start.index('modprobe fpga_i2c_ocores'))
        self.assertLess(start.index('modprobe fpga_i2c_ocores'),
                        start.index('create_optoe_devices'))


if __name__ == '__main__':
    unittest.main()
