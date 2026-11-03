"""Check that init_cfg.json.j2 only declares FEATURE entries for containers
that are part of the image, and that container_checker therefore does not
expect them to run (kamelnetworks/sonic-buildimage#121).

The template is rendered twice on a real image: once at build time by `j2`
with the INCLUDE_* build flags exported as `include_*` (slave.mk), and again
at first boot by sonic-cfggen, which evaluates the Jinja fragments left in the
FEATURE state values against DEVICE_METADATA. Both passes are reproduced here
with plain jinja2; nothing touches docker or redis.
"""
import importlib.machinery
import importlib.util
import json
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import jinja2

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = 'files/build_templates/init_cfg.json.j2'
CONTAINER_CHECKER = ROOT / 'files/image_config/monit/container_checker'

# Build flags that gate an optional container -> the FEATURE entry it adds.
OPTIONAL_FEATURES = {
    'include_dhcp_relay': 'dhcp_relay',
    'include_dhcp_server': 'dhcp_server',
    'include_nat': 'nat',
    'include_sflow': 'sflow',
}

# Upstream enables dhcp_relay on ToR-type devices and on devices without a
# type (e.g. a config_db.json written without DEVICE_METADATA.localhost.type),
# and disables it on leaf/spine routers.
LEAF_ROUTER = {'localhost': {'type': 'LeafRouter', 'hwsku': 'DS4001'}}
TOR_ROUTER = {'localhost': {'type': 'ToRRouter', 'hwsku': 'DS4001'}}
UNTYPED = {'localhost': {'hwsku': 'DS4001'}}
RUNTIME = {'ETHERNET_PORTS_PRESENT': True, 'MACSEC_SUPPORTED': False}


def render_init_cfg(**flags):
    """Build-time pass: render the template like sonic_debian_extension.j2 does
    (`j2` from the repository root), with every include_* flag off unless
    given."""
    text = (ROOT / TEMPLATE).read_text()
    ctx = {name: 'n' for name in set(re.findall(r'\binclude_[a-z_]+', text))}
    ctx.update(sonic_asic_platform='marvell-teralynx', installer_services='',
               BUILD_REDUCE_IMAGE_SIZE='n')
    ctx.update(flags)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT)),
                             extensions=['jinja2.ext.do'])
    return json.loads(env.get_template(TEMPLATE).render(**ctx))


def resolve_states(feature_table, device_metadata):
    """First-boot pass: evaluate the Jinja fragments in the FEATURE state
    values the way sonic-cfggen does."""
    env = jinja2.Environment()
    resolved = {}
    for name, entry in feature_table.items():
        entry = dict(entry)
        entry['state'] = env.from_string(entry['state']).render(
            DEVICE_METADATA=device_metadata, DEVICE_RUNTIME_METADATA=RUNTIME)
        resolved[name] = entry
    return resolved


def load_container_checker(feature_table):
    """Import /usr/bin/container_checker with its SONiC/docker imports
    replaced by fakes: single ASIC, non-chassis, CONFIG_DB FEATURE table as
    given."""
    docker = types.ModuleType('docker')
    docker.errors = types.SimpleNamespace(
        ImageNotFound=Exception, APIError=Exception, NotFound=Exception,
        DockerException=Exception)
    docker.DockerClient = mock.MagicMock()

    multi_asic = mock.MagicMock()
    multi_asic.is_multi_asic.return_value = False
    multi_asic.get_num_asics.return_value = 1
    multi_asic.get_asic_presence_list.return_value = [0]
    device_info = mock.MagicMock()
    for probe in ('is_supervisor', 'is_disaggregated_chassis', 'is_smartswitch'):
        getattr(device_info, probe).return_value = False
    sonic_py_common = types.ModuleType('sonic_py_common')
    sonic_py_common.multi_asic = multi_asic
    sonic_py_common.device_info = device_info

    swsscommon = mock.MagicMock()
    config_db = swsscommon.ConfigDBConnector.return_value
    config_db.get_table.side_effect = (
        lambda table: feature_table if table == 'FEATURE' else {})
    swsscommon_pkg = types.ModuleType('swsscommon')
    swsscommon_pkg.swsscommon = swsscommon

    fakes = {'docker': docker, 'sonic_py_common': sonic_py_common,
             'swsscommon': swsscommon_pkg}
    with mock.patch.dict(sys.modules, fakes):
        loader = importlib.machinery.SourceFileLoader(
            'container_checker', str(CONTAINER_CHECKER))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
    return module


class InitCfgFeatureTest(unittest.TestCase):
    def test_optional_features_follow_their_build_flag(self):
        default = render_init_cfg()['FEATURE']
        for flag, feature in OPTIONAL_FEATURES.items():
            with self.subTest(feature=feature):
                self.assertNotIn(feature, default,
                                 f'{feature} declared although {flag} != y')
                self.assertIn(feature, render_init_cfg(**{flag: 'y'})['FEATURE'])
        for feature in ('database', 'swss', 'syncd', 'bgp', 'pmon'):
            self.assertIn(feature, default)

    def test_dhcp_relay_default_state_when_built(self):
        table = render_init_cfg(include_dhcp_relay='y')['FEATURE']
        self.assertEqual(resolve_states(table, TOR_ROUTER)['dhcp_relay']['state'], 'enabled')
        self.assertEqual(resolve_states(table, UNTYPED)['dhcp_relay']['state'], 'enabled')
        self.assertEqual(resolve_states(table, LEAF_ROUTER)['dhcp_relay']['state'], 'disabled')

    def test_container_checker_expectations(self):
        # Image without the dhcp-relay package (rules/config INCLUDE_DHCP_RELAY=n)
        # on a device whose type would enable the feature.
        table = resolve_states(render_init_cfg()['FEATURE'], UNTYPED)
        expected, always = load_container_checker(table).get_expected_running_containers()
        self.assertNotIn('dhcp_relay', expected | always)
        self.assertEqual(always, {'database'})
        self.assertTrue({'database', 'swss', 'syncd', 'bgp', 'pmon'} <= expected)

        # Image with the package: the container is expected, as before.
        table = resolve_states(render_init_cfg(include_dhcp_relay='y')['FEATURE'], UNTYPED)
        expected, _ = load_container_checker(table).get_expected_running_containers()
        self.assertIn('dhcp_relay', expected)
        table = resolve_states(render_init_cfg(include_dhcp_relay='y')['FEATURE'], LEAF_ROUTER)
        expected, _ = load_container_checker(table).get_expected_running_containers()
        self.assertNotIn('dhcp_relay', expected)


if __name__ == '__main__':
    unittest.main()
