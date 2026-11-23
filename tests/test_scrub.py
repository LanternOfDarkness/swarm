import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scrub-check.sh"


class ScrubCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.patterns = self.root / "patterns"

        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("SWARM_WORKER", "SWARM_SCRUB_PATTERNS")
        }
        self.env["SWARM_HOME"] = str(self.root / "swarm")
        self.env["HOME"] = str(self.home)
        self.env["GIT_CONFIG_GLOBAL"] = os.devnull
        self.env["GIT_CONFIG_SYSTEM"] = os.devnull
        self.env["GIT_AUTHOR_NAME"] = "t"
        self.env["GIT_AUTHOR_EMAIL"] = "t@example.com"
        self.env["GIT_COMMITTER_NAME"] = "t"
        self.env["GIT_COMMITTER_EMAIL"] = "t@example.com"

        self.git("init", "-q")
        self.write_tracked("notes.txt", "nothing interesting here\n")
        self.commit()

    def git(self, *args, cwd=None):
        subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

    def write_tracked(self, name, text):
        (self.repo / name).write_text(text, encoding="utf-8")

    def commit(self):
        self.git("add", "-A")
        self.git(
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        )

    def write_patterns(self, text):
        self.patterns.write_text(text, encoding="utf-8")

    def run_scrub(self, *args, env=None):
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=self.repo,
            env=env or self.env,
            capture_output=True,
            text=True,
        )

    def test_clean(self):
        self.write_patterns("demo\n")
        result = self.run_scrub(str(self.patterns))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "scrub: clean")

    def test_planted_hit(self):
        self.write_tracked("notes.txt", "nothing\nplantedword lives here\n")
        self.commit()
        self.write_patterns("plantedword\n")
        result = self.run_scrub(str(self.patterns))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("notes.txt:2:", result.stdout)
        self.assertIn("plantedword", result.stdout)

    def test_missing_patterns_file(self):
        missing = self.root / "absent"
        env = dict(self.env, SWARM_SCRUB_PATTERNS=str(missing))
        result = self.run_scrub(env=env)
        self.assertEqual(result.returncode, 2, result.stdout)
        combined = result.stdout + result.stderr
        self.assertIn("scrub: no patterns file at", combined)
        self.assertIn(str(missing), combined)

    def test_empty_patterns_file(self):
        self.write_patterns("\n# only a comment\n")
        result = self.run_scrub(str(self.patterns))
        self.assertEqual(result.returncode, 2, result.stdout)
        combined = result.stdout + result.stderr
        self.assertIn("scrub: no patterns file at", combined)

    def test_comment_line_ignored(self):
        self.write_tracked("notes.txt", "plantedword\n")
        self.commit()
        self.write_patterns("# plantedword\nabsent\n")
        result = self.run_scrub(str(self.patterns))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "scrub: clean")

    def test_untracked_file_ignored(self):
        (self.repo / "draft.txt").write_text("plantedword\n", encoding="utf-8")
        self.write_patterns("plantedword\n")
        result = self.run_scrub(str(self.patterns))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "scrub: clean")

    def test_pattern_with_space_and_alternation(self):
        self.write_tracked("notes.txt", "a two words phrase\n")
        self.commit()
        self.write_patterns("two words|absent thing\n")
        result = self.run_scrub(str(self.patterns))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("notes.txt:1:", result.stdout)
        self.assertIn("two words", result.stdout)

    def test_env_patterns_file(self):
        self.write_tracked("notes.txt", "plantedword\n")
        self.commit()
        self.write_patterns("plantedword\n")
        env = dict(self.env, SWARM_SCRUB_PATTERNS=str(self.patterns))
        result = self.run_scrub(env=env)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("notes.txt:1:", result.stdout)


if __name__ == "__main__":
    unittest.main()
