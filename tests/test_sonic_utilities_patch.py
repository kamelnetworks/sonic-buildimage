"""Check the sonic-utilities quilt series (src/sonic-utilities.patch).

slave.mk applies src/<module>.patch/series with quilt before building the
module's wheel, so a stale series breaks the image build hours in. This test
applies the series to the pinned src/sonic-utilities checkout (no build) and
checks the behaviour the patches add:

  - 0001: 'config ntp add' accepts a hostname for a server association, as
    sonic-ntp.yang (NTP_SERVER keyed on inet:host) allows (#129). The
    validator, utilities_common.cli.is_hostname(), is exercised here without
    importing sonic-utilities (that needs swsscommon).
  - 0005: 'config interface breakout' gives the new ports the mtu and fec of
    the ports they replace (#197). ConfigMgmtDPB._inheritPortAttrs() is run
    here against a stub data tree.

Needs git. Run with: python3 -m pytest tests/test_sonic_utilities_patch.py
"""
import ast
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATCH_DIR = ROOT / 'src/sonic-utilities.patch'
SUBMODULE = ROOT / 'src/sonic-utilities'


def run(cmd, cwd, **kwargs):
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=True, **kwargs)


class SonicUtilitiesPatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('git'):
            raise unittest.SkipTest('git not found')
        if not (SUBMODULE / 'setup.py').is_file():
            raise unittest.SkipTest(
                f'{SUBMODULE.relative_to(ROOT)} is not checked out '
                '(scripts/run-fork-unit-tests.sh initialises it)')
        cls.series = [line.strip() for line in (PATCH_DIR / 'series').read_text().splitlines()
                      if line.strip() and not line.startswith('#')]
        # The files each patch touches, from its 'diff --git a/X b/X' headers.
        cls.touched = {}
        for name in cls.series:
            cls.touched[name] = re.findall(r'^diff --git a/(\S+) b/\1$', (PATCH_DIR / name).read_text(),
                                           re.MULTILINE)
        # Export the pinned versions of the touched files and apply the whole
        # series to them, like slave.mk's 'quilt push -a' does in the build.
        cls.tmp = Path(tempfile.mkdtemp(prefix='sonic-utilities-patch-'))
        for files in cls.touched.values():
            for f in files:
                # A file a patch creates has no pinned version to export.
                if subprocess.run(['git', 'cat-file', '-e', f'HEAD:{f}'], cwd=SUBMODULE,
                                  capture_output=True).returncode != 0:
                    continue
                target = cls.tmp / f
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(run(['git', 'show', f'HEAD:{f}'], SUBMODULE).stdout)
        for name in cls.series:
            run(['git', 'apply', '--whitespace=nowarn', str(PATCH_DIR / name)], cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_series_matches_directory(self):
        self.assertTrue(self.series, 'series is empty')
        patches = sorted(p.name for p in PATCH_DIR.glob('*.patch'))
        self.assertEqual(sorted(self.series), patches, 'series and *.patch files differ')
        for name in self.series:
            self.assertTrue(self.touched[name], f'{name} has no diff headers')

    def test_series_applies_to_pinned_submodule(self):
        # The direct check against the checkout, independent of the export above.
        for name in self.series:
            run(['git', 'apply', '--check', '--whitespace=nowarn', str(PATCH_DIR / name)], SUBMODULE)

    def patched(self, path):
        return (self.tmp / path).read_text()

    def extract_function(self, path, name):
        """Compile one top-level function of a patched file, without importing it."""
        module = ast.parse(self.patched(path))
        nodes = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == name]
        self.assertEqual(len(nodes), 1, f'{name} not found once in {path}')
        namespace = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), path, 'exec'), namespace)
        return namespace[name]

    def extract_method(self, path, cls, name):
        """Compile one method of a class in a patched file, without importing it."""
        module = ast.parse(self.patched(path))
        classes = [n for n in module.body if isinstance(n, ast.ClassDef) and n.name == cls]
        self.assertEqual(len(classes), 1, f'{cls} not found once in {path}')
        nodes = [n for n in classes[0].body if isinstance(n, ast.FunctionDef) and n.name == name]
        self.assertEqual(len(nodes), 1, f'{cls}.{name} not found once in {path}')
        namespace = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), path, 'exec'), namespace)
        return namespace[name]

    def test_breakout_inherits_before_deleting(self):
        main = self.patched('config/config_mgmt.py')
        start = main.index('    def breakOutPort(')
        body = main[start:main.index('\n    def ', start + 1)]
        self.assertLess(body.index('self._inheritPortAttrs('), body.index('self._deletePorts('))
        self.assertIn('config_dpb_inherit_test.py',
                      ' '.join(self.touched['0005-config-interface-breakout-new-ports-inherit-mtu-and-fec.patch']))

    def test_inherit_port_attrs(self):
        inherit = self.extract_method('config/config_mgmt.py', 'ConfigMgmtDPB', '_inheritPortAttrs')

        class Dpb:
            def __init__(self, ports):
                self.sy = type('Sy', (), {'getData': lambda _: {'PORT': ports}})()

            def sysLog(self, **kwargs):
                pass

        def run(old, new, delPorts=None):
            portJson = {'PORT': new}
            out = inherit(Dpb(old), delPorts=delPorts or list(old), portJson=portJson)
            self.assertEqual(portJson, {'PORT': new}, 'portJson was modified in place')
            return out['PORT']

        # 1x100G -> 4x25G: the children get the parent's mtu and fec
        children = {f'Ethernet{i}': {'lanes': str(65 + i), 'speed': '25000'} for i in range(4)}
        out = run({'Ethernet0': {'lanes': '65,66,67,68', 'mtu': '9216', 'fec': 'rs'}}, children)
        self.assertEqual({(e['mtu'], e['fec']) for e in out.values()}, {('9216', 'rs')})
        self.assertEqual(sorted(out), sorted(children))
        # 4x25G -> 1x100G: highest mtu, first active fec in lane order
        old = {'Ethernet3': {'lanes': '68', 'fec': 'fc'}, 'Ethernet2': {'lanes': '67', 'fec': 'rs', 'mtu': '1500'},
               'Ethernet1': {'lanes': '66', 'mtu': 9216}, 'Ethernet0': {'lanes': '65', 'fec': 'none'}}
        out = run(old, {'Ethernet0': {'lanes': '65,66,67,68', 'speed': '100000'}})
        self.assertEqual(out['Ethernet0'], {'lanes': '65,66,67,68', 'speed': '100000', 'mtu': '9216', 'fec': 'rs'})
        # nothing set: no mtu, fec none
        out = run({'Ethernet0': {'lanes': '65,66,67,68'}}, children)
        self.assertEqual({tuple(sorted(e)) for e in out.values()}, {('fec', 'lanes', 'speed')})
        self.assertEqual({e['fec'] for e in out.values()}, {'none'})
        # 400G -> 4x100G (Silverstone-X, 50G PAM4 lanes): platform rs stays without an active fec
        pam4 = {f'Ethernet{i}': {'lanes': f'{i + 1},{i + 2}', 'speed': '100000', 'fec': 'rs'} for i in range(0, 8, 2)}
        out = run({'Ethernet0': {'lanes': '1,2,3,4,5,6,7,8', 'fec': 'none', 'mtu': '9100'}}, pam4)
        self.assertEqual({(e['mtu'], e['fec']) for e in out.values()}, {('9100', 'rs')})

    def test_ntp_add_uses_hostname_validation(self):
        main = self.patched('config/main.py')
        start = main.index('def add_ntp_server(')
        body = main[start:main.index('@ntp.command', start)]
        self.assertIn('clicommon.is_hostname(ntp_ip_address)', body)
        self.assertNotIn('association_type != "pool"', body, 'the pool-only hostname check is back')

    def test_is_hostname(self):
        is_hostname = self.extract_function('utilities_common/cli.py', 'is_hostname')
        for value in ['ntp.sonix.network', 'ntp.example.com.', 'ntp-1', 'pool.ntp.org',
                      '_ntp._udp.example.com', 'a' * 63 + '.example.com', '10.10.10.4', '0']:
            self.assertTrue(is_hostname(value), value)
        for value in ['', None, '.', '-ntp.example.com', 'ntp..example.com', 'ntp.example.com/',
                      'ntp example.com', 'ntp,example.com', 'a' * 64 + '.example.com',
                      '.'.join(['a' * 50] * 5), '2001:db8::1', 'ntp.example.com\n']:
            self.assertFalse(is_hostname(value), repr(value))


if __name__ == '__main__':
    unittest.main()
