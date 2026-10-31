"""Exercise the storage guard with synthetic filesystem data, without builds."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


GUARD = Path(__file__).resolve().parents[1] / "check-storage.sh"
KIB_PER_GIB = 1024 * 1024


class StorageGuardTest(unittest.TestCase):
    def check_guard(self, mode="persistent", budget="500", used=100, free=400,
                    work_device="1", docker_device="1", missing=False,
                    malformed_df=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "workspace.conf"
            if not missing:
                config.write_text(
                    f"SONIC_WORKSPACE_MODE={mode}\n"
                    f"SONIC_WORKSPACE_BUDGET_GIB={budget}\n"
                )
            stat = root / "stat"
            stat.write_text(
                '#!/bin/bash\ncase "$3" in\n'
                f'/work) echo {work_device};;\n'
                f'/var/lib/docker) echo {docker_device};;\n'
                '*) echo 1;;\nesac\n'
            )
            df = root / "df"
            row = "invalid" if malformed_df else (
                f"/dev/test 9999999999 {used * KIB_PER_GIB} "
                f"{free * KIB_PER_GIB} 1% /work"
            )
            df.write_text(f"#!/bin/bash\nprintf '%s\\n' 'Filesystem Used Available' '{row}'\n")
            stat.chmod(0o755)
            df.chmod(0o755)
            return subprocess.run(
                ["bash", "-euc", 'source "$1"; check_image_storage', "test", str(GUARD)],
                env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                     "SONIC_WORKSPACE_CONFIG": str(config)},
                capture_output=True, text=True,
            )

    def test_persistent_workspace_with_capacity(self):
        self.assertEqual(self.check_guard().returncode, 0)

    def test_exact_threshold(self):
        self.assertEqual(self.check_guard(used=350, free=150).returncode, 0)

    def test_temporary_modes_reject_large_node_filesystem(self):
        for mode in ("preflight", "ephemeral", "unknown", ""):
            with self.subTest(mode=mode):
                self.assertNotEqual(self.check_guard(mode=mode, free=10000).returncode, 0)

    def test_budget_limits_large_backing_filesystem(self):
        self.assertNotEqual(self.check_guard(budget="150", used=1, free=10000).returncode, 0)

    def test_filesystem_space_limits_large_budget(self):
        self.assertNotEqual(self.check_guard(free=149).returncode, 0)

    def test_exhausted_budget(self):
        self.assertNotEqual(self.check_guard(used=501).returncode, 0)

    def test_invalid_budget(self):
        for budget in ("", "0", "-1", "abc", "0500", "999999999999999999999"):
            with self.subTest(budget=budget):
                self.assertNotEqual(self.check_guard(budget=budget).returncode, 0)

    def test_wrong_filesystem(self):
        self.assertNotEqual(self.check_guard(work_device="2").returncode, 0)
        self.assertNotEqual(self.check_guard(docker_device="2").returncode, 0)

    def test_missing_metadata(self):
        self.assertNotEqual(self.check_guard(missing=True).returncode, 0)

    def test_unreadable_usage(self):
        self.assertNotEqual(self.check_guard(malformed_df=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
