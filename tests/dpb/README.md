# Dynamic Port Breakout checks without hardware

Two layers of checks cover the DPB data of the 202611 Broadcom platforms
(Celestica Seastone 2, Dell S5232F-ON). Neither needs a switch.

## 1. Static data checks (run anywhere)

```sh
python3 scripts/check_breakout_modes.py \
    device/celestica/x86_64-cel_seastone_2-r0 \
    device/dell/x86_64-dellemc_s5232f_c3538-r0
python3 tests/test_breakout_modes.py -v
```

`check_breakout_modes.py` parses `platform.json` the way sonic-config-engine
does, derives the PORT table of every HWSKU that has a `hwsku.json` and
compares it with `port_config.ini`, maps the HWSKUs without `hwsku.json` onto
breakout modes, and checks each `config.bcm` for `port_flex_enable=1`, a
`portmap` per port macro and free logical port ids for the children.
Both are cheap enough for a GitHub Actions job on pull requests that touch
these platforms.

To write the `hwsku.json` a custom HWSKU (for example a SONIX one) needs for
its `port_config.ini` layout:

```sh
python3 scripts/check_breakout_modes.py --platform device/celestica/x86_64-cel_seastone_2-r0 \
    /path/to/SONIX/port_config.ini
```

## 2. `config interface breakout` walk (needs the SONiC Python stack)

`config_dpb_platforms_test.py` drives the real sonic-utilities
`config interface breakout` command, the real child port generation from
`platform.json` and the real YANG validation and dependency handling of
`ConfigMgmtDPB`, over every port and mode of both platforms and back to the
default mode. CONFIG_DB is sonic-utilities' mock DB and the wait for ASIC_DB
is mocked, as in upstream `tests/config_dpb_test.py`.

It has to run inside the sonic-utilities tree that `202611-master` pins
(`git ls-tree HEAD src/sonic-utilities`) with these installed: the
`sonic_config_engine`, `sonic_py_common`, `sonic_yang_mgmt`,
`sonic_yang_models` and `sonic_platform_common` wheels and the
`python3-swsscommon` and `python3-libyang` packages from a build, plus the
pip requirements of sonic-utilities' `setup.py`. The sonic-slave image plus
`target/python-wheels` and `target/debs` of a build give all of that:

```sh
cp tests/dpb/config_dpb_platforms_test.py /path/to/sonic-utilities/tests/
cd /path/to/sonic-utilities
DPB_DEVICE_ROOT=/path/to/sonic-buildimage/device pytest -q tests/config_dpb_platforms_test.py
```

`DPB_PORTS=Ethernet0,Ethernet124` restricts the walk to a few ports.

## Saved breakout modes during upgrade

Seastone 2 retains the legacy `1x100G` and SFP `1x10G` mode entries alongside
`1x100G[40G]` and `1x10G[1G]`. Their child port names, lanes, aliases and default
speeds are identical. Existing saved `BREAKOUT_CFG` and custom HWSKU defaults
therefore remain valid; no automatic database rewrite is required. New stock
HWSKUs use the expanded modes. The CLI tests start with saved legacy names,
transition into breakout and back, and compare real portconfig defaults.

Dell `Ethernet124` is different: the removed four-way mode collides with the
second SFP+ logical port and has no safe equivalent. Do not change only its
`BREAKOUT_CFG` string to a two-port or one-port mode: that would misdescribe
four live PORT rows. Before upgrading a system saved in that mode, arrange a
controlled reconfiguration to a supported layout using the old image and its
normal dependency handling, then verify PORT lanes/count, BREAKOUT_CFG and the
second SFP+, and save the resulting configuration. If the old path cannot
complete safely, stop and coordinate a maintenance recovery; do not let an
automatic name migration choose a different physical layout. This PR neither
remaps that mode nor attempts unsupported port creation.

Compatibility aliases only preserve configurations whose saved mode already
matches the actual PORT children. They do not repair a stale SONIX table that
says `1x100G` while four 10G PORT rows exist. Audit names/lanes/count/speeds and
regenerate PORT together with BREAKOUT_CFG from the intended HWSKU layout as
part of the SONIX integration before enabling DPB; do not infer live layout
from the old mode string alone.
