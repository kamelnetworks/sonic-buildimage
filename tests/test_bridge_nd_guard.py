"""Run with python3 -m unittest discover -s tests -p test_bridge_nd_guard.py.

Requires Jinja2. Kernel rule loading is covered separately by
sudo bash tests/check_bridge_nd_guard_netns.sh in a disposable builder.
"""
from pathlib import Path
import subprocess
import unittest

import jinja2

ROOT = Path(__file__).parents[1]
RENDERED = jinja2.Environment().from_string(
    (ROOT / 'files/build_templates/docker_image_ctl.j2').read_text()
).render(docker_container_name='swss')
FUNCTION = RENDERED[
    RENDERED.index('function ebtables_config()'):
    RENDERED.index('function getMountPoint()')
].replace('/etc/ebtables.filter.cfg', '/dev/null')


class BridgeFilterStartupTest(unittest.TestCase):
    def run_function(self, settings, fail=''):
        stubs = '''
ebtables-restore() { cat >/dev/null; echo ebtables; [[ "$FAIL" != ebtables ]]; }
nft() { echo "nft $*"; [[ "$FAIL" != nft ]]; }
ip() { echo "ip $*"; }
'''
        return subprocess.run(
            ['bash', '-c', stubs + FUNCTION + '\n' + settings +
             f'; FAIL={fail}; ebtables_config'],
            text=True, capture_output=True,
        )

    def test_host_loads_both_filters(self):
        result = self.run_function('DEV=; NUM_ASIC=1; DATABASE_TYPE=redis')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            'ebtables', 'nft -f /etc/bridge-nd-filter.nft',
        ])

    def test_asic_namespace_loads_both_filters(self):
        result = self.run_function(
            'DEV=0; NUM_ASIC=2; NET_NS=asic0; DATABASE_TYPE=redis'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            'ip netns exec asic0 ebtables-restore',
            'ip netns exec asic0 nft -f /etc/bridge-nd-filter.nft',
        ])

    def test_multi_asic_host_skips_filters(self):
        result = self.run_function('DEV=; NUM_ASIC=2; DATABASE_TYPE=redis')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_loader_failures_propagate(self):
        for loader in ['ebtables', 'nft']:
            with self.subTest(loader=loader):
                result = self.run_function(
                    'DEV=; NUM_ASIC=1; DATABASE_TYPE=redis', fail=loader
                )
                self.assertNotEqual(result.returncode, 0)

    def test_rendered_shell_syntax(self):
        result = subprocess.run(
            ['bash', '-n'], input=RENDERED, text=True, capture_output=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
