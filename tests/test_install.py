import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "link-skills.sh"
CLAUDE_SKILLS = sorted(
    p.name for p in (REPO / "skills").iterdir() if p.is_dir() and p.name != "omp"
)
OMP_SKILLS = sorted(p.name for p in (REPO / "skills" / "omp").iterdir() if p.is_dir())


class LinkSkillsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.swarm_home = base / "swarm-home"
        self.swarm_home.mkdir()
        self.stub_bin = base / "bin"
        self.stub_bin.mkdir()
        stub = self.stub_bin / "omp"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, path="/usr/bin:/bin"):
        env = dict(os.environ)
        env["SWARM_HOME"] = str(self.swarm_home)
        env["HOME"] = str(self.home)
        env["PATH"] = path
        return subprocess.run(
            ["bash", str(SCRIPT), str(REPO), str(self.home)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_fresh_link_links_claude_skills(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        claude_dir = self.home / ".claude" / "skills"
        for name in CLAUDE_SKILLS:
            target = claude_dir / name
            self.assertTrue(target.is_symlink(), target)
            self.assertEqual(target.resolve(), (REPO / "skills" / name).resolve())
            self.assertIn(f"linked {target}", result.stdout)
        self.assertNotIn("omp", [p.name for p in claude_dir.iterdir()])
        self.assertIn("omp not found: skipped omp skills", result.stdout)
        self.assertFalse((self.home / ".omp").exists())

    def test_second_run_changes_nothing(self):
        path = f"{self.stub_bin}:/usr/bin:/bin"
        first = self._run(path)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("linked", first.stdout)
        before = {
            p.name: p.readlink() for p in (self.home / ".claude" / "skills").iterdir()
        }
        second = self._run(path)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, "")
        after = {
            p.name: p.readlink() for p in (self.home / ".claude" / "skills").iterdir()
        }
        self.assertEqual(before, after)

    def test_existing_real_folder_is_kept(self):
        target = self.home / ".claude" / "skills" / "review-plan"
        target.mkdir(parents=True)
        marker = target / "keep.md"
        marker.write_text("mine\n")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"kept existing {target} (not linked; remove it to use the bundled copy)",
            result.stdout,
        )
        self.assertFalse(target.is_symlink())
        self.assertEqual(marker.read_text(), "mine\n")

    def test_omp_skills_skipped_without_omp(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("omp not found: skipped omp skills", result.stdout)
        self.assertFalse((self.home / ".omp").exists())

    def test_omp_skills_linked_with_stub(self):
        path = f"{self.stub_bin}:/usr/bin:/bin"
        result = self._run(path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("omp not found", result.stdout)
        omp_dir = self.home / ".omp" / "agent" / "skills"
        for name in OMP_SKILLS:
            target = omp_dir / name
            self.assertTrue(target.is_symlink(), target)
            self.assertEqual(
                target.resolve(), (REPO / "skills" / "omp" / name).resolve()
            )
            self.assertIn(f"linked {target}", result.stdout)


if __name__ == "__main__":
    unittest.main()
