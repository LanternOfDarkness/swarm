import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from swarm import board, db, gitops


class GitOpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
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
        for key, value in {
            "SWARM_HOME": str(self.root / "swarm"),
            **self.git_env,
        }.items():
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

    def run_git(self, repo, *args, env=None):
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            env=env if env is not None else self.git_env,
        )
        if result.returncode != 0:
            self.fail(f"git {args} failed: {result.stderr}")
        return result.stdout

    def git_rc(self, repo, *args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            env=self.git_env,
        )

    def sha(self, repo, ref="HEAD"):
        return self.run_git(repo, "rev-parse", ref).strip()

    def commit(self, repo, message, filename=None, content=None):
        name = filename or message.replace(" ", "-") + ".txt"
        return self.write_commit(repo, name, content or message, message)

    def write_commit(self, repo, relative, content, message):
        path = Path(repo) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self.run_git(repo, "add", relative)
        self.run_git(repo, "commit", "-q", "-m", message)
        return self.sha(repo)

    def init_repo(self, name="repo"):
        repo = self.root / name
        repo.mkdir()
        self.run_git(
            repo,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "init",
            "-q",
            "-b",
            "main",
        )
        self.commit(repo, "base")
        return repo

    def add_remote(self, repo, name="remote.git"):
        bare = self.root / name
        self.run_git(self.root, "init", "-q", "--bare", str(bare))
        self.run_git(repo, "remote", "add", "origin", str(bare))
        return bare

    def add_project(self, repo, name="demo"):
        board.add_project(self.conn, name, str(repo))
        return self.conn.execute(
            "SELECT * FROM projects WHERE name = ?", (name,)
        ).fetchone()

    def add_plan(self, project_name, ref, title="Plan work"):
        board.add_plan(self.conn, project_name, ref, title)
        return self.fetch_plan(ref)

    def fetch_plan(self, ref):
        return self.conn.execute(
            "SELECT * FROM plans WHERE ref = ?", (ref,)
        ).fetchone()

    def link(self, ref, path, branch):
        with self.conn:
            self.conn.execute(
                "UPDATE plans SET worktree = ?, branch = ? WHERE ref = ?",
                (path, branch, ref),
            )
        return self.fetch_plan(ref)

    def setup_plan(self, ref="1050", title="Plan work"):
        repo = self.init_repo()
        project = self.add_project(repo)
        plan = self.add_plan("demo", ref, title)
        path, branch, base = gitops.create_worktree(project, plan)
        plan = self.link(ref, path, branch)
        return repo, project, plan, path, branch, base

    # -- pure helpers ------------------------------------------------------

    def test_slug(self):
        self.assertEqual(gitops.slug("Build the Scheduler!"), "build-the-scheduler")
        self.assertEqual(gitops.slug("  A--B__C  "), "a-b-c")
        self.assertEqual(gitops.slug("x" * 60), "x" * 40)
        self.assertFalse(gitops.slug("y" * 39 + "!!!").endswith("-"))
        self.assertEqual(len(gitops.slug("Words and more words " * 10)), 40)

    def test_worktree_path(self):
        repo = self.init_repo()
        project = self.add_project(repo)
        expected = str(repo.resolve().parent / f"{repo.resolve().name}-1050")
        self.assertEqual(gitops.worktree_path(project, "1050"), expected)

    # -- worktrees ---------------------------------------------------------

    def test_create_worktree(self):
        repo = self.init_repo()
        project = self.add_project(repo)
        plan = self.add_plan("demo", "1050", "Plan work")
        base = self.sha(repo)

        path, branch, base_sha = gitops.create_worktree(project, plan)

        self.assertEqual(branch, "plan/1050-plan-work")
        self.assertEqual(base_sha, base)
        self.assertEqual(path, gitops.worktree_path(project, "1050"))
        self.assertTrue(Path(path).is_dir())
        head = self.run_git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
        self.assertEqual(head, branch)
        listing = self.run_git(repo, "worktree", "list", "--porcelain")
        self.assertIn(path, listing)

    def test_create_worktree_refuses_existing_path(self):
        repo, project, plan, path, branch, _ = self.setup_plan()

        with self.assertRaises(ValueError) as ctx:
            gitops.create_worktree(project, plan)
        self.assertIn("worktree path exists", str(ctx.exception))
        self.assertTrue(Path(path).is_dir())

    def test_create_worktree_refuses_existing_branch(self):
        repo = self.init_repo()
        project = self.add_project(repo)
        plan = self.add_plan("demo", "1051", "Other work")
        self.run_git(repo, "branch", "plan/1051-other-work")

        with self.assertRaises(ValueError) as ctx:
            gitops.create_worktree(project, plan)
        self.assertIn("branch exists", str(ctx.exception))
        self.assertFalse(Path(gitops.worktree_path(project, "1051")).exists())

    def test_update_branch_clean(self):
        repo, project, plan, path, branch, _ = self.setup_plan()
        self.write_commit(path, "plan.txt", "plan", "plan work")
        main_sha = self.commit(repo, "main work", "main.txt", "main")

        merged = gitops.update_branch(project, plan)

        self.assertEqual(merged, main_sha)
        self.assertTrue((Path(path) / "main.txt").exists())
        self.assertEqual(self.run_git(path, "status", "--porcelain"), "")

    def test_update_branch_conflict_aborts(self):
        repo, project, plan, path, branch, _ = self.setup_plan()
        self.write_commit(path, "shared.txt", "plan version", "plan edit")
        self.commit(repo, "main edit", "shared.txt", "main version")

        with self.assertRaises(ValueError) as ctx:
            gitops.update_branch(project, plan)
        message = str(ctx.exception)
        self.assertIn("merge conflict with main: shared.txt", message)

        self.assertEqual(self.run_git(path, "status", "--porcelain"), "")
        merge_head = self.git_rc(path, "rev-parse", "-q", "--verify", "MERGE_HEAD")
        self.assertNotEqual(merge_head.returncode, 0)
        self.assertEqual((Path(path) / "shared.txt").read_text(), "plan version")

    def test_merge_into_main(self):
        repo, project, plan, path, branch, _ = self.setup_plan()
        self.write_commit(path, "plan.txt", "plan", "plan work")
        expected = gitops.ib_head(project)

        merge_sha = gitops.merge_into_main(project, plan, expected)

        self.assertEqual(merge_sha, self.sha(repo))
        self.assertTrue((repo / "plan.txt").exists())
        parents = self.run_git(repo, "rev-list", "--parents", "-n", "1", merge_sha)
        self.assertEqual(len(parents.split()), 3)
        log = self.run_git(repo, "log", "-1", "--format=%s", merge_sha).strip()
        self.assertEqual(log, "Merge plan/1050-plan-work (plan 1050)")

    def test_merge_into_main_refuses_dirty_main(self):
        repo, project, plan, path, branch, _ = self.setup_plan()
        self.write_commit(path, "plan.txt", "plan", "plan work")
        expected = gitops.ib_head(project)
        (repo / "dirty.txt").write_text("dirty")

        with self.assertRaises(ValueError) as ctx:
            gitops.merge_into_main(project, plan, expected)
        self.assertEqual(
            str(ctx.exception), "main checkout has 1 uncommitted changes"
        )
        branch_head = self.run_git(repo, "rev-parse", "main").strip()
        self.assertEqual(branch_head, expected)

    def test_merge_into_main_refuses_wrong_branch(self):
        repo, project, plan, path, branch, _ = self.setup_plan()
        self.write_commit(path, "plan.txt", "plan", "plan work")
        expected = gitops.ib_head(project)
        self.run_git(repo, "checkout", "-q", "-b", "feature")

        with self.assertRaises(ValueError) as ctx:
            gitops.merge_into_main(project, plan, expected)
        self.assertEqual(str(ctx.exception), "main checkout is on feature, not main")

    def test_merge_into_main_refuses_moved_ib(self):
        repo, project, plan, path, branch, _ = self.setup_plan()
        self.write_commit(path, "plan.txt", "plan", "plan work")
        expected = gitops.ib_head(project)
        self.commit(repo, "moved", "moved.txt", "moved")

        with self.assertRaises(ValueError) as ctx:
            gitops.merge_into_main(project, plan, expected)
        self.assertEqual(str(ctx.exception), "main moved")
        self.assertFalse((repo / "plan.txt").exists())

    # -- cleanup -----------------------------------------------------------

    def test_cleanup_removes_worktree_target_and_branch(self):
        repo, project, plan, path, branch, _ = self.setup_plan()
        target = Path(path) / "target"
        target.mkdir()
        (target / "artifact").write_text("built")

        gitops.cleanup(project, plan)

        self.assertFalse(Path(path).exists())
        self.assertFalse(target.exists())
        branch_check = self.git_rc(repo, "rev-parse", "-q", "--verify", branch)
        self.assertNotEqual(branch_check.returncode, 0)
        listing = self.run_git(repo, "worktree", "list", "--porcelain")
        self.assertNotIn(path, listing)

    def test_cleanup_refuses_foreign_path(self):
        repo, project, plan, path, branch, _ = self.setup_plan()
        foreign = self.root / "foreign"
        foreign.mkdir()
        with self.conn:
            self.conn.execute(
                "UPDATE plans SET worktree = ? WHERE ref = ?", (str(foreign), "1050")
            )
        plan = self.fetch_plan("1050")

        with self.assertRaises(ValueError) as ctx:
            gitops.cleanup(project, plan)
        self.assertIn("refusing to clean up foreign worktree", str(ctx.exception))
        self.assertTrue(foreign.exists())
        self.assertTrue(Path(path).exists())

    # -- guard -------------------------------------------------------------

    def test_guard_install_idempotent_and_blocks_worker_push(self):
        repo = self.init_repo()
        project = self.add_project(repo)
        bare = self.add_remote(repo)

        hook = Path(gitops.install_guard(project))
        self.assertTrue(hook.is_file())
        self.assertEqual(hook.stat().st_mode & 0o777, 0o755)
        self.assertTrue(gitops.guard_installed(project))
        content = hook.read_text()

        self.assertEqual(gitops.install_guard(project), str(hook))
        self.assertEqual(hook.read_text(), content)
        self.assertTrue(gitops.guard_installed(project))

        self.run_git(repo, "push", "-q", "origin", "main")
        self.assertEqual(self.sha(bare, "main"), self.sha(repo))

        self.commit(repo, "next", "next.txt", "next")
        worker_env = dict(self.git_env)
        worker_env["SWARM_WORKER"] = "1"
        blocked = subprocess.run(
            ["git", "-C", str(repo), "push", "origin", "main"],
            capture_output=True,
            text=True,
            env=worker_env,
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("pushes are blocked for workers", blocked.stderr)
        self.assertEqual(self.sha(bare, "main"), self.sha(repo, "HEAD~1"))

        self.run_git(repo, "push", "-q", "origin", "main")
        self.assertEqual(self.sha(bare, "main"), self.sha(repo))

    def test_guard_refuses_foreign_hook(self):
        repo = self.init_repo()
        project = self.add_project(repo)
        hook = repo / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)

        with self.assertRaises(ValueError) as ctx:
            gitops.install_guard(project)
        self.assertIn("a different pre-push hook exists", str(ctx.exception))
        self.assertIn("exit 0", hook.read_text())
        self.assertFalse(gitops.guard_installed(project))

    def test_guard_installed_false_when_hookspath_set(self):
        repo = self.init_repo()
        project = self.add_project(repo)
        gitops.install_guard(project)
        self.assertTrue(gitops.guard_installed(project))

        self.run_git(repo, "config", "core.hooksPath", ".githooks")
        self.assertFalse(gitops.guard_installed(project))

        self.run_git(repo, "config", "--unset", "core.hooksPath")
        self.assertTrue(gitops.guard_installed(project))

        (repo / ".git" / "hooks" / "pre-push").unlink()
        self.assertFalse(gitops.guard_installed(project))

    def test_remove_guard_leaves_foreign_hook_alone(self):
        repo = self.init_repo()
        project = self.add_project(repo)
        hook = repo / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)

        self.assertFalse(gitops.remove_guard(project))
        self.assertIn("exit 0", hook.read_text())

        hook.unlink()
        self.assertFalse(gitops.remove_guard(project))
        gitops.install_guard(project)
        self.assertTrue(gitops.remove_guard(project))
        self.assertFalse(hook.exists())
        self.assertFalse(gitops.remove_guard(project))

    # -- read helpers ------------------------------------------------------

    def test_read_helpers(self):
        repo = self.init_repo()
        project = self.add_project(repo)
        plan = self.add_plan("demo", "1050", "Plan work")

        first = self.write_commit(repo, "docs/plans/1050.md", "plan", "plan doc")
        (repo / "a.txt").write_text("x")
        (repo / "b.txt").write_text("y")
        self.run_git(repo, "add", "a.txt", "b.txt")
        self.run_git(repo, "commit", "-q", "-m", "two files")
        second = self.sha(repo)

        self.assertEqual(gitops.ib_head(project), second)
        self.assertEqual(
            gitops.plan_commit(project, "docs/plans/1050.md", "2000-01-01T00:00:00Z"),
            first,
        )
        self.assertIsNone(
            gitops.plan_commit(
                project, "docs/plans/1050.md", "2999-01-01T00:00:00Z"
            )
        )
        self.assertIsNone(
            gitops.plan_commit(project, "docs/plans/missing.md", "2000-01-01T00:00:00Z")
        )
        self.assertEqual(set(gitops.commit_files(project, second)), {"a.txt", "b.txt"})

        base_sha = gitops.ib_head(project)
        path, branch, _ = gitops.create_worktree(project, plan)
        self.assertEqual(gitops.new_commits(project, base_sha, branch), 0)
        self.write_commit(path, "work.txt", "w", "work one")
        self.write_commit(path, "more.txt", "m", "work two")
        self.assertEqual(gitops.new_commits(project, base_sha, branch), 2)


if __name__ == "__main__":
    unittest.main()
