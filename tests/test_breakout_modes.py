#!/usr/bin/env python3
"""Dynamic Port Breakout data checks for the 202611 Broadcom platforms.

Runs scripts/check_breakout_modes.py over the in-scope Trident 3 platforms
(Celestica Seastone 2, Dell S5232F-ON) and pins down the breakout-mode
semantics the SONIX HWSKUs rely on. Pure Python, no SONiC runtime needed:

    python3 -m unittest tests/test_breakout_modes.py -v
"""

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "check_breakout_modes.py")
SEASTONE2 = os.path.join(REPO, "device", "celestica", "x86_64-cel_seastone_2-r0")
S5232F = os.path.join(REPO, "device", "dell", "x86_64-dellemc_s5232f_c3538-r0")

spec = importlib.util.spec_from_file_location("check_breakout_modes", SCRIPT)
cbm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cbm)


def run_check(*paths):
    report = cbm.Report()
    platforms = [cbm.check_platform(p, report) for p in paths]
    return report, platforms


class PlatformDataTest(unittest.TestCase):
    """The shipped platform.json / hwsku.json / port_config.ini / config.bcm agree."""

    def assert_clean(self, platform_dir):
        report, _ = run_check(platform_dir)
        buf = io.StringIO()
        report.dump(buf)
        self.assertEqual(report.errors, [], buf.getvalue())

    def test_seastone2(self):
        self.assert_clean(SEASTONE2)

    def test_s5232f(self):
        self.assert_clean(S5232F)

    def test_seastone2_modes(self):
        _, (plat,) = run_check(SEASTONE2)
        for name, props in plat.interfaces.items():
            if len(props["lanes"]) == 4:
                self.assertEqual(sorted(props["modes"]), ["1x100G[40G]", "2x50G", "4x10G", "4x25G[10G]"], name)
        self.assertEqual(sorted(plat.interfaces["Ethernet128"]["modes"]), ["1x10G[1G]"])
        kids = plat.children("Ethernet8", "4x10G")
        self.assertEqual([(k, v["lanes"], v["speed"], v["alias"], v["index"]) for k, v in kids.items()], [
            ("Ethernet8", "9", "10000", "Eth3/1", "3"),
            ("Ethernet9", "10", "10000", "Eth3/2", "3"),
            ("Ethernet10", "11", "10000", "Eth3/3", "3"),
            ("Ethernet11", "12", "10000", "Eth3/4", "3"),
        ])
        kids = plat.children("Ethernet124", "2x50G")
        self.assertEqual([(k, v["lanes"], v["speed"]) for k, v in kids.items()],
                         [("Ethernet124", "125,126", "50000"), ("Ethernet126", "127,128", "50000")])

    def test_s5232f_modes(self):
        _, (plat,) = run_check(S5232F)
        for name, props in plat.interfaces.items():
            if len(props["lanes"]) == 4:
                # Port 32 has no 4-way mode: its 4th child would be logical 130, the second SFP+.
                expected = ["1x100G[50G,40G]", "2x50G"] + ([] if name == "Ethernet124" else ["4x25G[10G]"])
                self.assertEqual(sorted(props["modes"]), expected, name)
        # Second SFP+ port shares physical lane 128 with the last QSFP28 on Trident 3 (vendor config convention).
        self.assertEqual(plat.interfaces["Ethernet129"]["lanes"], ["128"])

    def test_s5232f_stock_hwsku_matches_port_config(self):
        """DellEMC-S5232f-C32 ships hwsku.json; generated PORT equals port_config.ini apart from aliases."""
        _, (plat,) = run_check(S5232F)
        hwsku = cbm.read_json(os.path.join(S5232F, "DellEMC-S5232f-C32", "hwsku.json"))
        generated = plat.generate(hwsku)
        ini = cbm.read_port_config(os.path.join(S5232F, "DellEMC-S5232f-C32", "port_config.ini"))
        self.assertEqual(set(generated), set(ini))
        for name in ini:
            self.assertEqual(generated[name]["lanes"], ini[name]["lanes"], name)
            self.assertEqual(generated[name]["speed"], ini[name]["speed"], name)
        self.assertEqual(generated["Ethernet0"]["alias"], "etp1")
        self.assertEqual(ini["Ethernet0"]["alias"], "hundredGigE1/1")

    def test_seastone2_stock_hwsku_matches_port_config(self):
        _, (plat,) = run_check(SEASTONE2)
        hwsku = cbm.read_json(os.path.join(SEASTONE2, "Seastone_2", "hwsku.json"))
        generated = plat.generate(hwsku)
        ini = cbm.read_port_config(os.path.join(SEASTONE2, "Seastone_2", "port_config.ini"))
        self.assertEqual(set(generated), set(ini))
        for name in ini:
            for col in ("lanes", "speed", "alias", "index"):
                self.assertEqual(generated[name][col], ini[name][col], (name, col))
            # fec/autoneg come from hwsku.json and are copied to every child port
            self.assertEqual(generated[name]["autoneg"], "off")
        self.assertEqual(generated["Ethernet0"]["fec"], "rs")
        self.assertEqual(generated["Ethernet128"]["fec"], "none")


