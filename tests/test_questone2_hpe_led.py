"""Exercise only the isolated LED selector, never the hardware init entrypoint."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "platform/broadcom/sonic-platform-modules-cel/debian/platform-modules-questone2.init"
PLATFORM = ROOT / "device/celestica/x86_64-cel_questone_2-r0"
PREFIX = INIT.read_text().split('case "$1" in', 1)[0]
assert "select_hpe_led()" in PREFIX and "modprobe" not in PREFIX
TARGET = "./Questone_2/custom_led_hpe_r1n26a.bin"


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.platform = Path(self.temp.name)
        (self.platform / "Questone_2").mkdir()
        (self.platform / "Questone_2A").mkdir()
        (self.platform / TARGET).write_bytes(b"generated-image")
        self.sku = self.platform / "default_sku"
        self.sku.write_text("Questone_2 t1\n")
        self.led = self.platform / "custom_led.bin"
        self.json = self.platform / "platform.json"
        self.json.symlink_to("./Questone_2/platform.json")

    def run_selector(self, label="R1N26A"):
        before_sku = self.sku.read_bytes()
        before_json = self.json.readlink()
        subprocess.run(["bash", "-c", PREFIX + '\nselect_hpe_led "$1" "$2"',
                        "selector-test", str(self.platform), label], check=True)
        self.assertEqual(self.sku.read_bytes(), before_sku)
        self.assertEqual(self.json.readlink(), before_json)

    def test_stock_q2_and_stale_q2a_links_migrate(self):
        for old in ("./Questone_2/custom_led.bin", "Questone_2/custom_led.bin",
                    "./Questone_2A/custom_led.bin", "Questone_2A/custom_led.bin",
                    str(self.platform / "Questone_2/custom_led.bin"),
                    str(self.platform / "Questone_2A/custom_led.bin")):
            with self.subTest(old=old):
                self.led.symlink_to(old)
                self.run_selector()
                self.assertEqual(os.readlink(self.led), TARGET)
                self.led.unlink()

    def test_existing_hpe_link_is_not_replaced(self):
        self.led.symlink_to(TARGET)
        before = self.led.lstat()
        self.run_selector()
        self.assertEqual(self.led.lstat().st_ino, before.st_ino)

    def test_missing_link_is_created(self):
        self.run_selector()
        self.assertEqual(os.readlink(self.led), TARGET)

    def test_other_labels_are_unchanged(self):
        self.led.symlink_to("./Questone_2/custom_led.bin")
        for label in ("R1N25A", "Questone-II", "Questone-IIA", "ArcticaNX4808xxv", "", "R1N26A-other"):
            with self.subTest(label=label):
                self.run_selector(label)
                self.assertEqual(os.readlink(self.led), "./Questone_2/custom_led.bin")

    def test_other_sku_is_preserved(self):
        self.sku.write_text("Questone_2A t1\n")
        self.led.symlink_to("./Questone_2A/custom_led.bin")
        self.run_selector()
        self.assertEqual(os.readlink(self.led), "./Questone_2A/custom_led.bin")

    def test_custom_link_is_preserved(self):
        self.led.symlink_to("./operator_led.bin")
        self.run_selector()
        self.assertEqual(os.readlink(self.led), "./operator_led.bin")

    def test_regular_led_file_is_preserved(self):
        self.led.write_bytes(b"operator-data")
        self.run_selector()
        self.assertEqual(self.led.read_bytes(), b"operator-data")

    def test_absent_generated_binary_does_not_break_stock_link(self):
        (self.platform / TARGET).unlink()
        self.led.symlink_to("./Questone_2/custom_led.bin")
        self.run_selector()
        self.assertEqual(os.readlink(self.led), "./Questone_2/custom_led.bin")


if __name__ == "__main__":
    unittest.main()
