import os
import re
import tempfile
import unittest
from pathlib import Path

from swarm import runner, util

EXAMPLE = """\
safety_stop = "verified"
stop_pattern = "SAFETY STOP"

[steps.build]
cmd = "make release"
timeout = "30m"
locks = ["release-build"]

[steps.boot_test]
cmd = "./scripts/boot-test.sh"
timeout = "10m"

[windows.soak]
before = ["build", "boot_test"]
start = "./scripts/run.sh --mode canary"
length = "2h"
stop = "./scripts/stop.sh"
read = "./scripts/read-gate.sh soak"
production = false
"""


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_home = os.environ.get("SWARM_HOME")
        os.environ["SWARM_HOME"] = self.tmp.name
        self.addCleanup(self._restore_env)
        self.home = Path(self.tmp.name)

    def _restore_env(self):
        if self._old_home is None:
            os.environ.pop("SWARM_HOME", None)
        else:
            os.environ["SWARM_HOME"] = self._old_home

    def write(self, text, name="proj"):
        path = runner.config_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def load_error(self, text):
        path = self.write(text)
        with self.assertRaises(ValueError) as ctx:
            runner.load("proj")
        self.assertTrue(str(ctx.exception).startswith(f"{path}: "), str(ctx.exception))
        return str(ctx.exception)

    def test_config_path(self):
        path = runner.config_path("proj")
        self.assertEqual(path, self.home / "projects" / "proj.toml")

    def test_missing_file_returns_none(self):
        self.assertIsNone(runner.load("proj"))
        self.assertFalse(runner.config_path("proj").exists())

    def test_full_example(self):
        self.write(EXAMPLE)
        data = runner.load("proj")
        self.assertIs(data["safety_stop"], True)
        self.assertEqual(data["stop_pattern"].pattern, "SAFETY STOP")
        self.assertTrue(data["stop_pattern"].search("log SAFETY STOP here"))
        self.assertEqual(
            data["steps"],
            {
                "build": {
                    "cmd": "make release",
                    "timeout_s": 1800,
                    "locks": ["release-build"],
                },
                "boot_test": {
                    "cmd": "./scripts/boot-test.sh",
                    "timeout_s": 600,
                    "locks": [],
                },
            },
        )
        self.assertEqual(
            data["windows"],
            {
                "soak": {
                    "before": ["build", "boot_test"],
                    "start": "./scripts/run.sh --mode canary",
                    "length_s": 7200,
                    "stop": "./scripts/stop.sh",
                    "read": "./scripts/read-gate.sh soak",
                    "production": False,
                }
            },
        )
        self.assertEqual(sorted(data), ["safety_stop", "steps", "stop_pattern",
                                        "windows"])

    def test_defaults(self):
        self.write(
            '[steps.build]\ncmd = "make"\n'
            '[windows.soak]\nstart = "./run"\nlength = "1m"\nread = "./read"\n'
        )
        data = runner.load("proj")
        self.assertIs(data["safety_stop"], False)
        self.assertIsNone(data["stop_pattern"])
        self.assertEqual(
            data["steps"]["build"],
            {"cmd": "make", "timeout_s": 1800, "locks": []},
        )
        self.assertEqual(
            data["windows"]["soak"],
            {"before": [], "start": "./run", "length_s": 60, "stop": None,
             "read": "./read", "production": False},
        )

    def test_empty_file(self):
        self.write("")
        data = runner.load("proj")
        self.assertIs(data["safety_stop"], False)
        self.assertEqual(data["steps"], {})
        self.assertEqual(data["windows"], {})

    def test_safety_stop_only_for_verified(self):
        for value in ("verified", "Verified", "yes", "true", "verified "):
            self.write(f'safety_stop = "{value}"\n')
            self.assertEqual(runner.load("proj")["safety_stop"],
                             value == "verified", value)
        self.write("safety_stop = true\n")
        self.assertIs(runner.load("proj")["safety_stop"], False)

    def test_bad_toml(self):
        self.load_error('[steps.build\ncmd = "make"\n')

    def test_step_without_cmd(self):
        self.assertIn("build", self.load_error('[steps.build]\ntimeout = "1m"\n'))
        self.assertIn("build", self.load_error('[steps.build]\ncmd = 3\n'))
        self.assertIn("steps", self.load_error('steps = "nope"\n'))

    def test_window_without_start(self):
        self.assertIn("start", self.load_error(
            '[windows.w]\nlength = "1m"\nread = "./read"\n'
        ))

    def test_window_without_length(self):
        self.assertIn("length", self.load_error(
            '[windows.w]\nstart = "./run"\nread = "./read"\n'
        ))

    def test_window_without_read(self):
        self.assertIn("read", self.load_error(
            '[windows.w]\nstart = "./run"\nlength = "1m"\n'
        ))

    def test_window_value_not_a_table(self):
        self.assertIn("soak", self.load_error('windows = { soak = 3 }\n'))

    def test_before_names_no_step(self):
        self.assertIn("nope", self.load_error(
            '[steps.build]\ncmd = "make"\n'
            '[windows.w]\nbefore = ["nope"]\nstart = "./run"\n'
            'length = "1m"\nread = "./read"\n'
        ))

    def test_bad_duration(self):
        self.assertIn("timeout", self.load_error(
            '[steps.build]\ncmd = "make"\ntimeout = "soon"\n'
        ))
        self.assertIn("length", self.load_error(
            '[windows.w]\nstart = "./run"\nlength = "2 hours"\nread = "./read"\n'
        ))

    def test_bad_regex(self):
        self.assertIn("stop_pattern", self.load_error('stop_pattern = "("\n'))
        self.assertIn("stop_pattern", self.load_error("stop_pattern = 3\n"))

    def test_window_production_must_be_bool(self):
        msg = self.load_error(
            '[windows.soak]\nstart = "./run"\nlength = "1m"\nread = "./read"\n'
            'production = "false"\n'
        )
        self.assertIn("window soak production must be true or false", msg)
        self.assertIn(
            "window soak production must be true or false",
            self.load_error(
                '[windows.soak]\nstart = "./run"\nlength = "1m"\n'
                'read = "./read"\nproduction = 1\n'
            ),
        )

    def test_window_production_parses_true_and_false(self):
        for text, expected in (("true", True), ("false", False)):
            self.write(
                '[windows.soak]\nstart = "./run"\nlength = "1m"\n'
                f'read = "./read"\nproduction = {text}\n'
            )
            self.assertIs(runner.load("proj")["windows"]["soak"]["production"],
                          expected)

    def test_locks_not_list_of_strings(self):
        self.assertIn("locks", self.load_error(
            '[steps.build]\ncmd = "make"\nlocks = "release"\n'
        ))
        self.assertIn("locks", self.load_error(
            '[steps.build]\ncmd = "make"\nlocks = [1]\n'
        ))

    def test_gate_window_last_line(self):
        path = Path(self.tmp.name) / "plan.md"
        path.write_text(
            "# T\nGate window: first\n\nmore text\nGate window: soak\n",
            encoding="utf-8",
        )
        self.assertEqual(runner.gate_window(path), "soak")
        self.assertEqual(runner.gate_window(str(path)), "soak")

    def test_gate_window_ignores_non_matching_lines(self):
        path = Path(self.tmp.name) / "plan.md"
        path.write_text(
            "Gate window: soak\nGate window: soak extra\nGate window:\n"
            " gate window: lower\n",
            encoding="utf-8",
        )
        self.assertEqual(runner.gate_window(path), "soak")
        path.write_text("Gate window:  soak \n", encoding="utf-8")
        self.assertEqual(runner.gate_window(path), "soak")

    def test_gate_window_no_match(self):
        path = Path(self.tmp.name) / "plan.md"
        path.write_text("Gate window: a b\n", encoding="utf-8")
        self.assertIsNone(runner.gate_window(path))

    def test_gate_window_missing_file(self):
        self.assertIsNone(runner.gate_window(Path(self.tmp.name) / "absent.md"))
        self.assertIsNone(runner.gate_window(self.tmp.name))

    def test_stop_pattern_compiled(self):
        self.write(EXAMPLE)
        self.assertIsInstance(runner.load("proj")["stop_pattern"], re.Pattern)


if __name__ == "__main__":
    unittest.main()
