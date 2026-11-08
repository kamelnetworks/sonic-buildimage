#!/bin/bash
# Run with CAP_SYS_ADMIN/CAP_NET_ADMIN on a disposable builder, not in host netns.
set -euo pipefail
rules=$(readlink -f "${1:-$(dirname "$0")/../files/image_config/ebtables/bridge-nd-filter.nft}")
packets=$(readlink -f "$(dirname "$0")/bridge_nd_guard_packets.py")
ebtables_rules=$(readlink -f "$(dirname "$rules")/ebtables.filter.cfg")
unshare --net bash -s -- "$rules" "$packets" "$ebtables_rules" "${BRIDGE_GUARD_EBTABLES_RESTORE:-ebtables-restore}" <<'INNER'
set -euo pipefail
nft add table bridge issue184_sentinel
nft 'add chain bridge issue184_sentinel untouched'
nft 'add rule bridge issue184_sentinel untouched counter accept'
nft list table bridge issue184_sentinel > /tmp/issue184-sentinel-before.$$
trap 'rm -f /tmp/issue184-sentinel-before.$$ /tmp/issue184-sentinel-after.$$' EXIT
nft --check -f "$1"
"$4" < "$3"
nft -f "$1"
"$4" < "$3"
nft -f "$1"
nft list table bridge issue184_sentinel > /tmp/issue184-sentinel-after.$$
cmp /tmp/issue184-sentinel-before.$$ /tmp/issue184-sentinel-after.$$
nft -j list table bridge sonic_nd_guard | python3 -c '
import json,sys
items=json.load(sys.stdin)["nftables"]
rules=[x["rule"] for x in items if "rule" in x]
chains=[x["chain"] for x in items if "chain" in x]
assert len(rules)==2, rules
assert len(chains)==1 and chains[0]["hook"]=="forward", chains
print("PASS: exact parser/kernel load, repeated load, two rules, FORWARD only, unrelated table preserved")
'
# Isolated kernel bridge; inject packets through one veth and receive the other.
ip link add br0 type bridge
ip link add send0 type veth peer name port1
ip link add recv0 type veth peer name port2
ip link set port1 master br0
ip link set port2 master br0
for dev in br0 send0 port1 recv0 port2; do ip link set "$dev" up; done
nft flush table bridge sonic_nd_guard
python3 "$2" --unguarded
nft -f "$1"
python3 "$2"
INNER
