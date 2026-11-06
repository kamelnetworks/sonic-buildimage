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

Moving the SFP+ to another id doesn't work: 131 is the loopback port
(`Port type 6 is not supported TD3`), and ids above 131 belong to no pipe.
Tested on ixp-lab-sw4 in kamelnetworks/sonic-buildimage#110 and #172.

## Why we keep the SFP+

The only way to free logical 130 is to drop the second SFP+ port. Breakout
modes are listed per platform, not per HWSKU, so that would remove the port
on every S5232F, and it carries production links (for example a 10G
inter-switch link at SONIX). We decided to keep the SFP+ and give up 4-way
breakout on port 32 (kamelnetworks/sonic-buildimage#110).

`scripts/check_breakout_modes.py` enforces this: it fails on any mode whose
child needs a management port's logical id.
