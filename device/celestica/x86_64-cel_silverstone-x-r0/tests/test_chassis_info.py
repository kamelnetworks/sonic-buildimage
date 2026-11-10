"""Hardware-free regression for #119 on Silverstone-X: chassis_db_init's reads.

chassis_db_init (sonic-platform-daemons, sonic-chassisd) fills
STATE_DB CHASSIS_INFO|chassis 1 from Chassis.get_serial(), get_model() and
get_revision(). It only tolerates NotImplementedError and TimeoutError, so
Chassis.get_model() calling a Tlv.get_pn() that didn't exist raised
AttributeError and the service EXITED. These tests build a Chassis around the
real Tlv decoder, fed from a TlvInfo image or from the STATE_DB copy, and call
the three getters the way chassis_db_init does.

Needs sonic-platform-common on PYTHONPATH, e.g. from the repository root:

    PYTHONPATH=src/sonic-platform-common python3 -m pytest \
        device/celestica/x86_64-cel_silverstone-x-r0/tests/test_chassis_info.py
"""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_eeprom_nonroot import (FakeSonicV2Connector, eeprom_base, eeprom_module,
                                 fake_io, stub_modules, tlv_blob)

PRODUCT = 'Silverstone-X'
PART_NUMBER = 'R4035-F0001-01'
SERIAL = 'R0123ABC456'
MAC = '00:11:22:33:44:55'
REVISION = '5'  # TLV 0x26, Device Version: a one-byte integer


def load_chassis():
    stubs = {}
    for name, symbol in (
        ('sonic_platform_base.chassis_base', 'ChassisBase'),
        ('sonic_platform.helper', 'APIHelper'),
        ('sonic_platform.fan_drawer', 'FanDrawer'),
    ):
        module = types.ModuleType(name)
        setattr(module, symbol, type(symbol, (), {}))
        stubs[name] = module
    path = Path(__file__).resolve().parents[1] / 'sonic_platform/chassis.py'
    spec = importlib.util.spec_from_file_location('silverstone_chassis', path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module.Chassis


Chassis = load_chassis()


def chassis_db_init_fields(chassis):
    """The CHASSIS_INFO fields as chassis_db_init's provision_db() collects them."""
    def try_get(callback, default='N/A'):
        try:
            value = callback()
        except (NotImplementedError, TimeoutError):
            value = default
        return default if value is None else value

    return {
        'serial': try_get(chassis.get_serial),
        'model': try_get(chassis.get_model),
        'revision': try_get(chassis.get_revision),
    }


class ChassisInfoTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(sys.modules, stub_modules()))
        FakeSonicV2Connector.tables = {}
        cache_root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(eeprom_module, 'CACHE_ROOT', cache_root))

    def make_chassis(self, contents, euid):
        # Skip Chassis.__init__: it probes fans, PSUs and thermals. Only the
        # EEPROM is wired in, with the real Tlv decoder.
        self.enterContext(patch.object(eeprom_base, 'io', fake_io(contents)))
        self.enterContext(patch('os.geteuid', return_value=euid))
        chassis = Chassis.__new__(Chassis)
        chassis._eeprom = eeprom_module.Tlv()
        return chassis

    def test_chassis_db_init_fields_from_eeprom(self):
        image = tlv_blob([(0x21, PRODUCT.encode()),
                          (0x22, PART_NUMBER.encode()),
                          (0x23, SERIAL.encode()),
                          (0x24, bytes.fromhex(MAC.replace(':', ''))),
                          (0x26, bytes([int(REVISION)]))])
        chassis = self.make_chassis(image, euid=0)
        self.assertEqual(chassis_db_init_fields(chassis),
                         {'serial': SERIAL, 'model': PART_NUMBER, 'revision': REVISION})
        self.assertEqual(chassis.get_serial_number(), SERIAL)
        self.assertEqual(chassis.get_base_mac(), MAC)
        self.assertEqual(chassis.get_system_eeprom_info()['0x22'], PART_NUMBER)

    def test_chassis_db_init_fields_from_state_db(self):
        FakeSonicV2Connector.tables = {
            'EEPROM_INFO|State': {'Initialized': '1'},
            'EEPROM_INFO|TlvHeader': {'Id String': 'TlvInfo', 'Version': '1', 'Total Length': '60'},
            'EEPROM_INFO|0x22': {'Name': 'Part Number', 'Len': '14', 'Value': PART_NUMBER},
            'EEPROM_INFO|0x23': {'Name': 'Serial Number', 'Len': '11', 'Value': SERIAL},
            'EEPROM_INFO|0x26': {'Name': 'Device Version', 'Len': '1', 'Value': REVISION},
            'EEPROM_INFO|Checksum': {'Valid': '1'},
        }
        chassis = self.make_chassis(None, euid=1000)
        self.assertEqual(chassis_db_init_fields(chassis),
                         {'serial': SERIAL, 'model': PART_NUMBER, 'revision': REVISION})

    def test_missing_fields_do_not_raise(self):
        image = tlv_blob([(0x23, SERIAL.encode())])
        chassis = self.make_chassis(image, euid=0)
        self.assertEqual(chassis_db_init_fields(chassis),
                         {'serial': SERIAL, 'model': 'Undefined.', 'revision': 'Undefined.'})


if __name__ == '__main__':
    unittest.main()
