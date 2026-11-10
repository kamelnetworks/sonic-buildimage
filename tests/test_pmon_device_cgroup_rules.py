"""Start path of the rendered pmon.sh (files/build_templates/docker_image_ctl.j2).

pmon's --device-cgroup-rule list is computed from the char majors in
/proc/devices, and a container keeps the rules it was created with. Dynamic
majors (ipmidev, ...) can change between boots, so starting an existing
container must recreate it when its rules differ from this boot's (#203).

The template is rendered with jinja2 and its functions are sourced into bash
with docker, udevadm and /usr/local/bin/container stubbed; /proc/devices is
replaced by a fixture file.
"""

import json
import os
import pathlib
import subprocess
import sys

import jinja2
import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "files" / "build_templates" / "docker_image_ctl.j2"

PLATFORM = "x86_64-dellemc_s5232f_c3538-r0"
HWSKU = "DellEMC-S5232f-C32"


def render(container, asic_platform="broadcom"):
    env = jinja2.Environment(keep_trailing_newline=True)
    return env.from_string(TEMPLATE.read_text()).render(
        docker_container_name=container,
        sonic_asic_platform=asic_platform,
        docker_image_run_opt="-t",
        docker_image_name="docker-" + container,
    )


def proc_devices(majors):
    """A /proc/devices with the given {name: major} char devices."""
    chars = {"mem": 1, "tty": 4, **majors}
    lines = ["Character devices:"]
    lines += ["%3d %s" % (major, name) for name, major in sorted(chars.items(), key=lambda x: x[1])]
    lines += ["", "Block devices:", "  8 sd", "259 blkext", ""]
    return "\n".join(lines)


def inspect_json(rules, hwsku=HWSKU):
    return json.dumps([{
        "Name": "/pmon",
        "HostConfig": {"DeviceCgroupRules": rules},
        "Mounts": [{
            "Source": "/usr/share/sonic/device/%s/%s" % (PLATFORM, hwsku),
            "Destination": "/usr/share/sonic/hwsku",
        }],
    }])


DRIVER = r"""
source "$PMON_SH"

docker() {
    echo "docker $*" >> "$CALLS"
    case "$1" in
        inspect)
            [ -f "$INSPECT" ] || return 1
            cat "$INSPECT"
            ;;
    esac
    return 0
}
udevadm() { :; }
/usr/local/bin/container() { echo "container $*" >> "$CALLS"; }
preStartAction() { :; }
postStartAction() { :; }

start
"""


NO_CONTAINER = object()


class Switch:
    """A fake host: /proc/devices, an optional existing pmon container."""

    def __init__(self, tmp_path, script):
        self.tmp = tmp_path
        self.calls = tmp_path / "calls"
        self.inspect = tmp_path / "inspect.json"
        self.devices = tmp_path / "proc_devices"
        # Only the function definitions: the end of the script reads
        # /etc/sonic/sonic-environment and dispatches on $1.
        script = script.split("# read SONiC immutable variables")[0]
        reader = "/p' /proc/devices |"
        assert script.count(reader) == 1
        script = script.replace(reader, "/p' \"$PROC_DEVICES\" |")
        self.script = tmp_path / "pmon.sh"
        self.script.write_text(script)
        # The template calls 'python' to parse docker inspect output.
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "python").symlink_to(sys.executable)

    def start(self, majors, container_rules=NO_CONTAINER, hwsku=HWSKU):
        self.devices.write_text(proc_devices(majors))
        if container_rules is NO_CONTAINER:
            self.inspect.unlink(missing_ok=True)
        else:
            self.inspect.write_text(inspect_json(container_rules, hwsku))
        self.calls.write_text("")
        env = dict(os.environ,
                   PATH="%s:%s" % (self.bin, os.environ["PATH"]),
                   PMON_SH=str(self.script), CALLS=str(self.calls),
                   INSPECT=str(self.inspect), PROC_DEVICES=str(self.devices),
                   PLATFORM=PLATFORM, HWSKU=HWSKU, NUM_ASIC="1")
        proc = subprocess.run(["bash", "-c", DRIVER], env=env, cwd=self.tmp,
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout, self.calls.read_text().splitlines()


def created_rules(calls):
    create = [c for c in calls if c.startswith("docker create ")]
    assert len(create) == 1, calls
    words = create[0].split(" --device-cgroup-rule=")[1:]
    return sorted(w.split(" rwm")[0] + " rwm" for w in words)


MAJORS = {"i2c": 89, "spi": 153, "ipmidev": 242, "gpiochip": 254}
RULES = ["c 89:* rwm", "c 153:* rwm", "c 242:* rwm", "c 254:* rwm"]


@pytest.fixture
def switch(tmp_path):
    return Switch(tmp_path, render("pmon"))


def test_new_container_gets_this_boots_rules(switch):
    _, calls = switch.start(MAJORS)
    assert not [c for c in calls if c.startswith("docker rm")]
    assert created_rules(calls) == sorted(RULES)
    assert "container start pmon" in calls


def test_same_rules_start_existing_container(switch):
    out, calls = switch.start(MAJORS, container_rules=RULES)
    assert "Starting existing pmon container" in out
    assert calls == ["docker inspect --type container pmon", "container start pmon"]


def test_rule_order_does_not_recreate(switch):
    _, calls = switch.start(MAJORS, container_rules=list(reversed(RULES)))
    assert calls == ["docker inspect --type container pmon", "container start pmon"]


def test_changed_ipmidev_major_recreates(switch):
    out, calls = switch.start(dict(MAJORS, ipmidev=243), container_rules=RULES)
    assert calls.count("docker rm -f pmon") == 1
    assert calls.index("docker rm -f pmon") < [i for i, c in enumerate(calls) if c.startswith("docker create ")][0]
    assert created_rules(calls) == sorted(["c 89:* rwm", "c 153:* rwm", "c 243:* rwm", "c 254:* rwm"])
    assert "container start pmon" in calls
    # The log names the old and the new rules.
    line = [l for l in out.splitlines() if "stale device-cgroup rules" in l]
    assert len(line) == 1, out
    assert "c 242:* rwm" in line[0] and "c 243:* rwm" in line[0]


def test_new_major_class_recreates(switch):
    # A class that registers on this boot only (e.g. uio) is also a change.
    _, calls = switch.start(dict(MAJORS, uio=240), container_rules=RULES)
    assert calls.count("docker rm -f pmon") == 1
    assert "c 240:* rwm" in created_rules(calls)


def test_container_without_rules_recreates(switch):
    # A container created before the rules existed (docker inspect: null).
    _, calls = switch.start(MAJORS, container_rules=None)
    assert calls.count("docker rm -f pmon") == 1
    assert created_rules(calls) == sorted(RULES)


def test_hwsku_change_still_recreates_once(switch):
    out, calls = switch.start(MAJORS, container_rules=RULES, hwsku="Other-HWSKU")
    assert calls.count("docker rm -f pmon") == 1
    assert "Removing obsolete pmon container with HWSKU" in out
    assert "stale device-cgroup rules" not in out


@pytest.mark.parametrize("container,asic", [
    ("syncd", "broadcom"), ("syncd", "marvell-teralynx"),
    ("swss", "broadcom"), ("database", "broadcom"), ("bgp", "broadcom"),
])
def test_other_containers_have_no_pmon_rule_check(container, asic):
    script = render(container, asic)
    for word in ("device-cgroup", "DeviceCgroupRules", "PMON_", "get_pmon_"):
        assert word not in script
    assert 'if [ x"$DOCKERMOUNT" == x"$MOUNTPATH" ]; then' in script
