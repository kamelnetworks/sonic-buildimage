from unittest.mock import MagicMock, patch

import os
import sys
import jinja2
import pytest

from . import swsscommon_test

# These tests need no database bindings: use the existing SONiC API mock.
sys.modules.setdefault("swsscommon", swsscommon_test)
sys.modules.setdefault("swsscommon.swsscommon", swsscommon_test.swsscommon)

from bgpcfgd.directory import Directory
from bgpcfgd.template import TemplateFabric
from bgpcfgd.managers_setsrc import ZebraSetSrc

TEMPLATE_PATH = os.path.abspath('../../dockers/docker-fpm-frr/frr')

def constructor():
    cfg_mgr = MagicMock()
    common_objs = {
        'directory': Directory(),
        'cfg_mgr':   cfg_mgr,
        'tf':        TemplateFabric(TEMPLATE_PATH),
        'constants': {},
    }

    m = ZebraSetSrc(common_objs, "STATE_DB", "INTERFACE_TABLE")
    assert m.lo_ipv4 == None
    assert m.lo_ipv6 == None

    return m

@patch('bgpcfgd.managers_setsrc.log_info')
def test_set_handler(mocked_log_info):
    m = constructor()
    res = m.set_handler("Loopback0|10.1.0.32/32", {"state": "ok"})
    assert res, "Returns always True"
    mocked_log_info.assert_called_with("The 'set src' configuration with Loopback0 ip '10.1.0.32' has been scheduled to be added")

@patch('bgpcfgd.managers_setsrc.log_err')
def test_set_handler_no_slash(mocked_log_err):
    m = constructor()
    res = m.set_handler("Loopback0|10.1.0.32", {"state": "ok"})
    assert res, "Returns always True"
    mocked_log_err.assert_called_with("Wrong Loopback0 ip prefix: '10.1.0.32'")

@patch('bgpcfgd.managers_setsrc.log_info')
def test_set_handler_ipv6(mocked_log_info):
    m = constructor()
    res = m.set_handler("Loopback0|FC00:1::32/128", {"state": "ok"})
    assert res, "Returns always True"
    mocked_log_info.assert_called_with("The 'set src' configuration with Loopback0 ip 'FC00:1::32' has been scheduled to be added")

@patch('bgpcfgd.managers_setsrc.log_err')
def test_set_handler_invalid_ip(mocked_log_err):
    m = constructor()
    res = m.set_handler("Loopback0|invalid/ip", {"state": "ok"})
    assert res, "Returns always True"
    mocked_log_err.assert_called_with("Got ambiguous ip address 'invalid'")


FAMILIES = [
    ('10.1.0.32/32', '10.1.0.33/32', 'lo_ipv4', 'ip', 'RM_SET_SRC'),
    ('FC00:1::32/128', 'FC00:1::33/128', 'lo_ipv6', 'ipv6', 'RM_SET_SRC6'),
]


def commands(manager):
    return [line.strip() for line in manager.cfg_mgr.push.call_args.args[0].splitlines()
            if line.strip() and not line.startswith('!')]


@pytest.mark.parametrize('old,new,attr,protocol,route_map', FAMILIES)
def test_delete_and_readd(old, new, attr, protocol, route_map):
    m = constructor()
    key = 'Loopback0|' + old
    m.set_handler(key, {'state': 'ok'})
    m.cfg_mgr.reset_mock()
    m.del_handler(key)
    assert commands(m) == ['no %s protocol bgp route-map %s' % (protocol, route_map),
                           'no route-map %s' % route_map]
    assert getattr(m, attr) is None
    assert getattr(m, attr + '_key') is None
    assert key not in m.directory.get_slot(m.db_name, m.table_name)
    m.cfg_mgr.reset_mock()
    m.set_handler('Loopback0|' + new, {'state': 'ok'})
    assert commands(m) == ['route-map %s permit 10' % route_map,
                           'set src ' + new.split('/')[0],
                           '%s protocol bgp route-map %s' % (protocol, route_map)]
    assert getattr(m, attr) == new.split('/')[0]


