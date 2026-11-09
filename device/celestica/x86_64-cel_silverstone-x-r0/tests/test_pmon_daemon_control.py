"""Hardware-free checks for the Silverstone-X pmon_daemon_control.json (#106).

ledd needs ``plugins/led_control.py``; without it the daemon exits with
"Failed to load ledutil" and supervisord marks it FATAL on every boot. The
Silverstone-X port LEDs are driven by the switchboard FPGA/CPLD, so the
platform skips ledd instead. These tests pin that down: the file must parse,
``skip_ledd`` must be set while there is no plugin, and the real pmon
supervisord template must render without a ``[program:ledd]`` block.
"""
import json
import unittest
from pathlib import Path

import jinja2

PLATFORM_DIR = Path(__file__).resolve().parents[1]
ROOT = PLATFORM_DIR.parents[2]
CONTROL_FILE = PLATFORM_DIR / 'pmon_daemon_control.json'
SUPERVISORD_TEMPLATE = ROOT / 'dockers/docker-platform-monitor/docker-pmon.supervisord.conf.j2'

# What docker_init.j2 passes with -a next to the control file (-j).
PMON_CONF_VARS = dict(HAVE_SENSORS_CONF=0, HAVE_FANCONTROL_CONF=0, API_VERSION=3,
                      IS_MODULAR_CHASSIS=0, IS_SWITCH_BMC=0, python2_daemons=[])


def render_supervisord(control):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(SUPERVISORD_TEMPLATE.parent)))
    context = dict(PMON_CONF_VARS)
    context.update(control)
    return env.get_template(SUPERVISORD_TEMPLATE.name).render(**context)


def programs(rendered):
    return [line[len('[program:'):-1] for line in rendered.splitlines()
            if line.startswith('[program:') and line.endswith(']')]


class PmonDaemonControlTest(unittest.TestCase):
    def setUp(self):
        with CONTROL_FILE.open() as f:
            self.control = json.load(f)

    def test_skips_ledd_because_there_is_no_led_control_plugin(self):
        plugin = PLATFORM_DIR / 'plugins' / 'led_control.py'
        self.assertFalse(plugin.exists(),
                         'a led_control plugin exists now, so skip_ledd can be dropped')
        self.assertIs(self.control.get('skip_ledd'), True)

    def test_reason_is_documented_in_the_file(self):
        comment = self.control.get('_comment', '')
        self.assertIn('skip_ledd', comment)
        self.assertIn('#106', comment)

    def test_only_known_knobs_and_the_comment(self):
        # Keys become top-level template variables; keep to the ones the
        # template reads so a typo can't silently do nothing.
        template = SUPERVISORD_TEMPLATE.read_text()
        for key in self.control:
            if key == '_comment':
                continue
            self.assertIn(key, template, '%s is not read by the pmon template' % key)

    def test_supervisord_has_no_ledd_program(self):
        rendered = render_supervisord(self.control)
        progs = programs(rendered)
        self.assertNotIn('ledd', progs, rendered)
        for daemon in ('xcvrd', 'psud', 'syseepromd', 'thermalctld', 'pcied'):
            self.assertIn(daemon, progs, rendered)

    def test_without_skip_ledd_the_template_would_start_ledd(self):
        control = {k: v for k, v in self.control.items() if k != 'skip_ledd'}
        self.assertIn('ledd', programs(render_supervisord(control)))


if __name__ == '__main__':
    unittest.main()
