"""Regression test for BGP route maps declared through a YANG `uses` grouping
(kamelnetworks/sonic-buildimage#49).

`route_map_in` and `route_map_out` are leaf-lists in the
`sonic-bgp-common:sonic-bgp-cmn-af` grouping, which BGP_NEIGHBOR_AF and
BGP_PEER_GROUP_AF pull in with `uses`. With libyang2, sonic-yang-mgmt only
merged the `leaf` nodes of a grouping when it translated CONFIG_DB to YANG
data, so CONFIG_DB entries with these fields were refused:

    All Keys are not parsed in BGP_PEER_GROUP_AF
    dict_keys(['default|PEERS|ipv4_unicast'])
    exceptionList:["'route_map_in'"]

The fork carried a workaround (7db7b8be4, upstream PR 21078) that copied the
leaf-lists into each module. Upstream later fixed the translator instead
(sonic_yang_ext._compileUsesClause, upstream PR 21907), and the workaround was
not replayed when the fork moved to libyang3. This test checks that the
translator and libyang3 still accept, translate and validate such a config.

It uses the tree's YANG models and sonic-yang-mgmt sources, and the libyang
Python binding of the environment (docker-config-engine-trixie in the unified
test workflow).
"""
import copy
import shutil
import sys
from pathlib import Path

import jinja2
import pytest

ROOT = Path(__file__).resolve().parents[1]
YANG_MODELS = ROOT / 'src/sonic-yang-models/yang-models'
YANG_TEMPLATES = ROOT / 'src/sonic-yang-models/yang-templates'
YANG_MGMT = ROOT / 'src/sonic-yang-mgmt'

sys.path.insert(0, str(YANG_MGMT))

# A SONIX-style policy: the peering LAN is accepted with a higher local
# preference, everything else is denied, and the same maps are applied to a
# peer group and to one neighbor directly.
CONFIG = {
    'DEVICE_METADATA': {
        'localhost': {
            'hostname': 'ixp-lab-sw3',
            'hwsku': 'SONIX',
            'platform': 'x86_64-cel_seastone_2-r0',
            'mac': '00:11:22:33:44:55',
            'type': 'LeafRouter',
            'bgp_asn': '65003',
            'docker_routing_config_mode': 'unified',
            'frr_mgmt_framework_config': 'true',
        },
    },
    'BGP_GLOBALS': {
        'default': {'local_asn': '65003', 'router_id': '10.0.0.3'},
    },
    'BGP_GLOBALS_AF': {
        'default|ipv4_unicast': {},
        'default|ipv6_unicast': {},
    },
    'PREFIX_SET': {
        'SONIX-LAN-V4': {'mode': 'IPv4'},
        'SONIX-LAN-V6': {'mode': 'IPv6'},
    },
    'PREFIX': {
        'SONIX-LAN-V4|10|192.0.2.0/24|24..32': {'action': 'permit'},
        'SONIX-LAN-V6|10|2001:db8:1981::/48|48..64': {'action': 'permit'},
    },
    'ROUTE_MAP_SET': {
        'SONIX-IN': {},
        'SONIX-OUT': {},
    },
    'ROUTE_MAP': {
        'SONIX-IN|10': {
            'route_operation': 'permit',
            'match_prefix_set': 'SONIX-LAN-V4',
            'set_local_pref': '200',
        },
        'SONIX-IN|20': {
            'route_operation': 'permit',
            'match_ipv6_prefix_set': 'SONIX-LAN-V6',
            'set_local_pref': '200',
        },
        'SONIX-IN|100': {'route_operation': 'deny'},
        'SONIX-OUT|10': {'route_operation': 'permit'},
    },
    'BGP_PEER_GROUP': {
        'default|SONIX-PEERS': {
            'peer_type': 'external',
            'holdtime': '9',
            'keepalive': '3',
        },
    },
    'BGP_PEER_GROUP_AF': {
        'default|SONIX-PEERS|ipv4_unicast': {
            'admin_status': 'up',
            'route_map_in': ['SONIX-IN'],
            'route_map_out': ['SONIX-OUT'],
        },
        'default|SONIX-PEERS|ipv6_unicast': {
            'admin_status': 'up',
            'route_map_in': ['SONIX-IN'],
            'route_map_out': ['SONIX-OUT'],
        },
    },
    'BGP_NEIGHBOR': {
        'default|192.0.2.1': {
            'asn': '65001',
            'peer_group_name': 'SONIX-PEERS',
            'admin_status': 'down',
        },
    },
    'BGP_NEIGHBOR_AF': {
        'default|192.0.2.1|ipv4_unicast': {
            'admin_status': 'up',
            'route_map_in': ['SONIX-IN'],
            'route_map_out': ['SONIX-OUT'],
        },
    },
}

# CONFIG_DB table -> (YANG module, container) of the AF tables under test.
AF_TABLES = {
    'BGP_PEER_GROUP_AF': 'sonic-bgp-peergroup',
    'BGP_NEIGHBOR_AF': 'sonic-bgp-neighbor',
}


