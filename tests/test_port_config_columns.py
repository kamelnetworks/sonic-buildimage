"""Every column of a port_config.ini ends up as a field of the PORT table
(sonic-config-engine's parse_port_config_file copies them verbatim), and
`config reload` validates CONFIG_DB against the YANG models before it loads
anything (sonic-utilities, since sonic-net/sonic-utilities#4521). A column
that sonic-port.yang doesn't know therefore makes every `config reload` abort
with "All Keys are not parsed in PORT ... exceptionList: 'valid_speeds'"
(kamelnetworks/sonic-buildimage#136, Seastone 2).

This checks the port_config.ini files of the release platforms against the
leaves of PORT_LIST in sonic-port.yang. Nothing here needs libyang: the leaf
names are read from the YANG source and the .ini header is parsed like
parse_port_config_file does.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT_YANG = ROOT / 'src/sonic-yang-models/yang-models/sonic-port.yang'

# Platforms of the 202611-pre release (AGENTS.md); the other platforms in
# device/ are not maintained here.
PLATFORMS = [
    'device/celestica/x86_64-cel_seastone_2-r0',
    'device/celestica/x86_64-cel_silverstone-x-r0',
    'device/dell/x86_64-dellemc_s5232f_c3538-r0',
]


def port_list_leaves(yang_text):
    """Names of the leaves and leaf-lists of PORT_LIST in sonic-port.yang."""
    start = yang_text.index('list PORT_LIST')
    start = yang_text.index('{', start)
    depth = 0
    for pos in range(start, len(yang_text)):
        if yang_text[pos] == '{':
            depth += 1
        elif yang_text[pos] == '}':
            depth -= 1
            if depth == 0:
                break
    else:
        raise ValueError('unterminated PORT_LIST block')
    block = yang_text[start:pos]
    return set(re.findall(r'\bleaf(?:-list)?\s+([\w-]+)\s*\{', block))


def port_config_columns(ini_text):
    """The column titles that parse_port_config_file would use for the
    rows of this port_config.ini (the last '# ... name ...' header wins, the
    default is name/lanes/alias/index)."""
    titles = ['name', 'lanes', 'alias', 'index']
    for line in ini_text.splitlines():
        if line.startswith('#') and 'name' in line:
            titles = line.strip('#').split()
    return titles


def port_config_files():
    files = []
    for platform in PLATFORMS:
        files.extend(sorted((ROOT / platform).glob('*/port_config.ini')))
    return files


class TestPortConfigColumns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.leaves = port_list_leaves(PORT_YANG.read_text())

    def test_yang_leaves_are_parsed(self):
        # Guard against the regex silently matching nothing.
        self.assertLessEqual({'name', 'lanes', 'alias', 'index', 'speed', 'fec'},
                             self.leaves)
        self.assertNotIn('valid_speeds', self.leaves)

    def test_release_platforms_have_port_config_files(self):
        self.assertTrue(port_config_files())

    def test_port_config_columns_are_yang_leaves(self):
        for ini in port_config_files():
            with self.subTest(file=str(ini.relative_to(ROOT))):
                columns = port_config_columns(ini.read_text())
                unknown = [c for c in columns if c not in self.leaves]
                self.assertEqual(
                    [], unknown,
                    'column(s) {} of {} are not leaves of PORT_LIST in '
                    'sonic-port.yang, so a CONFIG_DB rendered from it fails '
                    'the YANG validation of config reload (#136)'.format(
                        unknown, ini.relative_to(ROOT)))

    def test_detects_valid_speeds_column(self):
        # The Seastone 2 file before the fix.
        ini = ('# name       lanes      alias    index   speed     valid_speeds\n'
               'Ethernet0    1,2,3,4    Eth1/1   1       100000    100000,40000\n')
        columns = port_config_columns(ini)
        self.assertEqual(['valid_speeds'], [c for c in columns if c not in self.leaves])

    def test_header_default_and_override(self):
        self.assertEqual(['name', 'lanes', 'alias', 'index'],
                         port_config_columns('Ethernet0 1 Eth1 1\n'))
        self.assertEqual(['name', 'lanes', 'alias', 'index', 'speed', 'fec'],
                         port_config_columns('# a comment\n# name lanes alias index speed fec\n'))


if __name__ == '__main__':
    unittest.main()