@pytest.mark.parametrize('old,new,attr,protocol,route_map', FAMILIES)
def test_replace_before_old_address_is_removed(old, new, attr, protocol, route_map):
    m = constructor()
    m.set_handler('Loopback0|' + old, {'state': 'ok'})
    m.cfg_mgr.reset_mock()
    m.set_handler('Loopback0|' + new, {'state': 'ok'})
    assert commands(m) == ['route-map %s permit 10' % route_map,
                           'set src ' + new.split('/')[0],
                           '%s protocol bgp route-map %s' % (protocol, route_map)]
    m.cfg_mgr.reset_mock()
    m.del_handler('Loopback0|' + old)
    m.cfg_mgr.push.assert_not_called()
    assert getattr(m, attr) == new.split('/')[0]
    assert getattr(m, attr + '_key') == 'Loopback0|' + new


@pytest.mark.parametrize('old,new,attr,protocol,route_map', FAMILIES)
def test_duplicate_ready_event_is_noop(old, new, attr, protocol, route_map):
    m = constructor()
    key = 'Loopback0|' + old
    m.set_handler(key, {'state': 'ok'})
    m.cfg_mgr.reset_mock()
    m.set_handler(key, {'state': 'ok'})
    m.cfg_mgr.push.assert_not_called()


@pytest.mark.parametrize('old,new,attr,protocol,route_map', FAMILIES)
def test_render_failure_preserves_selected_address(old, new, attr, protocol, route_map):
    m = constructor()
    m.set_handler('Loopback0|' + old, {'state': 'ok'})
    m.cfg_mgr.reset_mock()
    with patch.object(m.zebra_set_src_template, 'render', side_effect=jinja2.TemplateError):
        m.set_handler('Loopback0|' + new, {'state': 'ok'})
    m.cfg_mgr.push.assert_not_called()
    assert getattr(m, attr) == old.split('/')[0]
    assert getattr(m, attr + '_key') == 'Loopback0|' + old
    m.set_handler('Loopback0|' + new, {'state': 'ok'})
    assert getattr(m, attr) == new.split('/')[0]
    m.cfg_mgr.push.assert_called_once()


def test_delete_only_affects_selected_family():
    m = constructor()
    for address in ('10.1.0.32/32', 'FC00:1::32/128'):
        m.set_handler('Loopback0|' + address, {'state': 'ok'})
    m.cfg_mgr.reset_mock()
    m.del_handler('Loopback0|10.1.0.32/32')
    assert commands(m) == ['no ip protocol bgp route-map RM_SET_SRC',
                           'no route-map RM_SET_SRC']
    assert m.lo_ipv4 is None
    assert m.lo_ipv6 == 'FC00:1::32'
    m.cfg_mgr.reset_mock()
    m.del_handler('Loopback0|FC00:1::32/128')
    assert commands(m) == ['no ipv6 protocol bgp route-map RM_SET_SRC6',
                           'no route-map RM_SET_SRC6']
    assert m.lo_ipv6 is None


@pytest.mark.parametrize('key,data', [
    ('Loopback0', {'state': 'ok'}),
    ('Loopback1|10.1.0.33/32', {'state': 'ok'}),
    ('Ethernet0|10.1.0.33/32', {'state': 'ok'}),
    ('Loopback0|10.1.0.33/32', {'state': 'pending'}),
    ('Loopback0|10.1.0.33/32', {}),
])
def test_unrelated_or_unready_events_leave_source_unchanged(key, data):
    m = constructor()
    m.set_handler('Loopback0|10.1.0.32/32', {'state': 'ok'})
    m.cfg_mgr.reset_mock()
    m.set_handler(key, data)
    m.del_handler(key)
    m.cfg_mgr.push.assert_not_called()
    assert m.lo_ipv4 == '10.1.0.32'


def test_same_address_with_new_prefix_tracks_new_key():
    m = constructor()
    m.set_handler('Loopback0|10.1.0.32/32', {'state': 'ok'})
    m.cfg_mgr.reset_mock()
    m.set_handler('Loopback0|10.1.0.32/24', {'state': 'ok'})
    m.del_handler('Loopback0|10.1.0.32/32')
    m.cfg_mgr.push.assert_not_called()
    assert m.lo_ipv4 == '10.1.0.32'
    m.del_handler('Loopback0|10.1.0.32/24')
    assert m.lo_ipv4 is None
    m.cfg_mgr.push.assert_called_once()


def test_unknown_and_repeated_delete_are_noops():
    m = constructor()
    key = 'Loopback0|10.1.0.32/32'
    m.del_handler(key)
    m.cfg_mgr.push.assert_not_called()
    m.set_handler(key, {'state': 'ok'})
    m.del_handler(key)
    m.cfg_mgr.reset_mock()
    m.del_handler(key)
    m.cfg_mgr.push.assert_not_called()
