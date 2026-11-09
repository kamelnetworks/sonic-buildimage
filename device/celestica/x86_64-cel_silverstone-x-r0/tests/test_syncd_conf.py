"""Hardware-free regression for #60: Silverstone-X syncd runs with -a.

The Teralynx SAI segfaults during SaiDiscovery unless syncd runs with
--enableAttrVersionCheck (-a). The platform's syncd.conf adds it. These tests
run config_syncd() from the pinned sonic-sairedis syncd_init_common.sh, with
its helpers stubbed, against this platform directory and against a Trident 3
platform, so they also catch the hook being dropped or moved upstream.
"""
from pathlib import Path
import shlex
import subprocess
import unittest

PLATFORM = Path(__file__).resolve().parents[1]
ROOT = PLATFORM.parents[2]
SCRIPT = ROOT / 'src/sonic-sairedis/syncd/scripts/syncd_init_common.sh'
OTHER = ROOT / 'device/dell/x86_64-dellemc_s5232f_c3538-r0'


def config_syncd_source():
    """Return the config_syncd() definition from the pinned script."""
    lines = SCRIPT.read_text().splitlines()
    start = lines.index('config_syncd()')
    end = lines.index('}', start)
    return '\n'.join(lines[start:end + 1])


def run_config_syncd(asic, platform_dir, cmd_args='-u'):
    script = '\n'.join((
        'PLATFORM_DIR=' + shlex.quote(str(platform_dir)),
        'HWSKU_DIR=/nonexistent',
        'SONIC_ASIC_TYPE=' + shlex.quote(asic),
        'ENABLE_SAITHRIFT=0',
        'CMD_ARGS=' + shlex.quote(cmd_args),
        'config_syncd_marvell_teralynx() { CMD_ARGS+=" -p $HWSKU_DIR/sai.profile"; }',
        'config_syncd_bcm() { CMD_ARGS+=" -p $HWSKU_DIR/sai.profile"; }',
        'check_warm_boot() { :; }',
        'cleanup_stale_flow_dump_files() { :; }',
        'set_watchdog_timeout() { :; }',
        'set_start_type() { :; }',
        config_syncd_source(),
        'config_syncd',
        'printf "%s\\n" "$CMD_ARGS"',
    ))
    result = subprocess.run(['bash', '-c', script], capture_output=True, text=True, check=True)
    return result.stdout.split()


class SyncdConfTest(unittest.TestCase):
    def test_platform_is_teralynx(self):
        self.assertEqual((PLATFORM / 'platform_asic').read_text().strip(), 'marvell-teralynx')

    def test_pinned_script_sources_platform_hook(self):
        text = SCRIPT.read_text()
        self.assertIn('PLATFORM_DIR=/usr/share/sonic/platform\n', text)
        self.assertIn('. $PLATFORM_DIR/syncd.conf', config_syncd_source())

    def test_hook_parses(self):
        subprocess.run(['bash', '-n', str(PLATFORM / 'syncd.conf')], check=True)

    def test_silverstone_x_adds_attr_version_check(self):
        args = run_config_syncd('marvell-teralynx', PLATFORM)
        self.assertEqual(args.count('-a'), 1, args)
        self.assertEqual(args[-1], '-a')
        self.assertIn('-p', args)

    def test_existing_flag_is_not_duplicated(self):
        args = run_config_syncd('marvell-teralynx', PLATFORM, cmd_args='-u -a')
        self.assertEqual(args.count('-a'), 1, args)

    def test_other_platform_is_unchanged(self):
        self.assertFalse((OTHER / 'syncd.conf').exists())
        args = run_config_syncd('broadcom', OTHER)
        self.assertNotIn('-a', args)

    def test_hook_removed_drops_flag(self):
        # Negative control: without a hook, the stock script doesn't add -a.
        args = run_config_syncd('marvell-teralynx', OTHER)
        self.assertNotIn('-a', args)


if __name__ == '__main__':
    unittest.main()
