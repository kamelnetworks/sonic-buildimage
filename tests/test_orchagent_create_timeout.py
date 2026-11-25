"""Hardware-free unit tests for createSwitchTimeout handling in orchagent.sh.

Runs the real dockers/docker-orchagent/orchagent.sh with stubbed CLI helpers
and a recording orchagent binary so platform branches, precedence, stock
opt-out, missing sai.profile (sai.profile.j2-only HWSKUs), and malformed
values are exercised end-to-end without a container build.
"""

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ORCHAGENT_SH = ROOT / "dockers" / "docker-orchagent" / "orchagent.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def run_orchagent_sh(
    *,
    asic_type: str = "broadcom",
    cmdline: str = "root=/dev/sda2 rw quiet\n",
    platform_env: str | None = None,
    sai_profile: str | None = None,
) -> tuple[list[str], str]:
    """Execute orchagent.sh with stubbed helpers and return (orchagent_argv, stderr)."""
    with tempfile.TemporaryDirectory(prefix="orchagent-sh-test-") as tmp:
        root = Path(tmp)
        stub_bin = root / "bin"
        stub_bin.mkdir()
        hwsku_dir = root / "hwsku"
        hwsku_dir.mkdir()
        platform_dir = root / "platform"
        platform_dir.mkdir()
        log_dir = root / "swss-log"
        argv_out = root / "orchagent-argv.json"
        proc_cmdline = root / "cmdline"
        proc_cmdline.write_text(cmdline, encoding="utf-8")
        fips_file = root / "fips_enable"
        platform_env_conf = platform_dir / "platform_env.conf"

        if platform_env is not None:
            platform_env_conf.write_text(platform_env, encoding="utf-8")
        if sai_profile is not None:
            (hwsku_dir / "sai.profile").write_text(sai_profile, encoding="utf-8")

        swss_vars = json.dumps({
            "asic_type": asic_type,
            "asic_subtype": "",
            "mac": "00:11:22:33:44:55",
            "synchronous_mode": "",
            "swss_zmq": "",
            "asic_id": "",
        })
        _write_executable(
            stub_bin / "sonic-cfggen",
            f"#!/usr/bin/env bash\nprintf '%s\\n' '{swss_vars}'\n",
        )
        _write_executable(
            stub_bin / "sonic-db-cli",
            "#!/usr/bin/env bash\nexit 0\n",
        )
        _write_executable(
            stub_bin / "logger",
            "#!/usr/bin/env bash\nexit 0\n",
        )
        fake_orchagent = stub_bin / "fake-orchagent"
        _write_executable(
            fake_orchagent,
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['ARGV_OUT'], 'w', encoding='utf-8') as f:\n"
            "    json.dump(sys.argv[1:], f)\n",
        )

        env = {
            "PATH": f"{stub_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HWSKU_DIR": str(hwsku_dir),
            "PLATFORM_ENV_CONF": str(platform_env_conf),
            "SWSS_VARS_FILE": str(root / "swss_vars.j2"),
            "PROC_CMDLINE": str(proc_cmdline),
            "FIPS_ENABLE_FILE": str(fips_file),
            "SWSS_LOG_DIR": str(log_dir),
            "ORCHAGENT_BIN": str(fake_orchagent),
            "ARGV_OUT": str(argv_out),
        }
        proc = subprocess.run(
            ["bash", str(ORCHAGENT_SH)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(argv_out.read_text(encoding="utf-8")), proc.stderr


class OrchagentCreateTimeoutTest(unittest.TestCase):
    def test_script_syntax(self):
        subprocess.run(["bash", "-n", str(ORCHAGENT_SH)], check=True)

    def test_broadcom_unset_leaves_args_unchanged(self):
        # Missing sai.profile (sai.profile.j2-only HWSKUs) must not error or pass -t.
        argv, stderr = run_orchagent_sh(asic_type="broadcom")
        self.assertEqual(stderr, "")
        self.assertNotIn("-t", argv)
        self.assertIn("-m", argv)

    def test_broadcom_cmdline_sets_timeout(self):
        argv, stderr = run_orchagent_sh(
            asic_type="broadcom",
            cmdline="root=/dev/sda2 sonic_orchagent_create_timeout=300 quiet\n",
        )
        self.assertEqual(stderr, "")
        idx = argv.index("-t")
        self.assertEqual(argv[idx + 1], "300")

    def test_broadcom_platform_env_sets_timeout(self):
        argv, stderr = run_orchagent_sh(
            asic_type="broadcom",
            platform_env="# comment\ncreateSwitchTimeout=180\n",
        )
        self.assertEqual(stderr, "")
        idx = argv.index("-t")
        self.assertEqual(argv[idx + 1], "180")

    def test_cmdline_stock_overrides_platform_and_sai_profile(self):
        argv, stderr = run_orchagent_sh(
            asic_type="broadcom",
            cmdline="root=/dev/sda2 sonic_orchagent_create_timeout=stock\n",
            platform_env="createSwitchTimeout=180\n",
            sai_profile="createSwitchTimeout=150\n",
        )
        self.assertEqual(stderr, "")
        self.assertNotIn("-t", argv)

    def test_cmdline_overrides_platform_env_and_sai_profile(self):
        argv, stderr = run_orchagent_sh(
            asic_type="broadcom",
            cmdline="sonic_orchagent_create_timeout=240\n",
            platform_env="createSwitchTimeout=180\n",
            sai_profile="createSwitchTimeout=150\n",
        )
        self.assertEqual(stderr, "")
        idx = argv.index("-t")
        self.assertEqual(argv[idx + 1], "240")

    def test_platform_env_overrides_sai_profile(self):
        argv, stderr = run_orchagent_sh(
            asic_type="broadcom",
            platform_env="createSwitchTimeout=200\n",
            sai_profile="createSwitchTimeout=150\n",
        )
        self.assertEqual(stderr, "")
        idx = argv.index("-t")
        self.assertEqual(argv[idx + 1], "200")

    def test_sai_profile_honoured_on_broadcom_and_marvell_prestera(self):
        for asic in ("broadcom", "marvell-prestera", "marvell-teralynx", "vs"):
            with self.subTest(asic=asic):
                argv, stderr = run_orchagent_sh(
                    asic_type=asic,
                    sai_profile="SAI_INIT_CONFIG_FILE=/etc/sai.d/config.bcm\ncreateSwitchTimeout=150\n",
                )
                self.assertEqual(stderr, "")
                idx = argv.index("-t")
                self.assertEqual(argv[idx + 1], "150")

    def test_comments_and_malformed_values_ignored(self):
        for bad in (
            "# createSwitchTimeout=180\n",
            "createSwitchTimeout=0\n",
            "createSwitchTimeout=-10\n",
            "createSwitchTimeout=abc\n",
            "createSwitchTimeout=180;id\n",
        ):
            with self.subTest(bad=bad):
                argv, stderr = run_orchagent_sh(
                    asic_type="broadcom",
                    sai_profile=bad,
                )
                self.assertEqual(stderr, "")
                self.assertNotIn("-t", argv)

        argv, stderr = run_orchagent_sh(
            asic_type="broadcom",
            cmdline="sonic_orchagent_create_timeout=0\n",
        )
        self.assertEqual(stderr, "")
        self.assertNotIn("-t", argv)


if __name__ == "__main__":
    unittest.main()
