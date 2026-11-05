"""Check the patched XGS opennsl-modules init script and install list.

platform/broadcom/saibcm-modules is the upstream sdk-6.5.35-xgs submodule;
platform/broadcom/saibcm-modules.patch is applied by slave.mk before the deb
is built. This applies the series to a copy of debian/ and checks, without
loading modules or touching /dev, that linux-bcm-genl (which provides
/proc/bcm/genl/psample for sFlow on classic XGS such as Trident 3, #134) is
installed and loaded after its dependencies and removed before them.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUBMODULE = ROOT / 'platform/broadcom/saibcm-modules'
PATCHES = ROOT / 'platform/broadcom/saibcm-modules.patch'


class XgsModuleOrderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (SUBMODULE / 'debian/opennsl-modules.init').is_file():
            raise unittest.SkipTest(
                'platform/broadcom/saibcm-modules is not checked out')
        cls.tmp = tempfile.TemporaryDirectory()
        src = Path(cls.tmp.name) / 'saibcm-modules'
        src.mkdir()
        shutil.copytree(SUBMODULE / 'debian', src / 'debian')
        series = (PATCHES / 'series').read_text().split()
        cls.assertTrue(series, 'empty quilt series')
        for name in series:
            subprocess.run(
                ['patch', '-p1', '--forward', '--no-backup-if-mismatch',
                 '-i', str(PATCHES / name)],
                cwd=src, check=True, capture_output=True, text=True)
        cls.init = (src / 'debian/opennsl-modules.init').read_text()
        cls.install = (src / 'debian/opennsl-modules.install').read_text()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def commands(self, ltsw, function):
        # Extract definitions only: never run init dispatch, platform reads or mknod.
        definitions, marker, _ = self.init.partition('case "$1" in')
        self.assertTrue(marker, 'init dispatch boundary missing')
        mock = '''
modprobe() { printf 'modprobe'; printf ' %s' "$@"; printf '\\n'; }
rmmod() { printf 'rmmod'; printf ' %s' "$@"; printf '\\n'; }
'''
        result = subprocess.run(
            ['bash', '-eu'], input=definitions + mock +
            f'\nis_ltsw_chip={ltsw}\nrx_buffer_size=16384\ndmasize=64M\n'
            f'usemsi=1\nuse_napi=0\n{function}\n',
            text=True, capture_output=True, check=True)
        return [line.split() for line in result.stdout.splitlines()]

    def check_dependencies(self, ltsw):
        if ltsw:
            dependencies = {
                'linux_ngknet': ['linux_ngbde'],
                'linux_ngknetcb': ['linux_ngknet'],
                'linux_bcmgenl': ['linux_ngknet', 'psample'],
            }
        else:
            dependencies = {
                'linux-user-bde': ['linux-kernel-bde'],
                'linux-bcm-knet': ['linux-kernel-bde'],
                'linux-knet-cb': ['linux-bcm-knet', 'psample'],
                'linux-bcm-genl': ['linux-bcm-knet', 'psample'],
            }
        start = self.commands(ltsw, 'load_kernel_modules')
        stop = self.commands(ltsw, 'remove_kernel_modules')
        loaded = set()
        for command, module, *params in start:
            self.assertEqual(command, 'modprobe')
            self.assertNotIn(module, loaded)
            self.assertTrue(set(dependencies.get(module, [])) <= loaded,
                            f'{module} would autoload an unconfigured dependency')
            loaded.add(module)
        for command, module, *params in stop:
            self.assertEqual(command, 'rmmod')
            self.assertIn(module, loaded)
            consumers = [m for m in loaded if module in dependencies.get(m, [])]
            self.assertFalse(consumers, f'{module} still used by {consumers}')
            loaded.remove(module)
        self.assertFalse(loaded)
        return {module: params for _, module, *params in start}

    def test_classic_loads_bcm_genl_after_knet(self):
        params = self.check_dependencies(0)
        self.assertIn('linux-bcm-genl', params)
        self.assertEqual(params['linux-bcm-knet'], [
            'use_rx_skb=1', 'rx_buffer_size=9238', 'debug=0x5020',
            'default_mtu=9100', 'use_napi=0'])
        self.assertEqual(params['linux-kernel-bde'], [
            'dmasize=64M', 'maxpayload=128', 'debug=4', 'dma_debug=1', 'usemsi=1'])

    def test_ltsw_unchanged(self):
        params = self.check_dependencies(1)
        self.assertIn('linux_bcmgenl', params)
        self.assertEqual(params['linux_ngknet'], ['rx_buffer_size=16384'])

    def test_install_list_ships_both_genl_modules(self):
        modules = {Path(line.split()[0]).name
                   for line in self.install.splitlines() if line.strip()}
        self.assertIn('linux-bcm-genl.ko', modules)
        self.assertIn('linux_bcmgenl.ko', modules)
        self.assertIn('linux-knet-cb.ko', modules)


if __name__ == '__main__':
    unittest.main()
