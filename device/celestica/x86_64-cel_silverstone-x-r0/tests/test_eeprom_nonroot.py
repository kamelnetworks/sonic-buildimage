"""Hardware-free regression for #62 on Silverstone-X: Tlv() for non-root users.

'show interfaces status' (intfutil -> is_rj45_port()) builds the platform
Chassis as the invoking user, and Chassis() constructs Tlv(), which reads the
system EEPROM. The at24 node /sys/bus/i2c/devices/0-0056/eeprom is 0600 root,
so the read fails with PermissionError for a normal user. Tlv() must then use
the copy syseepromd publishes in STATE_DB instead of raising.

Needs sonic-platform-common on PYTHONPATH, e.g. from the repository root:

    PYTHONPATH=src/sonic-platform-common python3 -m pytest \
        device/celestica/x86_64-cel_silverstone-x-r0/tests/test_eeprom_nonroot.py
"""
import binascii
import fnmatch
import importlib.util
import io
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SYSEEPROM = '/sys/bus/i2c/devices/0-0056/eeprom'
SERIAL = 'R0123ABC456'
MAC = '00:11:22:33:44:55'


class FakeSonicV2Connector:
    """In-memory stand-in for swsscommon's STATE_DB connector."""
    STATE_DB = 'STATE_DB'
    tables = {}

    def connect(self, db_name, retry_on=True):
        pass

    def get(self, db_name, key, field):
        return self.tables.get(key, {}).get(field)

    def keys(self, db_name, pattern='*'):
        return [k for k in self.tables if fnmatch.fnmatchcase(k, pattern)] or None


def stub_modules():
    stubs = {}
    sonic_py_common = types.ModuleType('sonic_py_common')
    for name in ('logger', 'device_info', 'syslogger'):
        setattr(sonic_py_common, name, Mock())
        stubs['sonic_py_common.' + name] = getattr(sonic_py_common, name)
    # No cpo.json on these platforms: ChassisBase.__init__ must skip CPO setup.
    sonic_py_common.device_info.get_cpo_data.return_value = None
    stubs['sonic_py_common'] = sonic_py_common
    swsscommon = types.ModuleType('swsscommon')
    swsscommon.swsscommon = types.ModuleType('swsscommon.swsscommon')
    swsscommon.swsscommon.SonicV2Connector = FakeSonicV2Connector
    stubs['swsscommon'] = swsscommon
    stubs['swsscommon.swsscommon'] = swsscommon.swsscommon
    return stubs


def load_eeprom():
    path = Path(__file__).resolve().parents[1] / 'sonic_platform/eeprom.py'
    spec = importlib.util.spec_from_file_location('silverstone_eeprom', path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stub_modules()):
        spec.loader.exec_module(module)
        from sonic_platform_base.sonic_eeprom import eeprom_base
    return module, eeprom_base


eeprom_module, eeprom_base = load_eeprom()


def tlv_blob(tlvs):
    """A valid TlvInfo EEPROM image holding the given (code, bytes) fields."""
    body = b''.join(struct.pack('BB', code, len(value)) + value for code, value in tlvs)
    body += b'\xfe\x04'
    header = b'TlvInfo\x00\x01' + struct.pack('>H', len(body) + 4)
    crc = binascii.crc32(header + body) & 0xffffffff
    return header + body + struct.pack('>I', crc)


def fake_io(contents):
    """An io replacement whose open() serves SYSEEPROM from memory.

    contents=None makes it root-only: open() raises PermissionError like the
    0600 at24 node does for a normal user.
    """
    real_open = io.open

    def fake_open(path, *args, **kwargs):
        if str(path) != SYSEEPROM:
            return real_open(path, *args, **kwargs)
        if contents is None:
            raise PermissionError(13, 'Permission denied', path)
        return io.BytesIO(contents)
    return types.SimpleNamespace(open=fake_open)


STATE_DB_EEPROM = {
    'EEPROM_INFO|State': {'Initialized': '1'},
    'EEPROM_INFO|TlvHeader': {'Id String': 'TlvInfo', 'Version': '1', 'Total Length': '40'},
    'EEPROM_INFO|0x23': {'Name': 'Serial Number', 'Len': '11', 'Value': SERIAL},
    'EEPROM_INFO|0x24': {'Name': 'Base MAC Address', 'Len': '6', 'Value': MAC},
    'EEPROM_INFO|Checksum': {'Valid': '1'},
}


class NonRootTlvTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(sys.modules, stub_modules()))
        FakeSonicV2Connector.tables = {}
        self.stdout = sys.stdout
        # Keep the deprecated decode-syseeprom cache out of /var/cache.
        cache_root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(eeprom_module, 'CACHE_ROOT', cache_root))

    def set_eeprom(self, contents, euid):
        self.enterContext(patch.object(eeprom_base, 'io', fake_io(contents)))
        self.enterContext(patch('os.geteuid', return_value=euid))

    def test_nonroot_uses_state_db_copy(self):
        FakeSonicV2Connector.tables = dict(STATE_DB_EEPROM)
        self.set_eeprom(None, euid=1000)
        tlv = eeprom_module.Tlv()
        self.assertEqual(tlv.get_serial(), SERIAL)
        self.assertEqual(tlv.get_mac(), MAC)
        # _load_eeprom() captures stdout while it runs; it must hand it back.
        self.assertIs(sys.stdout, self.stdout)

    def test_nonroot_without_state_db(self):
        self.set_eeprom(None, euid=1000)
        tlv = eeprom_module.Tlv()
        self.assertEqual(tlv.get_serial(), 'Undefined.')
        self.assertEqual(tlv.get_eeprom(), {})

    def test_root_unreadable_eeprom_still_raises(self):
        self.set_eeprom(None, euid=0)
        with self.assertRaises(IOError):
            eeprom_module.Tlv()

    def test_root_reads_hardware(self):
        image = tlv_blob([(0x23, SERIAL.encode()), (0x24, bytes.fromhex(MAC.replace(':', '')))])
        self.set_eeprom(image, euid=0)
        tlv = eeprom_module.Tlv()
        self.assertEqual(tlv.get_serial(), SERIAL)
        self.assertEqual(tlv.get_mac(), MAC)


if __name__ == '__main__':
    unittest.main()
