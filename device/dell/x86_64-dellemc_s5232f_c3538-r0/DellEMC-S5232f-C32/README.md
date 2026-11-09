# DellEMC-S5232f-C32: breakout limits

Port 32 (`Ethernet124`, lanes 125-128) offers only `1x100G[50G,40G]` and
`2x50G` in `platform.json`. Every other QSFP28 port also offers `4x25G[10G]`.

## Why port 32 can't split four ways

On Trident 3 each pipe has a fixed set of logical port ids. Pipe 1 owns
logical ports 66-131 (`bcmcmd 'show pmap'`):

- 64 front-panel lanes (ports 17-32) need up to 64 ids for a full 4-way
  breakout;
- both SFP+ management ports sit in pipe 1, at logical 66 and 130
  (`portmap_66.0=129:10:m`, `portmap_130.0=128:10:m`);
- 131 is the pipe's internal loopback port (`lb1`).

That is 65 usable ids for 66 ports, so one is always missing. A breakout
child's logical id is the parent's id plus the lane offset, so the 4th
child of port 32 (lane 128) needs logical 130, which the second SFP+ holds.
The SAI creates the first three children and refuses the 4th:

```
_brcm_sai_dyn_port_brkout_config: Dynamic port config failed 130
port resource multi set failed with error Invalid parameter
```

Moving the SFP+ to another id doesn't work. Logical 130 and 131 were tested:
131 is the loopback port (`Port type 6 is not supported TD3`). Ids above 131
were not tested, because they belong to no pipe.
Tested on ixp-lab-sw4 in kamelnetworks/sonic-buildimage#110 and #172.

## Why we keep the SFP+

The only way to free logical 130 is to drop the second SFP+ port. Breakout
modes are listed per platform, not per HWSKU, so that would remove the port
on every S5232F, and it carries production links (for example a 10G
inter-switch link at SONIX). We decided to keep the SFP+ and give up 4-way
breakout on port 32 (kamelnetworks/sonic-buildimage#110).

`scripts/check_breakout_modes.py` enforces this: it fails on any mode whose
child needs a management port's logical id.

## What port 32 does on hardware

Tested on ixp-lab-sw4 (rc1 `SONiC.verify_rc1-20261104-kamel.60-5b1565382`,
libsaibcm 15.2.1.0.0.0.26.3, HWSKU `SONIX`) in
kamelnetworks/sonic-buildimage#110. The far end (ixp-lab-sw1 QSFP30) stayed
at 100G, so these results show what the SAI accepts, not that the link comes
up.

| Step | Result |
|---|---|
| `1x100G` at 40G | Accepted. SDK `xe(127)` 40G XLAUI. |
| `1x100G` at 50G | **Not supported.** STATE_DB `supported_speeds` is `40000,100000` on every 4-lane port, the CLI refuses 50000, and a CONFIG_DB write gets `doPortTask: Unsupported port Ethernet124 speed 50000` (dropped, no crash). |
| `1x100G` → `2x50G` | Accepted. Children `Ethernet124` (lanes 125-126) and `Ethernet126` (127-128) are logical 127 and 129, 50G KR2. Both SFP+ ports keep logical 66 and 130. |
| `2x50G` → `1x100G` | Accepted, logical 127 again. As on every Trident 3 port, the re-created 100G port stays in `Fault(Local)` until `systemctl restart swss` (#102). |

The `50G` in `1x100G[50G,40G]` comes from upstream and is on every port of
this platform. A 4-lane 50G port does not work on this SAI; use `2x50G`.

The stock `config interface breakout` refuses the `SONIX` HWSKU (no
`hwsku.json`), so the breakout steps were driven the way the CLI would be,
deleting the last lane first (#170).