@pytest.fixture(scope='module')
def yang_dir(tmp_path_factory):
    """The tree's YANG models, with the templated ones rendered the way
    sonic-yang-models' setup.py renders them for the Python package."""
    out = tmp_path_factory.mktemp('yang-models')
    for path in YANG_MODELS.glob('*.yang'):
        shutil.copy(path, out)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(YANG_TEMPLATES)),
                             trim_blocks=True)
    for path in YANG_TEMPLATES.glob('*.yang.j2'):
        text = env.get_template(path.name).render(yang_model_type='py')
        (out / path.name[:-len('.j2')]).write_text(text)
    return str(out)


@pytest.fixture(scope='module')
def loaded_models(yang_dir):
    # Keep the source-schema premise test runnable in lightweight CI. Skip
    # only runtime cases when the binding is absent; a module-level skip
    # collects zero tests and makes run-fork-unit-tests.sh fail with exit 5.
    pytest.importorskip('libyang', reason='needs the libyang3 Python binding')
    import sonic_yang

    syc = sonic_yang.SonicYang(yang_dir, print_log_enabled=False)
    syc.loadYangModel()
    return syc


@pytest.fixture
def syc(loaded_models):
    # loadData() replaces the data tree and per-load state, so the loaded
    # schema can be shared between tests.
    return loaded_models


def _af_list(xlate, table):
    module = AF_TABLES[table]
    return xlate[f'{module}:{module}'][f'{module}:{table}'][f'{table}_LIST']


def test_route_maps_come_from_the_uses_grouping():
    # Guard the premise of this test: if the 7db7b8be4 workaround is ever
    # re-applied, the leaf-lists no longer go through `uses` and this test
    # would stop covering the translator's grouping handling.
    common = (YANG_MODELS / 'sonic-bgp-common.yang').read_text()
    assert 'leaf-list route_map_in' in common
    assert 'leaf-list route_map_out' in common
    for module in AF_TABLES.values():
        text = (YANG_MODELS / f'{module}.yang').read_text()
        assert 'uses bgpcmn:sonic-bgp-cmn-af;' in text
        assert 'route_map_in' not in text, module
        assert 'route_map_out' not in text, module


def test_route_map_config_loads_and_validates(syc):
    syc.loadData(copy.deepcopy(CONFIG))
    assert syc.tablesWithOutYang == {}
    syc.validate_data_tree()

    for table, entries in CONFIG.items():
        if table not in AF_TABLES:
            continue
        xlated = _af_list(syc.xlateJson, table)
        assert len(xlated) == len(entries), table
        for entry in xlated:
            assert entry['route_map_in'] == ['SONIX-IN'], (table, entry)
            assert entry['route_map_out'] == ['SONIX-OUT'], (table, entry)


def test_route_map_config_round_trips(syc):
    # `config apply-patch` and `config reload` validation go through
    # loadData()/getData(); the route maps must survive both directions.
    syc.loadData(copy.deepcopy(CONFIG))
    syc.getData()
    for table in AF_TABLES:
        assert syc.revXlateJson[table] == CONFIG[table]


def test_yjson_view_lists_route_maps(syc):
    # sonic-utilities walks the YIN-shaped yJson view. Upstream 21907 also had
    # to drop sonic-utilities' own `uses` inlining, so the route maps must be
    # visible there without it.
    modules = {m['module']['@name']: m['module'] for m in syc.yJson}
    for table, module in AF_TABLES.items():
        lst = modules[module]['container']['container']
        lst = next(c for c in (lst if isinstance(lst, list) else [lst])
                   if c['@name'] == table)['list']
        assert 'uses' not in lst, table
        names = [ll['@name'] for ll in lst['leaf-list']]
        assert 'route_map_in' in names, table
        assert 'route_map_out' in names, table


def test_unknown_route_map_is_refused(syc):
    import sonic_yang

    # The leaf-list must be validated as a leafref, not dropped on the way in.
    config = copy.deepcopy(CONFIG)
    config['BGP_PEER_GROUP_AF']['default|SONIX-PEERS|ipv4_unicast']['route_map_in'] = ['NO-SUCH-MAP']
    # libyang3 already checks the leafref while parsing in loadData().
    with pytest.raises(sonic_yang.SonicYangException, match='NO-SUCH-MAP'):
        syc.loadData(config)
        syc.validate_data_tree()


def test_second_route_map_is_refused(syc):
    import sonic_yang

    # max-elements 1 from the grouping applies too.
    config = copy.deepcopy(CONFIG)
    config['BGP_NEIGHBOR_AF']['default|192.0.2.1|ipv4_unicast']['route_map_out'] = ['SONIX-OUT', 'SONIX-IN']
    with pytest.raises(sonic_yang.SonicYangException):
        syc.loadData(config)
        syc.validate_data_tree()
