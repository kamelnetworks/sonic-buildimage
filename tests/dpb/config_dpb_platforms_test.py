"""Drive ``config interface breakout`` over every port and mode of the 202611
Broadcom platforms with the real sonic-utilities, sonic-config-engine and
sonic-yang code paths.

What is real: ``platform.json`` / ``hwsku.json`` of the platform, the child
port generation (``portconfig.get_child_ports``), the ``BREAKOUT_CFG``
bookkeeping in ``config/main.py`` and the YANG validation and dependency
handling in ``ConfigMgmtDPB``. What is mocked: the CONFIG_DB (sonic-utilities'
mock tables, which also receive the diffs ConfigMgmtDPB writes) and the wait
for ASIC_DB to drop the deleted ports (as in upstream
``tests/config_dpb_test.py``). Nothing here talks to hardware.

Run it from the sonic-utilities checkout that 202611-master pins (it needs
that tree's ``tests/conftest.py`` and mock tables)::

    cp tests/dpb/config_dpb_platforms_test.py <sonic-utilities>/tests/
    cd <sonic-utilities>
    DPB_DEVICE_ROOT=<sonic-buildimage>/device pytest -q tests/config_dpb_platforms_test.py

``DPB_PORTS=Ethernet0,Ethernet64`` restricts the walk to some ports.
"""

import json
import os
from unittest import mock

import pytest
from click.testing import CliRunner
from utilities_common.db import Db
from utilities_common.general import load_module_from_source

import config.main as config
import portconfig

from .utils import worker_tmp_path

sonic_cfggen = load_module_from_source('sonic_cfggen', '/usr/local/bin/sonic-cfggen')
config_mgmt = load_module_from_source(
    'config_mgmt', os.path.join(os.path.dirname(__file__), '..', 'config', 'config_mgmt.py'))

DEVICE_ROOT = os.environ.get('DPB_DEVICE_ROOT')
PLATFORMS = {
    'seastone2': ('celestica/x86_64-cel_seastone_2-r0', 'Seastone_2'),
    's5232f': ('dell/x86_64-dellemc_s5232f_c3538-r0', 'DellEMC-S5232f-C32'),
}
PORT_FILTER = [p for p in os.environ.get('DPB_PORTS', '').split(',') if p]

pytestmark = pytest.mark.skipif(not DEVICE_ROOT, reason='DPB_DEVICE_ROOT not set')


def write_config_db(cfgdb, cfg):
    data = dict()
    sonic_cfggen.deep_update(data, sonic_cfggen.FormatConverter.to_deserialized(cfg))
    cfgdb.mod_config(sonic_cfggen.FormatConverter.output_to_db(data))


def read_config_db(cfgdb):
    data = dict()
    sonic_cfggen.deep_update(data, sonic_cfggen.FormatConverter.db_to_output(cfgdb.get_config()))
    return sonic_cfggen.FormatConverter.to_serialized(data)


def make_config_mgmt(cfgdb):
    """ConfigMgmtDPB on the mock CONFIG_DB with DB writes and ASIC_DB waits mocked."""
    config_mgmt.CONFIG_DB_JSON_FILE = worker_tmp_path('dpbPlatformsStartConfigDb.json')
    config_mgmt.DEFAULT_CONFIG_DB_JSON_FILE = worker_tmp_path('dpbPlatformsDefaultConfigDb.json')
    with open(config_mgmt.CONFIG_DB_JSON_FILE, 'w') as f:
        json.dump(read_config_db(cfgdb), f, indent=4)
    with open(config_mgmt.DEFAULT_CONFIG_DB_JSON_FILE, 'w') as f:
        json.dump({}, f)
    cm = config_mgmt.ConfigMgmtDPB(source=config_mgmt.CONFIG_DB_JSON_FILE)
    # Write the deletes and adds through the CLI's own mock CONFIG_DB
    # connector (every mock connector has its own in-memory store), so the
    # next breakout sees the children it has to delete. Only the wait for the
    # ASIC to drop the ports is mocked.
    cm.configdb = cfgdb
    cm._verifyAsicDB = mock.MagicMock(return_value=True)
    return cm


