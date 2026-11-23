import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from swarm import board, connect, db, gitops


class ConnectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.git_env = {
            key: value for key, value in os.environ.items() if key != "SWARM_WORKER"
        }
        self.git_env.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.com",
            }
        )
        self._old_env = {}
        for key, value in {"SWARM_HOME": str(self.home), **self.git_env}.items():
            self._old_env[key] = os.environ.get(key)
            os.environ[key] = value
        os.environ.pop("SWARM_WORKER", None)
        self.addCleanup(self._restore_env)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def _restore_env(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # -- fixtures ----------------------------------------------------------

    def run_git(self, repo, *args):
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            env=self.git_env,
        )
        if result.returncode != 0:
            self.fail(f"git {args} failed: {result.stderr}")
        return result.stdout

    def init_repo(self, name="repo", files=None, commit=True):
        repo = self.home / "repos" / name
        repo.mkdir(parents=True)
        self.run_git(repo, "init", "-q", "-b", "main")
        for relative, content in (files or {}).items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        if commit:
            self.run_git(repo, "add", "-A")
            self.run_git(repo, "commit", "-q", "--allow-empty", "-m", "init")
        return repo

    def project(self, name):
        return self.conn.execute(
            "SELECT * FROM projects WHERE name = ?", (name,)
        ).fetchone()

    # -- detect ------------------------------------------------------------

    def test_detect_cargo(self):
        repo = self.init_repo(files={"Cargo.toml": "[package]\n"})
        found = connect.detect(repo)
        self.assertEqual(found["repo"], str(repo.resolve()))
        self.assertEqual(found["branch"], "main")
        self.assertEqual(
            found["verify_cmd"],
            "cargo check --workspace --all-targets && cargo test --workspace",
        )
        self.assertIn("Bash(cargo check:*)", found["verify_allow"])

    def test_detect_go(self):
        repo = self.init_repo(files={"go.mod": "module x\n"})
        found = connect.detect(repo)
        self.assertEqual(found["verify_cmd"], "go vet ./... && go test ./...")
        self.assertIn("Bash(go test:*)", found["verify_allow"])

    def test_detect_package_json_with_test_script(self):
        repo = self.init_repo(
            files={"package.json": '{"scripts": {"test": "jest"}}'}
        )
        found = connect.detect(repo)
        self.assertEqual(found["verify_cmd"], "npm test")
        self.assertIn("Bash(npm test:*)", found["verify_allow"])

    def test_detect_package_json_without_test_script(self):
        repo = self.init_repo(files={"package.json": '{"scripts": {}}'})
        found = connect.detect(repo)
        self.assertIsNone(found["verify_cmd"])
        self.assertIsNone(found["verify_allow"])

    def test_detect_pyproject_pytest(self):
        repo = self.init_repo(
            files={"pyproject.toml": "[tool.pytest.ini_options]\n"}
        )
        found = connect.detect(repo)
        self.assertEqual(found["verify_cmd"], "python3 -m pytest -q")
        self.assertIn("Bash(pytest:*)", found["verify_allow"])

    def test_detect_tests_dir(self):
        repo = self.init_repo(files={"tests/test_x.py": "pass\n"})
        found = connect.detect(repo)
        self.assertEqual(
            found["verify_cmd"], "python3 -m unittest discover -s tests -v"
        )

    def test_detect_empty_repo(self):
        repo = self.init_repo()
        found = connect.detect(repo)
        self.assertIsNone(found["verify_cmd"])
        self.assertIsNone(found["verify_allow"])
        self.assertEqual(found["plans_dir"], "docs/plans")
        self.assertFalse(found["plans_dir_exists"])
        self.assertIsNone(found["gates_file"])

    def test_detect_plans_dir_only(self):
        repo = self.init_repo(files={"plans/0042-a.md": "# 0042 — A\n"})
        found = connect.detect(repo)
        self.assertEqual(found["plans_dir"], "plans")
        self.assertTrue(found["plans_dir_exists"])
        self.assertEqual([plan["ref"] for plan in found["plans"]], ["0042"])

    def test_detect_gates_file(self):
        repo = self.init_repo(
            files={"docs/plans/GATES.md": "gates\n"}
        )
        found = connect.detect(repo)
        self.assertEqual(
            found["gates_file"], "docs/plans/GATES.md"
        )

    def test_detect_detached_head(self):
        repo = self.init_repo()
        self.run_git(repo, "checkout", "-q", "--detach", "HEAD")
        with self.assertRaises(ValueError) as ctx:
            connect.detect(repo)
        self.assertIn("detached HEAD: pass --branch", str(ctx.exception))
        found = connect.detect(repo, branch="main")
        self.assertEqual(found["branch"], "main")

    def test_detect_non_repo(self):
        plain = self.root / "plain"
        plain.mkdir()
        with self.assertRaises(ValueError) as ctx:
            connect.detect(plain)
        self.assertEqual(str(ctx.exception), f"not a git repo: {plain}")

    def test_detect_name_normalised(self):
        repo = self.init_repo(name="My Cool Repo")
        self.assertEqual(connect.detect(repo)["name"], "my-cool-repo")

    def test_detect_plan_titles_and_states(self):
        repo = self.init_repo(
            files={
                "docs/plans/0042-alpha.md": (
                    "# 0042 — Alpha\n\n"
                    "<!-- swarm:status -->\n"
                    "**Swarm status:** MERGED · branch x · updated 2026-01-01T00:00:00Z\n"
                    "<!-- /swarm:status -->\n"
                ),
                "docs/plans/0043-plain-heading.md": "# 0043: Beta\n",
                "docs/plans/README.md": "# Readme\n",
                "docs/plans/notes-1.md": "# Notes\n",
            }
        )
        found = connect.detect(repo)
        plans = {plan["ref"]: plan for plan in found["plans"]}
        self.assertEqual(sorted(plans), ["0042", "0043"])
        self.assertEqual(plans["0042"]["title"], "Alpha")
        self.assertEqual(plans["0042"]["state"], "MERGED")
        self.assertEqual(plans["0042"]["path"], "docs/plans/0042-alpha.md")
        self.assertEqual(plans["0043"]["title"], "Beta")
        self.assertIsNone(plans["0043"]["state"])

    # -- apply -------------------------------------------------------------

    def test_apply_new_project_installs_guard_and_auto_off(self):
        repo = self.init_repo(files={"Cargo.toml": "[package]\n"})
        found = connect.detect(repo)
        lines = connect.apply(self.conn, found)
        row = self.project(found["name"])
        self.assertIsNotNone(row)
        self.assertEqual(row["archived"], 0)
        self.assertEqual(row["auto"], 0)
        self.assertEqual(row["implementer"], "omp")
        self.assertIn("Bash(cargo test:*)", row["verify_allow"])
        self.assertTrue(gitops.guard_installed(row))
        self.assertEqual(
            lines[-1], f"next: swarm project set {found['name']} --auto on"
        )

    def test_apply_twice_reports_already_connected(self):
        repo = self.init_repo()
        found = connect.detect(repo)
        connect.apply(self.conn, found)
        self.assertEqual(
            connect.apply(self.conn, found),
            [f"already connected as {found['name']}"],
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1
        )

    def test_apply_foreign_hook_reports_not_installed(self):
        repo = self.init_repo()
        hook = repo / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
        found = connect.detect(repo)
        lines = connect.apply(self.conn, found)
        self.assertTrue(any("push guard NOT installed" in line for line in lines))
        self.assertIsNotNone(self.project(found["name"]))
        self.assertIn("exit 0", hook.read_text())
        self.assertFalse(gitops.guard_installed(self.project(found["name"])))

    def test_apply_imports_plans_with_done(self):
        repo = self.init_repo(
            files={
                "docs/plans/0042-a.md": "# 0042 — A\n",
                "docs/plans/0043-b.md": "# 0043 — B\n",
                "docs/plans/0003-c.md": "# 0003 — C\n",
            }
        )
        found = connect.detect(repo)
        connect.apply(
            self.conn, found, import_plans=True, done={"0042", "0043"}
        )
        rows = self.conn.execute(
            "SELECT ref, state FROM plans WHERE project = ? ORDER BY ref",
            (found["name"],),
        ).fetchall()
        self.assertEqual(
            [(row["ref"], row["state"]) for row in rows],
            [("0003", "DRAFT"), ("0042", "DONE"), ("0043", "DONE")],
        )

    def test_apply_blocked_status_imports_as_draft(self):
        repo = self.init_repo(
            files={
                "docs/plans/0042-a.md": (
                    "# 0042 — A\n\n"
                    "<!-- swarm:status -->\n"
                    "**Swarm status:** BLOCKED · updated 2026-01-01T00:00:00Z\n"
                    "<!-- /swarm:status -->\n"
                )
            }
        )
        found = connect.detect(repo)
        lines = connect.apply(self.conn, found, import_plans=True)
        row = board.get_plan(self.conn, found["name"], "0042")
        self.assertEqual(row["state"], "DRAFT")
        self.assertIn(
            "0042: was BLOCKED in its file, imported as DRAFT", lines
        )

    def test_apply_archived_project_is_reconnected(self):
        repo = self.init_repo()
        found = connect.detect(repo)
        connect.apply(self.conn, found)
        board.archive_project(self.conn, found["name"])
        self.assertEqual(self.project(found["name"])["archived"], 1)

        lines = connect.apply(self.conn, found)
        row = self.project(found["name"])
        self.assertEqual(row["archived"], 0)
        self.assertEqual(row["auto"], 0)
        self.assertNotIn("already connected", " ".join(lines))
        events = [
            event["text"]
            for event in self.conn.execute("SELECT text FROM events").fetchall()
        ]
        self.assertIn(f"project {found['name']} reconnected", events)

    def test_apply_overrides_branch_and_verify(self):
        repo = self.init_repo()
        found = connect.detect(repo)
        connect.apply(
            self.conn,
            found,
            branch="trunk",
            verify_cmd="true",
            verify_allow="Bash(true:*)",
        )
        row = self.project(found["name"])
        self.assertEqual(row["integration_branch"], "trunk")
        self.assertEqual(row["verify_cmd"], "true")
        self.assertEqual(row["verify_allow"], "Bash(true:*)")

    # -- parse_refs --------------------------------------------------------

    def test_parse_refs(self):
        self.assertEqual(
            connect.parse_refs("0001-0003,0007"),
            frozenset({"0001", "0002", "0003", "0007"}),
        )
        self.assertEqual(connect.parse_refs("7"), frozenset({"7"}))
        self.assertEqual(connect.parse_refs(""), frozenset())
        self.assertEqual(connect.parse_refs("  "), frozenset())
        with self.assertRaises(ValueError):
            connect.parse_refs("x")
        with self.assertRaises(ValueError):
            connect.parse_refs("0003-0001")
        with self.assertRaises(ValueError):
            connect.parse_refs("0001,")
        with self.assertRaises(ValueError):
            connect.parse_refs("0000")


if __name__ == "__main__":
    unittest.main()
