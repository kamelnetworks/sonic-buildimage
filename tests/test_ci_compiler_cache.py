"""Exercise the CI compiler-cache configuration without containers or compiling."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/sonic-build.yml').read_text())
CONFIGURE = next(step['run'] for step in WORKFLOW['jobs']['build']['steps']
                 if step.get('name') == 'Configure (rules/config.user)')


class CompilerCacheConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'rules').mkdir()
        (self.root / 'persistent').mkdir()
        self.env = dict(os.environ, CACHE_MODE_INPUT='rwcache',
                        CACHE_SOURCE_INPUT=str(self.root / 'persistent'),
                        COMPILER_CACHE_INPUT='true')

    def configure(self):
        result = subprocess.run(['bash', '-e', '-c', CONFIGURE], cwd=self.root,
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return (self.root / 'rules/config.user').read_text(), result

    def test_persistent_override_survives_slave_default(self):
        config, _ = self.configure()
        self.assertIn('SONIC_CONFIG_USE_CCACHE = y', config)
        self.assertIn('export CCACHE_MAXSIZE = 5G', config)
        # Use the real default assignment to verify GNU make's precedence;
        # no cache directory is created and no build target is executed.
        default = next(line for line in (ROOT / 'slave.mk').read_text().splitlines()
                       if line.startswith('CCACHE_DIR :='))
        makefile = self.root / 'probe.mk'
        makefile.write_text('include rules/config.user\n' + default + '\n'
                            'all:\n\t@echo $(CCACHE_DIR)\n')
        result = subprocess.run(['make', '-s', '-f', str(makefile),
                                 'BLDENV=trixie', 'CONFIGURED_ARCH=amd64'],
                                cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f'/dpkg_cache/.ccache-{os.getuid()}/trixie/amd64')

    def test_disabled_by_default(self):
        self.env['COMPILER_CACHE_INPUT'] = 'false'
        config, _ = self.configure()
        self.assertNotIn('SONIC_CONFIG_USE_CCACHE', config)
        trigger = WORKFLOW.get('on', WORKFLOW.get(True))
        self.assertFalse(trigger['workflow_dispatch']['inputs']['compiler_cache']['default'])

    def test_no_package_cache_disables_compiler_cache(self):
        self.env['CACHE_MODE_INPUT'] = 'none'
        config, result = self.configure()
        self.assertNotIn('SONIC_CONFIG_USE_CCACHE', config)
        self.assertNotIn('CCACHE_DIR', config)
        self.assertIn('compiler cache disabled', result.stderr)

    def test_unavailable_cache_disables_compiler_cache(self):
        self.env['CACHE_SOURCE_INPUT'] = str(self.root / 'missing')
        config, result = self.configure()
        self.assertIn('SONIC_DPKG_CACHE_METHOD = none', config)
        self.assertNotIn('SONIC_CONFIG_USE_CCACHE', config)
        self.assertIn('compiler cache disabled', result.stderr)

    def test_existing_user_config_preserved(self):
        path = self.root / 'rules/config.user'
        path.write_text('USER_SETTING = retain\n')
        config, _ = self.configure()
        self.assertEqual(config, 'USER_SETTING = retain\n')

    def test_enabled_cache_fails_fast_when_ccache_is_missing(self):
        self.configure()
        source = (ROOT / 'slave.mk').read_text()
        fragment = source[source.index('CCACHE_ENV =\n'):
                          source.index('ifeq ($(SONIC_BUILD_JOBS),)')]
        makefile = self.root / 'missing-ccache.mk'
        makefile.write_text('include rules/config.user\n' + fragment +
                            '\nall:\n\t@true\n')
        empty_path = self.root / 'empty-path'
        empty_path.mkdir()
        result = subprocess.run(
            ['/usr/bin/make', '-s', '-f', str(makefile), 'SHELL=/bin/sh'],
            cwd=self.root, env=dict(os.environ, PATH=str(empty_path)),
            text=True, capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("'ccache' is not installed", result.stderr)