class ModeSemanticsTest(unittest.TestCase):
    """How sonic-config-engine matches hwsku.json modes against platform.json."""

    def test_bracket_speeds_distinguish_modes(self):
        # "4x10G" and "4x25G[10G]" are different modes: a hwsku.json saying
        # "4x10G" only resolves when platform.json lists exactly that key.
        self.assertNotEqual(cbm.mode_key(cbm.parse_mode("4x10G", 4)), cbm.mode_key(cbm.parse_mode("4x25G[10G]", 4)))
        self.assertNotEqual(cbm.mode_key(cbm.parse_mode("1x100G", 4)), cbm.mode_key(cbm.parse_mode("1x100G[40G]", 4)))
        # Order inside the brackets does not matter.
        self.assertEqual(cbm.mode_key(cbm.parse_mode("1x100G[50G,40G]", 4)), cbm.mode_key(cbm.parse_mode("1x100G[40G,50G]", 4)))

    def test_mixed_mode(self):
        entries = cbm.parse_mode("1x50G(2)+2x25G(2)", 4)
        kids = cbm.child_ports("Ethernet0", ["1", "2", "3", "4"], ["1"] * 4, ["a", "b", "c"], entries)
        self.assertEqual([(k, v["lanes"], v["speed"]) for k, v in kids.items()],
                         [("Ethernet0", "1,2", "50000"), ("Ethernet2", "3", "25000"), ("Ethernet3", "4", "25000")])

    def test_bad_modes(self):
        with self.assertRaises(ValueError):
            cbm.parse_mode("3x25G", 4)
        with self.assertRaises(ValueError):
            cbm.parse_mode("4x25G[10G", 4)
        with self.assertRaises(ValueError):
            cbm.child_ports("Ethernet0", ["1", "2", "3", "4"], ["1"] * 4, ["a"], cbm.parse_mode("4x25G", 4))

    def equivalent_platform(self, second_aliases):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        data = {"interfaces": {"Ethernet0": {
            "index": "1,1,1,1", "lanes": "1,2,3,4",
            "breakout_modes": {
                "4x25G[10G](4)": ["a", "b", "c", "d"],
                "4x10G[25G](4)": second_aliases,
            },
        }}}
        with open(os.path.join(directory, "platform.json"), "w") as handle:
            json.dump(data, handle)
        report = cbm.Report()
        return report, cbm.Platform(directory, report)

    def test_equivalent_speed_modes_keep_requested_default(self):
        report, platform = self.equivalent_platform(["a", "b", "c", "d"])
        self.assertEqual(report.errors, [])
        for mode, speed in (("4x25G[10G](4)", "25000"),
                            ("4x10G[25G](4)", "10000")):
            ports = platform.generate({"interfaces": {
                "Ethernet0": {"default_brkout_mode": mode}}})
            self.assertEqual([p["alias"] for p in ports.values()], ["a", "b", "c", "d"])
            self.assertEqual({p["speed"] for p in ports.values()}, {speed})

    def test_equivalent_speed_modes_with_different_aliases_are_rejected(self):
        report, _ = self.equivalent_platform(["w", "x", "y", "z"])
        self.assertTrue(any("different aliases" in error for error in report.errors), report.errors)