class Platform:
    def __init__(self, key):
        rel, self.hwsku = PLATFORMS[key]
        self.dir = os.path.join(DEVICE_ROOT, rel)
        self.name = os.path.basename(rel)
        self.platform_json = os.path.join(self.dir, 'platform.json')
        self.hwsku_json = os.path.join(self.dir, self.hwsku, 'hwsku.json')
        with open(self.platform_json) as f:
            self.interfaces = json.load(f)['interfaces']
        ports, _, _ = portconfig.get_port_config(port_config_file=self.platform_json,
                                                 hwsku_config_file=self.hwsku_json)
        self.ports = ports
        self.breakout_cfg = portconfig.parse_breakout_mode(self.hwsku_json)

    def start_config(self):
        return {
            'DEVICE_METADATA': {'localhost': {
                'hostname': 'dpb-test', 'hwsku': self.hwsku, 'platform': self.name,
                'mac': '00:11:22:33:44:55', 'type': 'ToRRouter', 'bgp_asn': '65100',
            }},
            'PORT': self.ports,
            'BREAKOUT_CFG': self.breakout_cfg,
        }

    def children(self, intf, mode):
        return portconfig.get_child_ports(intf, mode, self.platform_json)


@pytest.fixture(params=sorted(PLATFORMS))
def env(request):
    """Mock CONFIG_DB loaded with the platform's default breakout layout, CLI mocks installed."""
    plat = Platform(request.param)
    db = Db()
    for table in db.cfgdb.get_config():
        db.cfgdb.delete_table(table)
    write_config_db(db.cfgdb, plat.start_config())
    cm = make_config_mgmt(db.cfgdb)

    saved = (config.device_info.get_path_to_port_config_file, config.load_ConfigMgmt,
             config.breakout_warnUser_extraTables, config.asic_type)
    config.asic_type = mock.MagicMock(return_value='broadcom')
    config.device_info.get_path_to_port_config_file = mock.MagicMock(return_value=plat.platform_json)
    config.load_ConfigMgmt = mock.MagicMock(return_value=cm)
    config.breakout_warnUser_extraTables = mock.MagicMock(return_value=True)
    yield plat, db, cm
    (config.device_info.get_path_to_port_config_file, config.load_ConfigMgmt,
     config.breakout_warnUser_extraTables, config.asic_type) = saved


def breakout(db, intf, mode, *flags):
    runner = CliRunner()
    return runner.invoke(config.config.commands['interface'].commands['breakout'],
                         [intf, mode, '-y'] + list(flags), obj={'config_db': db.cfgdb})


def assert_layout(cm, plat, intf, mode, gone):
    """The data tree ConfigMgmtDPB validated holds exactly the children of ``mode``."""
    ports = cm.configdbJsonOut['PORT']
    expected = plat.children(intf, mode)
    for child, props in expected.items():
        assert child in ports, (intf, mode, child, sorted(ports))
        assert ports[child]['lanes'] == props['lanes'], (intf, mode, child)
        assert ports[child]['speed'] == props['speed'], (intf, mode, child)
        assert ports[child]['alias'] == props['alias'], (intf, mode, child)
    for old in gone:
        if old not in expected:
            assert old not in ports, (intf, mode, old)


def test_every_mode_and_back(env):
    """CUJ-19 on the control plane: every port into every mode and back to the default."""
    plat, db, cm = env
    walked = 0
    for intf, props in plat.interfaces.items():
        if PORT_FILTER and intf not in PORT_FILTER:
            continue
        modes = list(props['breakout_modes'])
        if len(modes) < 2:
            continue
        default = plat.breakout_cfg[intf]['brkout_mode']
        assert default in modes, (intf, default)
        for mode in modes:
            if mode == default:
                continue
            result = breakout(db, intf, mode, '-f')
            assert result.exit_code == 0, (intf, mode, result.output)
            assert 'Breakout process got successfully completed.' in result.output
            assert db.cfgdb.get_table('BREAKOUT_CFG')[intf]['brkout_mode'] == mode
            assert_layout(cm, plat, intf, mode, plat.children(intf, default))

            result = breakout(db, intf, default, '-f')
            assert result.exit_code == 0, (intf, default, result.output)
            assert db.cfgdb.get_table('BREAKOUT_CFG')[intf]['brkout_mode'] == default
            assert_layout(cm, plat, intf, default, plat.children(intf, mode))
            walked += 1
    assert walked > 0
    # Nothing else changed: the tree is back to the start layout.
    assert set(cm.configdbJsonOut['PORT']) == set(plat.ports)


def test_unsupported_mode_is_refused(env):
    plat, db, cm = env
    intf = 'Ethernet0'
    bad = '8x50G' if '8x50G' not in plat.interfaces[intf]['breakout_modes'] else '2x200G'
    result = breakout(db, intf, bad, '-f')
    assert result.exit_code != 0
    assert 'is not available for the port' in result.output
    assert db.cfgdb.get_table('BREAKOUT_CFG')[intf]['brkout_mode'] == plat.breakout_cfg[intf]['brkout_mode']


