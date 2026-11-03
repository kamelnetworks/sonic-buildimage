"""Check module dependency ordering without loading modules or touching /dev."""
import subprocess
import unittest
from pathlib import Path


SCRIPT = (Path(__file__).resolve().parents[1] / 'platform/broadcom/'
          'saibcm-modules-legacy-th/debian/opennsl-modules.init')


class ModuleOrderTest(unittest.TestCase):
    def commands(self, ltsw, function):
        # Extract definitions only: never run init dispatch, platform reads or mknod.
        definitions, marker, _ = SCRIPT.read_text().partition('case "$1" in')
        self.assertTrue(marker, 'init dispatch boundary missing')
        mock = '''
modprobe() { printf 'modprobe'; printf ' %s' "$@"; printf '\\n'; }
rmmod() { printf 'rmmod'; printf ' %s' "$@"; printf '\\n'; }
'''
        result = subprocess.run(
            ['bash', '-eu'], input=definitions + mock +
            f'\nis_ltsw_chip={ltsw}\nrx_buffer_size=16384\ndmasize=64M\nusemsi=1\n{function}\n',
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

    def test_ltsw_dependencies_and_parameters(self):
        params = self.check_dependencies(1)
        self.assertEqual(params['linux_ngknet'], ['rx_buffer_size=16384'])

    def test_classic_dependencies_and_parameters(self):
        params = self.check_dependencies(0)
        self.assertEqual(params['linux-bcm-knet'], [
            'use_rx_skb=1', 'rx_buffer_size=9238', 'debug=0x5020', 'default_mtu=9100'])
        self.assertEqual(params['linux-kernel-bde'], [
            'dmasize=64M', 'maxpayload=128', 'debug=4', 'dma_debug=1', 'usemsi=1'])


if __name__ == '__main__':
    unittest.main()
