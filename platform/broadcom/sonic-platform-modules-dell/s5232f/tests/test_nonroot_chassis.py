"""Hardware-free regression for #62: a non-root caller can build the Chassis.

'show interfaces status' (intfutil -> utilities_common.platform_sfputil_helper
.is_rj45_port()) instantiates Platform().get_chassis() as the invoking user.
Chassis() reads the system EEPROM in its constructor, and the at24 sysfs node
/sys/bus/i2c/devices/i2c-0/0-0050/eeprom is 0600 root, so that read fails
with PermissionError for a normal user. Eeprom() must then use the copy that
syseepromd publishes in STATE_DB instead of raising and killing the command.

Needs sonic-platform-common on PYTHONPATH, e.g. from the repository root:

    PYTHONPATH=src/sonic-platform-common python3 -m pytest \
        platform/broadcom/sonic-platform-modules-dell/s5232f/tests
"""
import binascii
import fnmatch
import importlib.util
import io
import struct
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SYSEEPROM = '/sys/bus/i2c/devices/i2c-0/0-0050/eeprom'
MODULE_DIR = Path(__file__).resolve().parents[1]
IPMIHELPER = MODULE_DIR.parent / 'common' / 'ipmihelper.py'

SERIAL = 'CN0ABCD1234'
MODEL = 'S5232F-ON'
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
    """Stubs for the SONiC runtime packages the platform code imports."""
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


def load_platform():
    """Import the S5232F sonic_platform package from the source tree.

    The Debian build copies common/ipmihelper.py into the package, so do the
    same here. Everything is loaded under patch.dict so nothing leaks into
    sys.modules for other test modules.
    """
    stubs = stub_modules()
    package = types.ModuleType('sonic_platform')
    package.__path__ = [str(MODULE_DIR / 'sonic_platform')]
    stubs['sonic_platform'] = package
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location('sonic_platform.ipmihelper', IPMIHELPER)
        ipmihelper = importlib.util.module_from_spec(spec)
        sys.modules['sonic_platform.ipmihelper'] = ipmihelper
        spec.loader.exec_module(ipmihelper)
        import sonic_platform.chassis
        import sonic_platform.eeprom
        import sonic_platform.sfp
        import sonic_eeprom.eeprom_base
        return (sonic_platform.chassis, sonic_platform.eeprom, sonic_platform.sfp,
                sonic_eeprom.eeprom_base)


chassis_module, eeprom_module, sfp_module, eeprom_base = load_platform()


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
    'EEPROM_INFO|TlvHeader': {'Id String': 'TlvInfo', 'Version': '1', 'Total Length': '70'},
    'EEPROM_INFO|0x21': {'Name': 'Product Name', 'Len': '9', 'Value': MODEL},
    'EEPROM_INFO|0x23': {'Name': 'Serial Number', 'Len': '11', 'Value': SERIAL},
    'EEPROM_INFO|0x24': {'Name': 'Base MAC Address', 'Len': '6', 'Value': MAC},
    'EEPROM_INFO|0xfd': {'Name': 'Vendor Extension', 'Num_vendor_ext': '1',
                         'Len_0': '4', 'Value_0': '0x00 0x01 0x02 0x03'},
    'EEPROM_INFO|Checksum': {'Valid': '1'},
}


class NonRootChassisTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(sys.modules, stub_modules()))
        FakeSonicV2Connector.tables = {}
        # Eeprom() only reads the node it finds; make the probe find it.
        real_exists = eeprom_module.os.path.exists
        self.enterContext(patch.object(
            eeprom_module.os.path, 'exists',
            lambda path: path == SYSEEPROM or real_exists(path)))

    def set_eeprom(self, contents, euid):
        self.enterContext(patch.object(eeprom_base, 'io', fake_io(contents)))
        self.enterContext(patch('os.geteuid', return_value=euid))

    def test_nonroot_eeprom_uses_state_db_copy(self):
        FakeSonicV2Connector.tables = dict(STATE_DB_EEPROM)
        self.set_eeprom(None, euid=1000)
        eeprom = eeprom_module.Eeprom()
        self.assertEqual(eeprom.system_eeprom_info(), {
            '0x21': MODEL, '0x23': SERIAL, '0x24': MAC, '0xFD': '0x00 0x01 0x02 0x03'})
        # The raw accessors have no EEPROM image to parse; they must not crash.
        self.assertEqual(eeprom.serial_number_str(), 'N/A')
        self.assertEqual(eeprom.modelstr(), 'N/A')

    def test_nonroot_eeprom_without_state_db(self):
        self.set_eeprom(None, euid=1000)
        eeprom = eeprom_module.Eeprom()
        self.assertEqual(eeprom.system_eeprom_info(), {})
        self.assertEqual(eeprom.serial_number_str(), 'N/A')

    def test_nonroot_chassis_builds_without_eeprom_access(self):
        """The is_rj45_port() path: Platform().get_chassis() as a normal user."""
        FakeSonicV2Connector.tables = dict(STATE_DB_EEPROM)
        self.set_eeprom(None, euid=1000)
        # Sfp() media probing and optoe re-binding are #101, not this test.
        self.enterContext(patch.object(sfp_module.Sfp, '_initialize_media', Mock(), create=True))
        chassis = chassis_module.Chassis()
        self.assertEqual(chassis.get_system_eeprom_info()['0x23'], SERIAL)
        self.assertEqual(chassis.get_num_sfps(), 34)
        with self.assertRaises(NotImplementedError):
            chassis.get_port_or_cage_type(1)

    def test_root_unreadable_eeprom_still_raises(self):
        self.set_eeprom(None, euid=0)
        with self.assertRaises(RuntimeError):
            eeprom_module.Eeprom()

    def test_readable_eeprom_is_parsed(self):
        image = tlv_blob([(0x21, MODEL.encode()), (0x23, SERIAL.encode()),
                          (0x24, bytes.fromhex(MAC.replace(':', '')))])
        self.set_eeprom(image, euid=0)
        eeprom = eeprom_module.Eeprom()
        self.assertEqual(eeprom.serial_number_str(), SERIAL)
        self.assertEqual(eeprom.modelstr(), MODEL)
        self.assertEqual(eeprom.base_mac_addr(None), MAC)
        self.assertEqual(eeprom.system_eeprom_info()['0x23'], SERIAL)


if __name__ == '__main__':
    unittest.main()