class SonixHwskuTest(unittest.TestCase):
    """What a SONIX HWSKU needs: a hwsku.json equivalent to its port_config.ini."""

    def write_ini(self, rows, header="# name lanes alias index speed"):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False)
        tmp.write(header + "\n")
        for row in rows:
            tmp.write(" ".join(row) + "\n")
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def seastone2_rows(self, split_port3=True):
        rows = []
        for i in range(1, 33):
            base = 4 * (i - 1)
            if i == 3 and split_port3:
                for k in range(4):
                    rows.append(("Ethernet%d" % (base + k), str(base + k + 1), "Eth3/%d" % (k + 1), "3", "10000"))
            else:
                rows.append(("Ethernet%d" % base, ",".join(str(base + k + 1) for k in range(4)), "Eth%d/1" % i, str(i), "100000"))
        rows.append(("Ethernet128", "129", "Eth33/1", "33", "10000"))
        return rows

    def test_lab_seastone2_layout(self):
        """ixp-lab-sw2/sw3: port 3 as 4x10G, everything else 1x100G."""
        _, (plat,) = run_check(SEASTONE2)
        ini = cbm.read_port_config(self.write_ini(self.seastone2_rows()))
        hwsku, notes = cbm.hwsku_from_port_config(plat, ini)
        self.assertEqual(notes, [])
        modes = {k: v["default_brkout_mode"] for k, v in hwsku["interfaces"].items()}
        self.assertEqual(modes["Ethernet8"], "4x10G")
        self.assertEqual(modes["Ethernet0"], "1x100G[40G]")
        self.assertEqual(modes["Ethernet128"], "1x10G[1G]")
        self.assertEqual(len(modes), 33)
        # Round trip: the hwsku.json reproduces the port_config.ini layout.
        generated = plat.generate(hwsku)
        self.assertEqual({k: (v["lanes"], v["speed"]) for k, v in generated.items()},
                         {k: (v["lanes"], v["speed"]) for k, v in ini.items()})

    def test_fec_column_is_carried(self):
        """ixp-sk2-sw1 style port_config.ini with an fec column."""
        _, (plat,) = run_check(SEASTONE2)
        rows = [(r[0], r[1], r[2], "none" if r[4] == "10000" else "rs", r[3], r[4]) for r in self.seastone2_rows()]
        ini = cbm.read_port_config(self.write_ini(rows, "# name lanes alias fec index speed"))
        hwsku, notes = cbm.hwsku_from_port_config(plat, ini)
        self.assertEqual(notes, [])
        self.assertEqual(hwsku["interfaces"]["Ethernet8"], {"default_brkout_mode": "4x10G", "fec": "none"})
        self.assertEqual(hwsku["interfaces"]["Ethernet0"], {"default_brkout_mode": "1x100G[40G]", "fec": "rs"})

    def test_lab_s5232f_layout_needs_speed_override(self):
        """ixp-lab-sw4: port 3 as 4x10G. S5232F only offers 4x25G[10G], so 10G is a speed override."""
        _, (plat,) = run_check(S5232F)
        rows = []
        for i in range(1, 33):
            base = 4 * (i - 1)
            if i == 3:
                for k in range(4):
                    rows.append(("Ethernet%d" % (base + k), str(base + k + 1), "tenGigE1/3/%d" % (k + 1), "3", "10000"))
            else:
                rows.append(("Ethernet%d" % base, ",".join(str(base + k + 1) for k in range(4)), "hundredGigE1/%d" % i, str(i), "100000"))
        rows.append(("Ethernet128", "129", "tenGigE1/33", "33", "10000"))
        rows.append(("Ethernet129", "128", "tenGigE1/34", "34", "10000"))
        ini = cbm.read_port_config(self.write_ini(rows))
        hwsku, notes = cbm.hwsku_from_port_config(plat, ini)
        self.assertEqual(hwsku["interfaces"]["Ethernet8"]["default_brkout_mode"], "4x25G[10G]")
        self.assertEqual(hwsku["interfaces"]["Ethernet0"]["default_brkout_mode"], "1x100G[50G,40G]")
        self.assertEqual(len(notes), 1)
        self.assertIn("Ethernet8: mode 4x25G[10G] runs at 25000 by default", notes[0])

    def test_unreachable_layout_is_reported(self):
        _, (plat,) = run_check(SEASTONE2)
        rows = self.seastone2_rows(split_port3=False)
        # 1x50G on two lanes with the other two lanes dark is not a Seastone 2 mode.
        rows[0] = ("Ethernet0", "1,2", "Eth1/1", "1", "50000")
        ini = cbm.read_port_config(self.write_ini(rows))
        _, notes = cbm.hwsku_from_port_config(plat, ini)
        self.assertTrue(any("Ethernet0 (lanes 1,2) cannot be produced" in n for n in notes), notes)