def test_dependencies_need_force(env):
    """A VLAN membership on the port blocks the breakout unless -f removes it."""
    plat, db, cm = env
    intf = 'Ethernet8'
    default = plat.breakout_cfg[intf]['brkout_mode']
    target = [m for m in plat.interfaces[intf]['breakout_modes'] if m != default][0]
    write_config_db(db.cfgdb, {
        'VLAN': {'Vlan100': {'admin_status': 'up'}},
        'VLAN_MEMBER': {'Vlan100|%s' % intf: {'tagging_mode': 'untagged'}},
    })
    cm2 = make_config_mgmt(db.cfgdb)
    config.load_ConfigMgmt = mock.MagicMock(return_value=cm2)

    result = breakout(db, intf, target)
    assert result.exit_code != 0, result.output
    assert 'Dependencies Exist.' in result.output
    assert 'VLAN_MEMBER' in result.output
    assert db.cfgdb.get_table('BREAKOUT_CFG')[intf]['brkout_mode'] == default

    result = breakout(db, intf, target, '-f')
    assert result.exit_code == 0, result.output
    assert db.cfgdb.get_table('BREAKOUT_CFG')[intf]['brkout_mode'] == target
    assert 'Vlan100|%s' % intf not in cm2.configdbJsonOut.get('VLAN_MEMBER', {})
    assert_layout(cm2, plat, intf, target, plat.children(intf, default))


@pytest.mark.parametrize('mode,speed', [
    ('4x25G[10G](4)', '25000'), ('4x10G[25G](4)', '10000')])
def test_equivalent_alias_modes_keep_requested_speed(tmp_path, mode, speed):
    """Real portconfig uses the first equivalent alias list, but requested speed."""
    aliases = ['QSFP1/1', 'QSFP1/2', 'QSFP1/3', 'QSFP1/4']
    platform = tmp_path / 'platform.json'
    platform.write_text(json.dumps({'interfaces': {'Ethernet0': {
        'index': '1,1,1,1', 'lanes': '1,2,3,4', 'breakout_modes': {
            '4x25G[10G](4)': aliases, '4x10G[25G](4)': aliases}}}}))
    hwsku = tmp_path / 'hwsku.json'
    hwsku.write_text(json.dumps({'interfaces': {
        'Ethernet0': {'default_brkout_mode': mode}}}))
    children = portconfig.get_child_ports('Ethernet0', mode, str(platform))
    assert [p['alias'] for p in children.values()] == aliases
    assert [p['lanes'] for p in children.values()] == ['1', '2', '3', '4']
    assert {p['speed'] for p in children.values()} == {speed}
    generated, _, _ = portconfig.parse_platform_json_file(str(hwsku), str(platform))
    assert generated == children


def test_saved_legacy_seastone_modes_round_trip(env):
    """Saved names still resolve; transition both ways without rewriting layouts."""
    plat, db, _ = env
    if plat.name != 'x86_64-cel_seastone_2-r0':
        pytest.skip('legacy mode names are specific to Seastone 2')
    for intf, props in plat.interfaces.items():
        if PORT_FILTER and intf not in PORT_FILTER:
            continue
        legacy, expanded = (('1x100G', '1x100G[40G]') if len(props['lanes'].split(',')) == 4
                            else ('1x10G', '1x10G[1G]'))
        assert plat.children(intf, legacy) == plat.children(intf, expanded)
        write_config_db(db.cfgdb, {'BREAKOUT_CFG': {intf: {'brkout_mode': legacy}}})
        cm = make_config_mgmt(db.cfgdb)
        config.load_ConfigMgmt = mock.MagicMock(return_value=cm)
        # Reproduce the reported old-name -> breakout path as well as both
        # old/current spellings. The initial PORT layout is already identical.
        target = '4x25G[10G]' if legacy == '1x100G' else expanded
        for mode in (target, legacy, expanded):
            result = breakout(db, intf, mode, '-f')
            assert result.exit_code == 0, (intf, mode, result.output)
            assert db.cfgdb.get_table('BREAKOUT_CFG')[intf]['brkout_mode'] == mode
            assert_layout(cm, plat, intf, mode, {})


def test_removed_dell_four_way_mode_is_not_remapped(env):
    plat, db, _ = env
    if plat.name != 'x86_64-dellemc_s5232f_c3538-r0':
        pytest.skip('management logical-port collision is specific to Dell')
    before = read_config_db(db.cfgdb)
    result = breakout(db, 'Ethernet124', '4x25G[10G]', '-f')
    assert result.exit_code != 0
    assert 'is not available for the port' in result.output
    assert read_config_db(db.cfgdb) == before
    with pytest.raises(RuntimeError, match='Unsupported breakout mode'):
        plat.children('Ethernet124', '4x25G[10G]')
