"""Silverstone-X must use the SONiC kernel's optoe driver (issue #27).

The platform scripts bind transceiver EEPROMs to the i2c device classes
optoe1 (SFF-8436/8636 QSFP), optoe2 (SFF-8472 SFP) and optoe3 (CMIS
QSFP-DD/OSFP, with page and bank select).  The SONiC kernel ships an
optoe.ko that handles all three (CONFIG_EEPROM_OPTOE=m, see
patches-sonic/driver-support-optoe*.patch in sonic-linux-kernel).

The platform package used to build and install its own copy of optoe.c,
written before optoe3 existed, into /lib/modules/<kver>/extra.  With
Debian's default depmod configuration no search order is defined between
extra/ and kernel/, so the module that wins is the one the directory scan
happens to return first.  On the switch the package copy won, "echo optoe3
0x50 > new_device" found no driver with an i2c:optoe3 alias, and QSFP-DD
modules could not be read.

These tests run without hardware and fail on the old tree.
"""

import os
import re
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PLATFORM_DIR = os.path.dirname(TESTS_DIR)                      # silverstone-x/
PACKAGE_DIR = os.path.dirname(PLATFORM_DIR)                     # sonic-platform-modules-cel/
REPO_DIR = os.path.abspath(os.path.join(PACKAGE_DIR, '..', '..', '..'))

MODULES_DIR = os.path.join(PLATFORM_DIR, 'modules')
MAKEFILE = os.path.join(MODULES_DIR, 'Makefile')
INIT_SCRIPT = os.path.join(PACKAGE_DIR, 'debian', 'platform-modules-silverstone-x.init')
SFP_PY = os.path.join(REPO_DIR, 'device', 'celestica', 'x86_64-cel_silverstone-x-r0',
                      'sonic_platform', 'sfp.py')

# Device classes the SONiC kernel's optoe.ko binds (MODULE_DEVICE_TABLE in
# drivers/misc/eeprom/optoe.c after driver-support-optoe-QSFP_DD.patch).
KERNEL_OPTOE_CLASSES = {'optoe1', 'optoe2', 'optoe3'}

NEW_DEVICE_RE = re.compile(r'echo\s+(optoe\d)\s+0x50\s*>')
I2C_ID_RE = re.compile(r'\{\s*"(optoe\d)"\s*,')


def read(path):
    # some module sources carry Latin-1 copyright signs
    with open(path, encoding='utf-8', errors='replace') as f:
        return f.read()


def makefile_objects():
    """Object files listed in obj-m of the platform modules Makefile."""
    objs = []
    for line in read(MAKEFILE).splitlines():
        m = re.match(r'\s*obj-m\s*[:+]?=\s*(.*)', line)
        if m:
            objs.extend(m.group(1).split())
    return objs


def requested_classes():
    """optoe classes the init script and sfp.py ask the kernel to bind."""
    classes = set()
    for path in (INIT_SCRIPT, SFP_PY):
        classes.update(NEW_DEVICE_RE.findall(read(path)))
    return classes


def private_optoe_sources():
    """modules/*.c files that register optoe i2c device ids, with the ids."""
    found = {}
    for name in sorted(os.listdir(MODULES_DIR)):
        if not name.endswith('.c'):
            continue
        ids = set(I2C_ID_RE.findall(read(os.path.join(MODULES_DIR, name))))
        if ids:
            found[name] = ids
    return found


class OptoeDriverTest(unittest.TestCase):

    def test_scripts_request_classes_the_kernel_driver_binds(self):
        requested = requested_classes()
        self.assertIn('optoe3', requested,
                      'sfp.py is expected to bind QSFP-DD modules to optoe3')
        self.assertLessEqual(requested, KERNEL_OPTOE_CLASSES,
                             'scripts request an optoe class the kernel driver does not bind')

    def test_package_does_not_build_its_own_optoe(self):
        objs = makefile_objects()
        self.assertTrue(objs, 'no obj-m found in %s' % MAKEFILE)
        self.assertNotIn('optoe.o', objs,
                         'platform-modules-silverstone-x builds a private optoe.ko that '
                         'can shadow the kernel optoe.ko in /lib/modules/<kver>/extra')

    def test_no_private_optoe_source_missing_requested_classes(self):
        # A private copy of the driver is only tolerable if it binds every
        # class the platform scripts request; the old copy lacked optoe3.
        for name, ids in private_optoe_sources().items():
            missing = requested_classes() - ids
            self.assertFalse(
                missing,
                '%s registers %s but the platform scripts also request %s'
                % (name, sorted(ids), sorted(missing)))

    def test_init_script_loads_optoe_before_instantiating_devices(self):
        script = read(INIT_SCRIPT)
        self.assertLess(script.index('modprobe optoe'), script.index('echo optoe1 0x50'))


if __name__ == '__main__':
    unittest.main()
