#!/usr/bin/env python3
"""Static checks for Dynamic Port Breakout (DPB) platform data.

For every SONiC device directory given (``device/<vendor>/<platform>``) the
script checks, without any SONiC runtime:

* ``platform.json``: every interface carries ``index``, ``lanes`` and
  ``breakout_modes``; every mode parses the way sonic-config-engine's
  ``portconfig.BreakoutCfg`` parses it; the alias list matches the number of
  child ports; two modes never collapse into the same parsed mode; the child
  port names of one interface never collide with another interface.
* every HWSKU (a subdirectory with a ``port_config.ini``):

  - with ``hwsku.json``: the PORT table sonic-cfggen derives from
    ``platform.json`` + ``hwsku.json`` must agree with ``port_config.ini`` on
    port names, lanes, index and speed. Alias differences are reported, since
    the running system uses the ``platform.json`` aliases as soon as
    ``hwsku.json`` exists.
  - without ``hwsku.json``: every ``port_config.ini`` row must be reachable as
    a breakout of a ``platform.json`` interface, i.e. a layout
    ``config interface breakout`` could produce. ``--emit-hwsku`` prints the
    equivalent ``hwsku.json``.
  - every ``*.config.bcm`` the HWSKU ships: ``port_flex_enable=1`` is set, a
    ``portmap`` exists for the first lane of every front-panel interface, and
    the logical port ids a breakout needs are not taken by another port macro
    (on Trident 3 logical ports 66 and 130 are the SFP+ management ports).

Exit status is 1 when any error was found. Warnings never fail the run.

Usage::

    scripts/check_breakout_modes.py device/celestica/x86_64-cel_seastone_2-r0
    scripts/check_breakout_modes.py --emit-hwsku device/dell/x86_64-dellemc_s5232f_c3538-r0/DellEMC-S5232f-P-10G
    scripts/check_breakout_modes.py --emit-hwsku --platform device/celestica/x86_64-cel_seastone_2-r0 /path/to/SONIX/port_config.ini
"""

import argparse
import json
import os
import re
import sys

PORT_STR = "Ethernet"
BRKOUT_PATTERN = r'(\d{1,6})x(\d{1,6}G?)(\[(\d{1,6}G?,?)*\])?(\((\d{1,6})\))?'
# Attributes sonic-config-engine copies from hwsku.json onto the child ports.
OPTIONAL_HWSKU_ATTRIBUTES = ("fec", "autoneg", "role")


class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.infos = []

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def info(self, msg):
        self.infos.append(msg)

    def dump(self, out=sys.stdout):
        for msg in self.infos:
            out.write("INFO    %s\n" % msg)
        for msg in self.warnings:
            out.write("WARNING %s\n" % msg)
        for msg in self.errors:
            out.write("ERROR   %s\n" % msg)


def speed_to_int(speed):
    if speed.endswith('G'):
        return int(speed[:-1]) * 1000
    return int(speed)


class ModeEntry:
    """One ``<n>x<speed>[alt,...](lanes)`` term of a breakout mode."""

    def __init__(self, num_ports, default_speed, supported, num_lanes):
        self.num_ports = num_ports
        self.default_speed = default_speed
        self.supported = frozenset(supported) | {default_speed}
        self.num_lanes = num_lanes

    def key(self):
        return (self.num_ports, self.supported, self.num_lanes)


def parse_mode(mode, num_lanes):
    """Parse a breakout mode string like portconfig.BreakoutCfg does.

    ``num_lanes`` is the lane count of the interface; it is the default lane
    assignment of a term without an explicit ``(n)``.
    """
    entries = []
    for term in mode.split("+"):
        m = re.fullmatch(BRKOUT_PATTERN, term)
        if not m:
            raise ValueError("breakout mode %r: term %r does not parse" % (mode, term))
        num_ports, default_speed, supported, _, _, assigned = m.groups()
        num_ports = int(num_ports)
        default = speed_to_int(default_speed)
        alt = []
        if supported:
            alt = [speed_to_int(s.strip()) for s in supported[1:-1].split(",") if s.strip()]
        lanes = int(assigned) if assigned else num_lanes
        if num_ports <= 0:
            raise ValueError("breakout mode %r: zero ports" % mode)
        if lanes % num_ports:
            raise ValueError("breakout mode %r: %d lanes do not split evenly over %d ports" % (mode, lanes, num_ports))
        entries.append(ModeEntry(num_ports, default, alt, lanes))
    return entries


