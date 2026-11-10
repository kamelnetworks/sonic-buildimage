"""Check the carried db_migrator valid_speeds cleanup (#136) against the pin.

The patch must stay in the series, apply on top of the earlier patches, call
the cleanup from common_migration_ops (which runs on every migration) and leave
upstream's CURRENT_VERSION alone, so that a config migrated by this fork never
records a database version upstream doesn't know. Its behaviour tests are in
the patch (sonic-utilities tests/db_migrator_test.py) and run in the wheel build.
"""
import ast
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PATCH_DIR = ROOT / 'src/sonic-utilities.patch'
SUBMODULE = ROOT / 'src/sonic-utilities'
PATCH = '0004-db_migrator-remove-valid_speeds-from-PORT-entries.patch'
SCRIPT = 'scripts/db_migrator.py'


def run(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def series():
    return [line.split('#', 1)[0].strip()
            for line in (PATCH_DIR / 'series').read_text().splitlines()
            if line.split('#', 1)[0].strip()]


def method(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{name} not found in {SCRIPT}')


def calls(function):
    return {node.func.attr for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}


def current_version(source):
    for node in ast.walk(method(ast.parse(source), '__init__')):
        if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Attribute)
                and node.targets[0].attr == 'CURRENT_VERSION'):
            return node.value.value
    raise AssertionError('CURRENT_VERSION not set')


class DbMigratorValidSpeedsPatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (SUBMODULE / 'setup.py').is_file():
            raise unittest.SkipTest('initialise src/sonic-utilities before running this test')
        pin = run(['git', 'rev-parse', 'HEAD:src/sonic-utilities'], ROOT).stdout.strip()
        actual = run(['git', 'rev-parse', 'HEAD'], SUBMODULE).stdout.strip()
        assert actual == pin, 'sonic-utilities checkout differs from the pin'
        archive = run(['git', 'archive', '--format=tar', 'HEAD'], SUBMODULE).stdout
        with tempfile.TemporaryDirectory(prefix='db-migrator-quilt-') as temporary:
            with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                source.extractall(temporary, filter='data')
            cls.original = (Path(temporary) / SCRIPT).read_text()
            for name in series():
                result = subprocess.run(
                    ['git', 'apply', '--whitespace=nowarn', str(PATCH_DIR / name)],
                    cwd=temporary, capture_output=True, text=True)
                assert result.returncode == 0, f'{name}: {result.stderr}'
            cls.patched = (Path(temporary) / SCRIPT).read_text()
            cls.fixture = (Path(temporary) / 'tests/db_migrator_input/config_db'
                           / 'port_valid_speeds_input.json').is_file()

    def test_patch_is_in_series(self):
        self.assertIn(PATCH, series())

    def test_cleanup_runs_on_every_migration(self):
        tree = ast.parse(self.patched)
        self.assertIn('migrate_port_valid_speeds', calls(method(tree, 'common_migration_ops')))
        cleanup = method(tree, 'migrate_port_valid_speeds')
        self.assertIn('hdel', calls(cleanup))
        self.assertTrue(self.fixture, 'the sonic-utilities test fixture is missing')

    def test_no_fork_only_database_version(self):
        self.assertEqual(current_version(self.patched), current_version(self.original))


if __name__ == '__main__':
    unittest.main()
