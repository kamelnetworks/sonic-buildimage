"""Unit tests for files/image_config/backend_acl/backend_acl.py (#150).

The script runs on every boot. It used to call ``sonic-cfggen -m`` unconditionally,
which logs a traceback on switches that have no /etc/sonic/minigraph.xml.
"""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).parents[1] / "files" / "image_config" / "backend_acl" / "backend_acl.py"


def load_module():
    # swsscommon is only available on a switch / in the runtime images; the
    # device-type logic under test never touches it.
    swsscommon = mock.MagicMock()
    with mock.patch.dict(sys.modules, {"swsscommon": swsscommon,
                                       "swsscommon.swsscommon": swsscommon.swsscommon}):
        spec = importlib.util.spec_from_file_location("backend_acl", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class DeviceTypeSourceTest(unittest.TestCase):

    def setUp(self):
        self.backend_acl = load_module()
        patcher = mock.patch.object(self.backend_acl, "log_info")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_minigraph_when_present(self):
        with mock.patch.object(self.backend_acl.os.path, "isfile", return_value=True), \
                mock.patch.object(self.backend_acl, "run_command",
                                  return_value="BackEndToRRouter") as run_command:
            self.assertEqual(self.backend_acl._get_device_type(), "BackEndToRRouter")
        run_command.assert_called_once_with(
            [self.backend_acl.SONIC_CFGGEN_PATH, "-m", "-v", "DEVICE_METADATA.localhost.type"],
            return_cmd=True)

    def test_reads_config_db_without_minigraph(self):
        with mock.patch.object(self.backend_acl.os.path, "isfile", return_value=False) as isfile, \
                mock.patch.object(self.backend_acl, "run_command",
                                  return_value="LeafRouter") as run_command:
            self.assertEqual(self.backend_acl._get_device_type(), "LeafRouter")
        isfile.assert_called_once_with("/etc/sonic/minigraph.xml")
        run_command.assert_called_once_with(
            [self.backend_acl.SONIC_CFGGEN_PATH, "-d", "-v", "DEVICE_METADATA.localhost.type"],
            return_cmd=True)
        self.assertNotIn("-m", run_command.call_args[0][0])

    def test_main_skips_non_backend_tor(self):
        with mock.patch.object(self.backend_acl, "_get_device_type", return_value="LeafRouter"), \
                mock.patch.object(self.backend_acl, "load_backend_acl") as load:
            self.backend_acl.main()
        load.assert_not_called()

    def test_main_loads_on_backend_tor(self):
        with mock.patch.object(self.backend_acl, "_get_device_type", return_value="BackEndToRRouter"), \
                mock.patch.object(self.backend_acl, "load_backend_acl") as load:
            self.backend_acl.main()
        load.assert_called_once_with("BackEndToRRouter")


if __name__ == "__main__":
    unittest.main()
