import os
import tempfile
import unittest
from pathlib import Path

from swarm import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SWARM_HOME")
        os.environ["SWARM_HOME"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        if self._old_home is None:
            os.environ.pop("SWARM_HOME", None)
        else:
            os.environ["SWARM_HOME"] = self._old_home

    def _write(self, text):
        (Path(self.tmp.name) / "config.toml").write_text(text)

    def test_defaults(self):
        cfg = config.load()
        self.assertEqual(cfg.claude_bin, "claude")
        self.assertEqual(cfg.claude_model, "opus")
        self.assertEqual(cfg.omp_runner,
                         str(Path("~/.claude/skills/delegate/scripts/omp-run.py").expanduser()))
        self.assertEqual(cfg.daemon_interval_s, 20)
        self.assertEqual(cfg.max_active_plans, 3)
        self.assertEqual(cfg.max_claude_jobs, 2)
        self.assertEqual(cfg.worktree_reserve_gb, 15)
        self.assertEqual(cfg.max_goal_wait_h, 12)
        self.assertEqual(cfg.timeout_architect_s, 2700)
        self.assertEqual(cfg.timeout_review_s, 1200)
        self.assertEqual(cfg.timeout_verify_s, 1800)
        self.assertEqual(cfg.timeout_preverify_s, 2700)
        self.assertEqual(cfg.omp_max_cost, 3.0)
        self.assertEqual(cfg.omp_max_time, "60m")
        self.assertEqual(cfg.omp_fix_max_cost, 1.0)
        self.assertEqual(cfg.omp_fix_max_time, "30m")
        self.assertEqual(cfg.worker_env, {})
        self.assertEqual(cfg.arch_review_every, 8)
        self.assertEqual(cfg.pm_context_warn_tokens, 150000)
        self.assertEqual(cfg.pm_context_warn_step, 20000)
        self.assertEqual(cfg.pm_context_alert_tokens, 200000)
        self.assertEqual(cfg.impl_timeout_s(), 4200)
        self.assertEqual(cfg.fix_timeout_s(), 2400)

    def test_load_without_file_defaults_to_home_disk(self):
        cfg = config.load()
        self.assertEqual(cfg.disk_path, str(Path.home()))
        self.assertEqual(cfg.disk_name(), "home")

    def test_disk_name(self):
        self.assertEqual(config.Config(disk_path="/mnt/c").disk_name(), "C:")
        self.assertEqual(config.Config(disk_path="/mnt/d/").disk_name(), "D:")
        self.assertEqual(
            config.Config(disk_path=str(Path.home())).disk_name(), "home"
        )
        self.assertEqual(config.Config(disk_path="/home/x").disk_name(), "/home/x")
        self.assertEqual(
            config.Config(disk_path="/mnt/c", disk_label="D:").disk_name(), "D:"
        )

    def test_disk_label_from_file(self):
        self._write('disk_label = "E-lab"\n')
        self.assertEqual(config.load().disk_label, "E-lab")
        self.assertEqual(config.load().disk_name(), "E-lab")

    def test_overrides(self):
        self._write(
            'claude_bin = "/tmp/fake-claude"\n'
            'claude_model = "sonnet"\n'
            'omp_runner = "~/fake-omp.py"\n'
            'daemon_interval_s = 5\n'
            'max_active_plans = 7\n'
            'max_claude_jobs = 4\n'
            'worktree_reserve_gb = 25\n'
            'max_goal_wait_h = 3\n'
            'timeout_architect_s = 100\n'
            'timeout_review_s = 200\n'
            'timeout_verify_s = 300\n'
            'timeout_preverify_s = 400\n'
            'omp_max_cost = 9.5\n'
            'omp_max_time = "2h"\n'
            'omp_fix_max_cost = 0.5\n'
            'omp_fix_max_time = "90s"\n'
            'unknown_key = 1\n'
            '[worker_env]\n'
            'CARGO_BUILD_JOBS = "4"\n'
            'RUSTFLAGS = "-Awarnings"\n'
        )
        cfg = config.load()
        self.assertEqual(cfg.claude_bin, "/tmp/fake-claude")
        self.assertEqual(cfg.claude_model, "sonnet")
        self.assertEqual(cfg.omp_runner, str(Path("~/fake-omp.py").expanduser()))
        self.assertEqual(cfg.daemon_interval_s, 5)
        self.assertEqual(cfg.max_active_plans, 7)
        self.assertEqual(cfg.max_claude_jobs, 4)
        self.assertEqual(cfg.worktree_reserve_gb, 25)
        self.assertEqual(cfg.max_goal_wait_h, 3)
        self.assertEqual(cfg.timeout_architect_s, 100)
        self.assertEqual(cfg.timeout_review_s, 200)
        self.assertEqual(cfg.timeout_verify_s, 300)
        self.assertEqual(cfg.timeout_preverify_s, 400)
        self.assertEqual(cfg.omp_max_cost, 9.5)
        self.assertEqual(cfg.omp_max_time, "2h")
        self.assertEqual(cfg.omp_fix_max_cost, 0.5)
        self.assertEqual(cfg.omp_fix_max_time, "90s")
        self.assertEqual(cfg.worker_env,
                         {"CARGO_BUILD_JOBS": "4", "RUSTFLAGS": "-Awarnings"})
        self.assertEqual(cfg.impl_timeout_s(), 7200 + 600)
        self.assertEqual(cfg.fix_timeout_s(), 90 + 600)
        self.assertFalse(hasattr(cfg, "unknown_key"))

    def test_pm_context_alert_tokens_from_file(self):
        self._write("pm_context_alert_tokens = 250000\n")
        cfg = config.load()
        self.assertEqual(cfg.pm_context_alert_tokens, 250000)
        self.assertEqual(cfg.pm_context_warn_tokens, 150000)

    def test_models_defaults(self):
        cfg = config.load()
        self.assertEqual(cfg.models, config.DEFAULT_MODELS)
        self.assertEqual(cfg.model_for("architect"), "opus")
        self.assertEqual(cfg.model_for("verifier"), "opus")
        self.assertEqual(cfg.model_for("nope"), "opus")
        self.assertEqual(cfg.claude_projects_dir,
                         str(Path("~/.claude/projects").expanduser()))

    def test_models_table_merges_over_defaults(self):
        self._write('[models]\nverifier = "sonnet"\n')
        cfg = config.load()
        self.assertEqual(cfg.model_for("verifier"), "sonnet")
        self.assertEqual(cfg.model_for("rereview"), "sonnet")

    def test_models_table_absent_keeps_defaults_and_claude_model_fallback(self):
        self._write('claude_model = "haiku"\n')
        cfg = config.load()
        self.assertEqual(cfg.model_for("unknown"), "haiku")
        self.assertEqual(cfg.model_for("architect"), "opus")

    def test_parse_duration(self):
        self.assertEqual(config.parse_duration("90s"), 90)
        self.assertEqual(config.parse_duration("60m"), 3600)
        self.assertEqual(config.parse_duration("2h"), 7200)
        self.assertEqual(config.parse_duration("0s"), 0)
        for bad in ("", "60", "1d", "m", "-5m", "1.5h", "5 m", "5M"):
            with self.assertRaises(ValueError, msg=bad):
                config.parse_duration(bad)


if __name__ == "__main__":
    unittest.main()
