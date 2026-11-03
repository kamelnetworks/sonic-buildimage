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
