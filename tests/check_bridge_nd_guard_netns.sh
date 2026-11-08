#!/bin/bash
# Run with CAP_SYS_ADMIN/CAP_NET_ADMIN on a disposable builder, not in host netns.
set -euo pipefail
rules=$(readlink -f "${1:-$(dirname "$0")/../files/image_config/ebtables/bridge-nd-filter.nft}")
unshare --net bash -s -- "$rules" <<'INNER'
set -euo pipefail
nft add table bridge issue184_sentinel
nft 'add chain bridge issue184_sentinel untouched'
nft 'add rule bridge issue184_sentinel untouched counter accept'
nft list table bridge issue184_sentinel > /tmp/issue184-sentinel-before.$$
trap 'rm -f /tmp/issue184-sentinel-before.$$ /tmp/issue184-sentinel-after.$$' EXIT
nft --check -f "$1"
nft -f "$1"
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
INNER