class ConfigBcmTest(unittest.TestCase):
    def test_missing_flex_is_an_error(self):
        _, (plat,) = run_check(SEASTONE2)
        with tempfile.NamedTemporaryFile("w", suffix=".config.bcm", delete=False) as f:
            f.write("portmap_1=1:100\nportmap_5=5:100\n")
        self.addCleanup(os.unlink, f.name)
        report = cbm.Report()
        cbm.check_config_bcm(plat, "X", f.name, report)
        self.assertTrue(any("port_flex_enable=1 is missing" in e for e in report.errors), report.errors)
        self.assertTrue(any("no portmap for physical port 9" in e for e in report.errors), report.errors)

    def write_td3_config(self, extra=""):
        """A minimal Trident 3 style config.bcm: physical 65 starts at logical 67 because 66 is the management port."""
        f = tempfile.NamedTemporaryFile("w", suffix=".config.bcm", delete=False)
        f.write("port_flex_enable=1\n")
        for lane in range(1, 129, 4):
            f.write("portmap_%d=%d:100\n" % (lane if lane < 65 else lane + 2, lane))
        f.write("portmap_66=129:10:m\n")
        f.write(extra)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_logical_port_collision(self):
        _, (plat,) = run_check(SEASTONE2)
        report = cbm.Report()
        cbm.check_config_bcm(plat, "X", self.write_td3_config(), report)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])
        self.assertTrue(any("management port 66" in i for i in report.infos))

        # Numbering physical 65 as logical 65 leaves no room: lane 66 would need
        # logical 66, the management port. Only the 4-way modes start a child
        # on lane 66; 2x50G children start on lanes 65 and 67.
        report = cbm.Report()
        cbm.check_config_bcm(plat, "X", self.write_td3_config("portmap_65=65:100\n"), report)
        self.assertTrue(any("Ethernet64 mode 4x10G, 4x25G[10G] needs logical port 66 for its child on lane 66, "
                            "but that is the management port" in e for e in report.errors), report.errors)

        # Any other port squatting on an id the breakout needs is a plain error.
        report = cbm.Report()
        cbm.check_config_bcm(plat, "X", self.write_td3_config("portmap_70=131:10\n"), report)
        self.assertTrue(any("logical port 70 is needed for lane 68 of Ethernet64 but maps physical port 131" in e
                            for e in report.errors), report.errors)

        # Pinning a child's logical port is an error too.
        report = cbm.Report()
        cbm.check_config_bcm(plat, "X", self.write_td3_config("port_flex_enable_2=0\n"), report)
        self.assertTrue(any("port_flex_enable_2=0 pins a lane of Ethernet0" in e for e in report.errors), report.errors)

    def test_mixed_sister_oversub(self):
        """Seastone 2 2x50G was refused on hardware without oversubscribe_mixed_sister_25_50_enable=1 (#172)."""
        _, (plat,) = run_check(SEASTONE2)
        report = cbm.Report()
        cbm.check_config_bcm(plat, "X", self.write_td3_config("oversubscribe_mode=1\n"), report)
        self.assertTrue(any("oversubscribe_mixed_sister_25_50_enable=1 is missing" in e for e in report.errors),
                        report.errors)
        report = cbm.Report()
        cbm.check_config_bcm(plat, "X", self.write_td3_config("oversubscribe_mode=1\n"), report, dpb=False)
        self.assertEqual(report.errors, [])
        self.assertTrue(any("oversubscribe_mixed_sister_25_50_enable=1 is missing" in w for w in report.warnings),
                        report.warnings)
        report = cbm.Report()
        cbm.check_config_bcm(plat, "X", self.write_td3_config(
            "oversubscribe_mode=1\noversubscribe_mixed_sister_25_50_enable=1\n"), report)
        self.assertEqual(report.errors, [])
        for name in ("td3-seastone_2-32x100G.config.bcm", "td3-seastone_2-32x100G-vxlan.config.bcm"):
            props, _ = cbm.parse_config_bcm(os.path.join(SEASTONE2, "Seastone_2", name))
            self.assertEqual(props.get("oversubscribe_mixed_sister_25_50_enable"), "1", name)

    def test_s5232f_port32_has_no_4way_mode(self):
        """Dell maps the second SFP+ to logical 130, the id a 4-way breakout of port 32 needs.

        On hardware (#102, #172) the SAI creates the first three 4x25G children
        of Ethernet124 and refuses the fourth ("Dynamic port config failed 130"),
        so platform.json offers only 1x100G and 2x50G on that port.
        """
        report, (plat,) = run_check(S5232F)
        self.assertEqual(report.errors, [])
        self.assertEqual(sorted(plat.interfaces["Ethernet124"]["modes"]), ["1x100G[50G,40G]", "2x50G"])
        self.assertIn("4x25G[10G]", plat.interfaces["Ethernet120"]["modes"])

        # Offering the 4-way mode again is an error against Dell's config.bcm.
        with open(os.path.join(S5232F, "platform.json")) as f:
            data = json.load(f)
        data["interfaces"]["Ethernet124"]["breakout_modes"]["4x25G[10G]"] = ["etp32a", "etp32b", "etp32c", "etp32d"]
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with open(os.path.join(tmp, "platform.json"), "w") as f:
            json.dump(data, f)
        report = cbm.Report()
        plat4 = cbm.Platform(tmp, report)
        cbm.check_config_bcm(plat4, "DellEMC-S5232f-C32", os.path.join(
            S5232F, "DellEMC-S5232f-C32", "td3-s5232f-32x100G.config.bcm"), report)
        self.assertTrue(any("Ethernet124 mode 4x25G[10G] needs logical port 130 for its child on lane 128" in e
                            for e in report.errors), report.errors)

if __name__ == "__main__":
    unittest.main()