def mode_key(entries):
    return tuple(e.key() for e in entries)


def child_ports(name, lanes, indexes, aliases, entries):
    """Return the child PORT entries of ``name`` in the given mode.

    Mirrors ``portconfig.BreakoutCfg.get_config`` (names, lanes, index, speed,
    alias, subport). Raises ValueError on inconsistent data.
    """
    base = int(name[len(PORT_STR):])
    lanes_used = sum(e.num_lanes for e in entries)
    total_ports = sum(e.num_ports for e in entries)
    if lanes_used > len(lanes):
        raise ValueError("%s: mode assigns %d lanes but the interface has %d" % (name, lanes_used, len(lanes)))
    if len(aliases) != total_ports:
        raise ValueError("%s: mode yields %d ports but lists %d aliases" % (name, total_ports, len(aliases)))
    ports = {}
    lane_id = 0
    alias_id = 0
    for e in entries:
        per_port = e.num_lanes // e.num_ports
        for _ in range(e.num_ports):
            child = PORT_STR + str(base + lane_id)
            ports[child] = {
                "alias": aliases[alias_id],
                "lanes": ",".join(lanes[lane_id:lane_id + per_port]),
                "speed": str(e.default_speed),
                "index": indexes[lane_id],
                "subport": "0" if total_ports == 1 else str(lane_id // per_port + 1),
                "_supported": e.supported,
            }
            lane_id += per_port
            alias_id += 1
    return ports


def read_json(path):
    with open(path) as f:
        return json.load(f)


def read_port_config(path):
    """Parse port_config.ini into {name: {column: value}} (same as portconfig)."""
    ports = {}
    titles = ["name", "lanes", "alias", "index"]
    with open(path) as f:
        for line in f:
            if line.startswith("#"):
                if "name" in line:
                    titles = line.strip("#").split()
                continue
            tokens = line.split()
            if len(tokens) < 2:
                continue
            name_idx = titles.index("name")
            data = {}
            for i, tok in enumerate(tokens):
                if i != name_idx and i < len(titles):
                    data[titles[i]] = tok
            data.setdefault("alias", tokens[name_idx])
            ports[tokens[name_idx]] = data
    return ports


class Platform:
    def __init__(self, platform_dir, report):
        self.dir = platform_dir
        self.name = os.path.basename(os.path.normpath(platform_dir))
        self.report = report
        self.interfaces = {}   # name -> dict(index=[...], lanes=[...], modes={mode: entries}, aliases={mode: [...]})
        self.load()

    def load(self):
        path = os.path.join(self.dir, "platform.json")
        if not os.path.isfile(path):
            self.report.error("%s: no platform.json, DPB is not available" % self.name)
            return
        data = read_json(path)
        ifs = data.get("interfaces")
        if not isinstance(ifs, dict) or not ifs:
            self.report.error("%s: platform.json has no 'interfaces'" % self.name)
            return
        lane_owner = {}
        for name, props in ifs.items():
            tag = "%s platform.json %s" % (self.name, name)
            if not name.startswith(PORT_STR) or not name[len(PORT_STR):].isdigit():
                self.report.error("%s: interface name is not Ethernet<n>" % tag)
                continue
            missing = [k for k in ("index", "lanes", "breakout_modes") if k not in props]
            if missing:
                self.report.error("%s: missing %s" % (tag, ", ".join(missing)))
                continue
            lanes = [x.strip() for x in str(props["lanes"]).split(",")]
            indexes = [x.strip() for x in str(props["index"]).split(",")]
            if len(lanes) != len(indexes):
                self.report.error("%s: %d lanes but %d index values" % (tag, len(lanes), len(indexes)))
                continue
            if not all(l.isdigit() for l in lanes):
                self.report.error("%s: non-numeric lane in %r" % (tag, props["lanes"]))
                continue
            for lane in lanes:
                if lane in lane_owner:
                    self.report.warn("%s: lane %s is also used by %s" % (tag, lane, lane_owner[lane]))
                lane_owner[lane] = name
            modes = {}
            aliases = {}
            seen = {}
            for mode, alias_list in props["breakout_modes"].items():
                try:
                    entries = parse_mode(mode, len(lanes))
                    child_ports(name, lanes, indexes, alias_list, entries)
                except ValueError as e:
                    self.report.error("%s: %s" % (tag, e))
                    continue
                key = mode_key(entries)
                if key in seen:
                    self.report.error("%s: modes %r and %r are the same mode; sonic-config-engine resolves both to the first one" % (tag, seen[key], mode))
                seen[key] = mode
                modes[mode] = entries
                aliases[mode] = list(alias_list)
            if not modes:
                self.report.error("%s: no usable breakout mode" % tag)
                continue
            self.interfaces[name] = {"lanes": lanes, "index": indexes, "modes": modes, "aliases": aliases}

        # Child names of one interface must not collide with another interface
        # or with its children (names are Ethernet<base + lane offset>).
        owner = {}
        for name, props in self.interfaces.items():
            base = int(name[len(PORT_STR):])
            for off in range(len(props["lanes"])):
                child = PORT_STR + str(base + off)
                if child in owner and owner[child] != name:
                    self.report.error("%s platform.json: %s and %s both own child port %s" % (self.name, owner[child], name, child))
                owner[child] = name

    def children(self, name, mode):
        props = self.interfaces[name]
        return child_ports(name, props["lanes"], props["index"], props["aliases"][mode], props["modes"][mode])

    def resolve_mode(self, name, mode):
        """Return the platform.json mode string matching ``mode`` like BreakoutCfg does."""
        props = self.interfaces[name]
        key = mode_key(parse_mode(mode, len(props["lanes"])))
        for cand, entries in props["modes"].items():
            if mode_key(entries) == key:
                return cand
        raise ValueError("%s: mode %r is not offered by platform.json" % (name, mode))

    def generate(self, hwsku_json):
        """PORT table from platform.json + hwsku.json, like parse_platform_json_file."""
        ports = {}
        ifs = hwsku_json.get("interfaces", {})
        for name in self.interfaces:
            if name not in ifs:
                continue
            mode = self.resolve_mode(name, ifs[name]["default_brkout_mode"])
            kids = self.children(name, mode)
            for child in kids:
                if child in ifs:
                    for k, v in ifs[child].items():
                        if k in OPTIONAL_HWSKU_ATTRIBUTES:
                            for other in kids.values():
                                other[k] = v
            ports.update(kids)
        return ports

    def infer(self, port_config):
        """Map a port_config.ini layout onto breakout modes.

        Returns {interface: (mode, exact, rows)} and reports rows that no mode
        can produce. ``exact`` is False when the rows run at a bracket speed
        of the mode rather than its default speed.
        """
        by_first_lane = {}
        for name, props in self.interfaces.items():
            for lane in props["lanes"]:
                by_first_lane[lane] = name
        grouped = {}
        unmapped = []
        for pname, row in port_config.items():
            lanes = [x.strip() for x in row["lanes"].split(",")]
            parent = by_first_lane.get(lanes[0])
            if parent is None or not set(lanes) <= set(self.interfaces[parent]["lanes"]):
                unmapped.append(pname)
                continue
            grouped.setdefault(parent, []).append(pname)
        result = {}
        for parent, rows in grouped.items():
            layout = sorted(((p, port_config[p]) for p in rows), key=lambda x: int(x[1]["lanes"].split(",")[0]))
            want = [(p, r["lanes"].replace(" ", "")) for p, r in layout]
            exact = None
            override = None
            for mode in self.interfaces[parent]["modes"]:
                kids = self.children(parent, mode)
                have = [(k, v["lanes"]) for k, v in kids.items()]
                if have != want:
                    continue
                speeds_ok = all("speed" not in r or int(r["speed"]) in kids[p]["_supported"] for p, r in layout)
                default_ok = all("speed" not in r or int(r["speed"]) == int(kids[p]["speed"]) for p, r in layout)
                if default_ok:
                    exact = exact or mode
                elif speeds_ok:
                    override = override or mode
            if exact:
                result[parent] = (exact, True, layout)
            elif override:
                result[parent] = (override, False, layout)
            else:
                unmapped.extend(p for p, _ in layout)
        return result, unmapped


def hwsku_from_port_config(platform, port_config):
    """Return (hwsku.json dict, notes) equivalent to a port_config.ini layout."""
    inferred, unmapped = platform.infer(port_config)
    notes = []
    for p in unmapped:
        notes.append("%s (lanes %s) cannot be produced by any breakout mode of platform.json" % (p, port_config[p]["lanes"]))
    out = {"interfaces": {}}
    for name in platform.interfaces:
        if name not in inferred:
            notes.append("%s is not in port_config.ini; without an entry it would vanish from PORT" % name)
            continue
        mode, exact, layout = inferred[name]
        entry = {"default_brkout_mode": mode}
        for attr in ("fec", "autoneg"):
            values = {r.get(attr) for _, r in layout if r.get(attr)}
            if len(values) == 1:
                entry[attr] = values.pop()
            elif len(values) > 1:
                notes.append("%s: children disagree on %s (%s); hwsku.json applies one value to all children" % (name, attr, sorted(values)))
        out["interfaces"][name] = entry
        if not exact:
            speeds = sorted({r.get("speed") for _, r in layout})
            notes.append("%s: mode %s runs at %s by default; port_config.ini wants %s, which needs a speed override after breakout" % (name, mode, platform.children(name, mode)[name]["speed"], ",".join(speeds)))
    return out, notes


def parse_config_bcm(path):
    """Return (properties dict, portmaps {logical: (phys, speed, opts)})."""
    props = {}
    portmaps = {}
    with open(path, errors="replace") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            props[key] = value
            m = re.fullmatch(r"portmap_(\d+)(?:\.\d+)?", key)
            if m:
                parts = value.split(":")
                try:
                    portmaps[int(m.group(1))] = (int(parts[0]), parts[1] if len(parts) > 1 else "", parts[2:])
                except ValueError:
                    pass
    return props, portmaps


def check_config_bcm(platform, hwsku, path, report, dpb=True):
    """``dpb``: the HWSKU has a hwsku.json, so the breakout CLI can use every platform.json mode."""
    tag = "%s/%s %s" % (platform.name, hwsku, os.path.basename(path))
    props, portmaps = parse_config_bcm(path)
    if props.get("port_flex_enable") != "1":
        report.error("%s: port_flex_enable=1 is missing, the SDK will refuse runtime breakout" % tag)
    if not portmaps:
        report.warn("%s: no portmap entries found, skipping logical port checks" % tag)
        return
    by_phys = {phys: logical for logical, (phys, _, _) in portmaps.items()}
    mgmt = [logical for logical, (_, _, opts) in portmaps.items() if "m" in opts]
    for name, props_if in sorted(platform.interfaces.items(), key=lambda x: int(x[0][len(PORT_STR):])):
        lanes = [int(l) for l in props_if["lanes"]]
        if len(lanes) < 2:
            continue  # single-lane ports (SFP+ management) are not broken out
        first = lanes[0]
        if first not in by_phys:
            report.error("%s: no portmap for physical port %d (%s), the port macro is not configured" % (tag, first, name))
            continue
        base_logical = by_phys[first]
        # The SDK numbers the children of a flexed macro after the lane they
        # start on (Trident 3: logical = macro's logical port + lane offset;
        # physical 65 is logical 67 because 66 is a management port), so the
        # ids of every child the offered modes can create must be free.
        # Hardware (S5232F port 32, #102/#172): a child on a management
        # port's id is refused ("Dynamic port config failed 130"), so a mode
        # that needs one must not be offered.
        needed = {}
        for mode in props_if["modes"]:
            for child, row in platform.children(name, mode).items():
                offset = lanes.index(int(row["lanes"].split(",")[0]))
                needed.setdefault(offset, []).append((mode, child))
        for offset in sorted(needed):
            logical = base_logical + offset
            modes = sorted({m for m, _ in needed[offset]})
            if logical in mgmt:
                report.error("%s: %s mode %s needs logical port %d for its child on lane %d, but that is the management "
                             "port on physical port %d; the SAI refuses it, drop the mode from platform.json"
                             % (tag, name, ", ".join(modes), logical, lanes[offset], portmaps[logical][0]))
                continue
            if logical in portmaps and portmaps[logical][0] not in lanes:
                report.error("%s: logical port %d is needed for lane %d of %s but maps physical port %d"
                             % (tag, logical, lanes[offset], name, portmaps[logical][0]))
            if props.get("port_flex_enable_%d" % logical) == "0":
                report.error("%s: port_flex_enable_%d=0 pins a lane of %s" % (tag, logical, name))
    # Oversubscribed Trident 3: a 50G port next to 25G ports in the same pair
    # of port macros ("sister" ports) is refused unless enabled explicitly
    # (Seastone 2 2x50G, #172: "Mixed-sister oversub ports are not supported").
    speeds = {speed_to_int(row["speed"]) for name, p in platform.interfaces.items() if len(p["lanes"]) > 1
              for mode in p["modes"] for row in platform.children(name, mode).values()}
    if (props.get("oversubscribe_mode") == "1" and 50000 in speeds and 25000 in speeds
            and props.get("oversubscribe_mixed_sister_25_50_enable") != "1"):
        (report.error if dpb else report.warn)(
            "%s: platform.json offers 50G and 25G breakouts but oversubscribe_mixed_sister_25_50_enable=1 is "
            "missing; the SAI refuses a 50G port next to 25G ones" % tag)
    for logical in mgmt:
        if props.get("port_flex_enable_%d" % logical) != "0":
            report.info("%s: management port %d (physical %d) is not excluded from flexing (port_flex_enable_%d=0)"
                        % (tag, logical, portmaps[logical][0], logical))


def check_hwsku(platform, hwsku_dir, report, emit_hwsku=False):
    hwsku = os.path.basename(os.path.normpath(hwsku_dir))
    tag = "%s/%s" % (platform.name, hwsku)
    ini_path = os.path.join(hwsku_dir, "port_config.ini")
    hwsku_path = os.path.join(hwsku_dir, "hwsku.json")
    port_config = read_port_config(ini_path) if os.path.isfile(ini_path) else {}

    if os.path.isfile(hwsku_path):
        hwsku_json = read_json(hwsku_path)
        ifs = hwsku_json.get("interfaces", {})
        for name, entry in ifs.items():
            if name not in platform.interfaces:
                if name in port_config or not any(name in platform.children(p, m) for p in platform.interfaces for m in platform.interfaces[p]["modes"]):
                    report.error("%s hwsku.json: %s is not an interface of platform.json" % (tag, name))
                continue
            if "default_brkout_mode" not in entry:
                report.error("%s hwsku.json: %s has no default_brkout_mode" % (tag, name))
                continue
            try:
                platform.resolve_mode(name, entry["default_brkout_mode"])
            except ValueError as e:
                report.error("%s hwsku.json: %s" % (tag, e))
        for name in platform.interfaces:
            if name not in ifs:
                report.error("%s hwsku.json: %s has no default_brkout_mode, sonic-cfggen drops it from PORT" % (tag, name))
        if report.errors:
            return
        generated = platform.generate(hwsku_json)
        report.info("%s: hwsku.json yields %d ports in %d breakout groups" % (tag, len(generated), len(ifs)))
        if port_config:
            if set(generated) != set(port_config):
                report.error("%s: PORT from hwsku.json %s differs from port_config.ini %s" % (
                    tag, sorted(set(generated) - set(port_config)), sorted(set(port_config) - set(generated))))
            alias_diffs = 0
            for name in sorted(set(generated) & set(port_config), key=lambda x: int(x[len(PORT_STR):])):
                gen, ref = generated[name], port_config[name]
                for col in ("lanes", "index", "speed", "fec", "autoneg"):
                    if col in ref and str(gen.get(col)) != str(ref[col]):
                        report.error("%s: %s %s is %s from hwsku.json but %s in port_config.ini" % (tag, name, col, gen.get(col), ref[col]))
                if gen["alias"] != ref.get("alias"):
                    alias_diffs += 1
            if alias_diffs:
                report.warn("%s: %d of %d aliases differ between platform.json and port_config.ini (e.g. %s vs %s); "
                            "with hwsku.json present the system uses the platform.json aliases"
                            % (tag, alias_diffs, len(generated), generated[name]["alias"], port_config[name].get("alias")))
    elif port_config:
        inferred, unmapped = platform.infer(port_config)
        for p in unmapped:
            report.error("%s port_config.ini: %s (lanes %s) is not a breakout mode of platform.json" % (tag, p, port_config[p]["lanes"]))
        overrides = [name for name, (mode, exact, _) in inferred.items() if not exact]
        modes = {}
        for name, (mode, _, _) in inferred.items():
            modes[mode] = modes.get(mode, 0) + 1
        report.info("%s: no hwsku.json (DPB CLI unusable on this HWSKU); port_config.ini maps onto %s%s"
                    % (tag, modes, ", %d groups need a speed override" % len(overrides) if overrides else ""))
        if emit_hwsku:
            out, notes = hwsku_from_port_config(platform, port_config)
            print("--- equivalent hwsku.json for %s" % tag)
            print(json.dumps(out, indent=4))
            for n in notes:
                print("note: " + n)
    else:
        report.warn("%s: neither port_config.ini nor hwsku.json" % tag)

    for entry in sorted(os.listdir(hwsku_dir)):
        if entry.endswith(".config.bcm"):
            check_config_bcm(platform, hwsku, os.path.join(hwsku_dir, entry), report,
                             dpb=os.path.isfile(os.path.join(hwsku_dir, "hwsku.json")))


def check_platform(platform_dir, report, emit_hwsku=False, only_hwsku=None):
    platform = Platform(platform_dir, report)
    if not platform.interfaces:
        return platform
    report.info("%s: %d interfaces, modes %s" % (
        platform.name, len(platform.interfaces),
        sorted({m for p in platform.interfaces.values() for m in p["modes"]})))
    for entry in sorted(os.listdir(platform_dir)):
        hwsku_dir = os.path.join(platform_dir, entry)
        if only_hwsku and entry != only_hwsku:
            continue
        if os.path.isdir(hwsku_dir) and os.path.isfile(os.path.join(hwsku_dir, "port_config.ini")):
            check_hwsku(platform, hwsku_dir, report, emit_hwsku)
    return platform


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="device/<vendor>/<platform> directories, HWSKU directories, or (with --platform) port_config.ini files")
    ap.add_argument("--emit-hwsku", action="store_true", help="print the hwsku.json equivalent of each port_config.ini without hwsku.json")
    ap.add_argument("--platform", help="platform directory for bare port_config.ini arguments")
    args = ap.parse_args(argv)
    report = Report()
    for path in args.paths:
        path = os.path.normpath(path)
        if os.path.isfile(path):
            if not args.platform:
                ap.error("%s: a port_config.ini argument needs --platform" % path)
            platform = Platform(args.platform, report)
            if platform.interfaces:
                port_config = read_port_config(path)
                out, notes = hwsku_from_port_config(platform, port_config)
                print("--- equivalent hwsku.json for %s" % path)
                print(json.dumps(out, indent=4))
                for n in notes:
                    print("note: " + n)
        elif os.path.isfile(os.path.join(path, "platform.json")):
            check_platform(path, report, args.emit_hwsku)
        elif os.path.isfile(os.path.join(os.path.dirname(path), "platform.json")):
            check_platform(os.path.dirname(path), report, args.emit_hwsku, only_hwsku=os.path.basename(path))
        else:
            report.error("%s: not a platform or HWSKU directory" % path)
    report.dump()
    print("%d error(s), %d warning(s)" % (len(report.errors), len(report.warnings)))
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
