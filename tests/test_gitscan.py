import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swarm import board, db, gitscan, util

WORKTREE_KEYS = {
    "project",
    "path",
    "branch",
    "head",
    "is_main",
    "ahead",
    "behind",
    "merged",
    "dirty",
    "target_bytes",
    "last_commit_at",
    "plan_ref",
    "missing",
    "scanned_at",
}

BRANCH_KEYS = {
    "project",
    "name",
    "head",
    "merged",
    "ahead",
    "last_commit_at",
    "plan_ref",
    "scanned_at",
}

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}

STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class GitScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self._old_env = {}
        for key, value in {
            "SWARM_HOME": str(self.root / "swarm"),
            "SWARM_NOW": "2026-11-17T04:39:00Z",
            **GIT_ENV,
        }.items():
            self._old_env[key] = os.environ.get(key)
            os.environ[key] = value
        self.addCleanup(self._restore_env)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def _restore_env(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def run_git(self, repo, *args):
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True
        )
        if result.returncode != 0:
            self.fail(f"git {args} failed: {result.stderr}")
        return result.stdout

    def sha(self, repo, ref="HEAD"):
        return self.run_git(repo, "rev-parse", ref).strip()

    def commit(self, repo, message, filename=None, content=None):
        name = filename or message.replace(" ", "-") + ".txt"
        (Path(repo) / name).write_text(content if content is not None else message)
        self.run_git(repo, "add", name)
        self.run_git(repo, "commit", "-q", "-m", message)
        return self.sha(repo)

    def init_repo(self, name="repo"):
        repo = self.root / name
        repo.mkdir()
        self.run_git(repo, "init", "-q", "-b", "main")
        self.commit(repo, "base")
        return repo

    def add_project(self, repo, name="demo", integration_branch="main"):
        board.add_project(
            self.conn, name, str(repo), integration_branch=integration_branch
        )

    def scan(self, project="demo", sizes=False):
        return gitscan.scan_project(self.conn, project, sizes=sizes)

    def by_branch(self, result):
        return {row["branch"]: row for row in result["worktrees"]}

    # -- porcelain parsing -------------------------------------------------

    def test_parse_porcelain_main_linked_detached_prunable(self):
        text = (
            "worktree /repo\n"
            "HEAD aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /repo/linked\n"
            "HEAD bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
            "branch refs/heads/plan/1051-t1\n"
            "\n"
            "worktree /repo/detached\n"
            "HEAD cccccccccccccccccccccccccccccccccccccccc\n"
            "detached\n"
            "\n"
            "worktree /repo/gone\n"
            "HEAD dddddddddddddddddddddddddddddddddddddddd\n"
            "branch refs/heads/old\n"
            "prunable gitdir file points to non-existent location\n"
        )
        trees = gitscan.parse_worktree_porcelain(text)

        self.assertEqual(len(trees), 4)
        for tree in trees:
            self.assertEqual(set(tree), {"path", "head", "branch", "is_main", "prunable"})
        self.assertEqual(trees[0]["path"], "/repo")
        self.assertEqual(trees[0]["branch"], "main")
        self.assertEqual(trees[0]["head"], "a" * 40)
        self.assertTrue(trees[0]["is_main"])
        self.assertFalse(trees[0]["prunable"])

        self.assertEqual(trees[1]["branch"], "plan/1051-t1")
        self.assertFalse(trees[1]["is_main"])

        self.assertIsNone(trees[2]["branch"])
        self.assertEqual(trees[2]["path"], "/repo/detached")
        self.assertFalse(trees[2]["prunable"])

        self.assertEqual(trees[3]["branch"], "old")
        self.assertTrue(trees[3]["prunable"])
        self.assertFalse(trees[3]["is_main"])

    def test_parse_porcelain_skips_bare_blocks(self):
        text = (
            "worktree /srv/bare.git\n"
            "HEAD eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\n"
            "bare\n"
            "\n"
            "worktree /repo\n"
            "HEAD aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "branch refs/heads/main\n"
        )
        trees = gitscan.parse_worktree_porcelain(text)
        self.assertEqual(len(trees), 1)
        self.assertEqual(trees[0]["path"], "/repo")
        self.assertTrue(trees[0]["is_main"])

    # -- cleanup candidate -------------------------------------------------

    def test_cleanup_candidate_definition(self):
        self.assertTrue(gitscan.cleanup_candidate(
            {"is_main": 0, "merged": 1, "dirty": 0, "plan_ref": "0001"}, "DONE"
        ))
        nice_row = {"is_main": 0, "merged": 1, "dirty": 0, "plan_ref": "0001"}
        self.assertFalse(gitscan.cleanup_candidate(
            {"is_main": 1, "merged": 1, "dirty": 0, "plan_ref": "0001"}, "DONE"
        ))
        self.assertFalse(gitscan.cleanup_candidate(
            {**nice_row, "merged": 0}, "DONE"
        ))
        self.assertFalse(gitscan.cleanup_candidate(
            {**nice_row, "dirty": 2}, "DONE"
        ))
        self.assertFalse(gitscan.cleanup_candidate(
            {**nice_row, "plan_ref": None}, "DONE"
        ))
        self.assertFalse(gitscan.cleanup_candidate(nice_row, "EXECUTING"))
        self.assertFalse(gitscan.cleanup_candidate(nice_row, None))
        self.assertFalse(gitscan.cleanup_candidate({"is_main": 0, "merged": 1},
                                                   "DONE"))
        row = self.conn.execute(
            "SELECT ? AS is_main, ? AS merged, ? AS dirty, ? AS plan_ref",
            (0, 1, 0, "0001"),
        ).fetchone()
        self.assertTrue(gitscan.cleanup_candidate(row, "MERGED"))

    def test_worktree_status_definition(self):
        self.assertEqual(gitscan.worktree_status({"missing": 1}, None), "missing")
        self.assertEqual(gitscan.worktree_status(
            {"missing": 0, "is_main": 1, "merged": 1, "dirty": 0}, None
        ), "main")
        self.assertEqual(gitscan.worktree_status(
            {"is_main": 0, "merged": 1, "dirty": 0, "plan_ref": "0001"}, "DONE"
        ), "cleanup")
        self.assertEqual(gitscan.worktree_status(
            {"is_main": 0, "merged": 1, "dirty": 0, "plan_ref": "0001"},
            "EXECUTING"
        ), "active")
        self.assertEqual(gitscan.worktree_status(
            {"is_main": 0, "merged": 1, "dirty": 0, "plan_ref": None}, None
        ), "merged")
        self.assertEqual(gitscan.worktree_status(
            {"is_main": 0, "merged": 0, "dirty": 2, "plan_ref": None}, None
        ), "active")
        self.assertEqual(gitscan.worktree_status({}, None), "active")

    def test_branch_deletable_definition(self):
        self.assertTrue(gitscan.branch_deletable({"merged": 1}, None))
        self.assertTrue(gitscan.branch_deletable(
            {"merged": 1, "plan_ref": "0001"}, "DONE"
        ))
        self.assertTrue(gitscan.branch_deletable(
            {"merged": 1, "plan_ref": "0001"}, "MERGED"
        ))
        self.assertFalse(gitscan.branch_deletable(
            {"merged": 1, "plan_ref": "0001"}, "EXECUTING"
        ))
        self.assertFalse(gitscan.branch_deletable(
            {"merged": 1, "plan_ref": "missing"}, None
        ))
        self.assertFalse(gitscan.branch_deletable({"merged": 0}, None))
        self.assertFalse(gitscan.branch_deletable({}, None))

    # -- scan --------------------------------------------------------------

    def test_scan_three_worktrees(self):
        repo = self.init_repo()
        merged_wt = self.root / "wt-merged"
        self.run_git(repo, "worktree", "add", "-q", "-b", "merged-branch", str(merged_wt))
        active_wt = self.root / "wt-active"
        self.run_git(repo, "worktree", "add", "-q", "-b", "plan/1051-t1", str(active_wt))
        self.commit(active_wt, "one")
        self.commit(active_wt, "two")
        (active_wt / "scratch.txt").write_text("x\n")
        self.add_project(repo)

        board.add_plan(self.conn, "demo", "0900", "merged worktree")
        board.set_plan_fields(self.conn, "demo", "0900", worktree=str(merged_wt))
        for target in ("REVIEW", "APPROVED", "EXECUTING", "VERIFYING", "MERGED"):
            board.move_plan(self.conn, "demo", "0900", target)

        result = self.scan()
        states = gitscan.plan_states(self.conn, "demo")

        self.assertEqual(
            set(result),
            {"text", "worktrees", "branches", "worktree_count", "cleanup_count",
             "unmerged_count", "merged_count", "targets_gb"},
        )
        self.assertEqual(result["worktree_count"], 3)
        self.assertEqual(result["cleanup_count"], 1)
        for row in result["worktrees"]:
            self.assertEqual(set(row), WORKTREE_KEYS)
            self.assertEqual(row["project"], "demo")
            self.assertEqual(row["missing"], 0)
            self.assertEqual(row["scanned_at"], util.now())
            self.assertRegex(row["last_commit_at"], STAMP)
            util.parse_ts(row["last_commit_at"])

        rows = self.by_branch(result)
        self.assertEqual(set(rows), {"main", "merged-branch", "plan/1051-t1"})
        main = rows["main"]
        self.assertEqual(main["is_main"], 1)
        self.assertEqual(main["dirty"], 0)
        self.assertEqual(main["merged"], 1)
        self.assertFalse(gitscan.cleanup_candidate(main, None))
        self.assertEqual(gitscan.worktree_status(main, None), "main")

        merged = rows["merged-branch"]
        self.assertEqual(merged["is_main"], 0)
        self.assertEqual((merged["ahead"], merged["behind"]), (0, 0))
        self.assertEqual(merged["merged"], 1)
        self.assertEqual(merged["dirty"], 0)
        self.assertEqual(merged["plan_ref"], "0900")
        merged_state = states.get(("demo", merged["plan_ref"]))
        self.assertTrue(gitscan.cleanup_candidate(merged, merged_state))
        self.assertEqual(gitscan.worktree_status(merged, merged_state), "cleanup")

        active = rows["plan/1051-t1"]
        self.assertEqual(active["is_main"], 0)
        self.assertEqual((active["ahead"], active["behind"]), (2, 0))
        self.assertEqual(active["merged"], 0)
        self.assertEqual(active["dirty"], 1)
        self.assertIsNone(active["plan_ref"])
        self.assertFalse(gitscan.cleanup_candidate(active, None))
        self.assertEqual(gitscan.worktree_status(active, None), "active")

        # An unlinked worktree that is merged and clean is not cleanup.
        self.assertFalse(gitscan.cleanup_candidate(
            {**main, "is_main": 0, "plan_ref": None}, None
        ))
        self.assertEqual(gitscan.worktree_status(
            {**main, "is_main": 0, "plan_ref": None}, None
        ), "merged")

        self.assertEqual(result["branches"], [])
        self.assertEqual(result["unmerged_count"], 0)
        self.assertEqual(result["merged_count"], 0)
        self.assertEqual(result["targets_gb"], 0.0)
        self.assertEqual(
            result["text"],
            "demo: 3 worktrees (1 cleanup), 0 unmerged branches, "
            "0 merged branches, targets 0.0 GB",
        )
        stored = self.conn.execute(
            "SELECT text, kind FROM events WHERE project = ? AND kind = 'scan'",
            ("demo",),
        ).fetchall()
        self.assertEqual([row["text"] for row in stored], [result["text"]])

    def test_fresh_worktree_linked_to_executing_plan_is_active(self):
        repo = self.init_repo()
        wt = self.root / "wt-fresh"
        self.run_git(repo, "worktree", "add", "-q", "-b", "worker-a", str(wt))
        self.add_project(repo)
        board.add_plan(self.conn, "demo", "2048", "fresh worker")
        board.set_plan_fields(self.conn, "demo", "2048", worktree=str(wt))
        for target in ("REVIEW", "APPROVED", "EXECUTING"):
            board.move_plan(self.conn, "demo", "2048", target)

        result = self.scan()
        states = gitscan.plan_states(self.conn, "demo")
        row = self.by_branch(result)["worker-a"]

        self.assertEqual(row["plan_ref"], "2048")
        self.assertEqual((row["merged"], row["dirty"]), (1, 0))
        self.assertEqual(states.get(("demo", row["plan_ref"])), "EXECUTING")
        self.assertFalse(gitscan.cleanup_candidate(
            row, states.get(("demo", row["plan_ref"]))
        ))
        self.assertEqual(gitscan.worktree_status(
            row, states.get(("demo", row["plan_ref"]))
        ), "active")
        self.assertEqual(result["cleanup_count"], 0)

    def test_fresh_worktree_linked_to_merged_plan_is_cleanup(self):
        repo = self.init_repo()
        wt = self.root / "wt-fresh"
        self.run_git(repo, "worktree", "add", "-q", "-b", "worker-b", str(wt))
        self.add_project(repo)
        board.add_plan(self.conn, "demo", "2049", "finished worker")
        board.set_plan_fields(self.conn, "demo", "2049", worktree=str(wt))
        for target in ("REVIEW", "APPROVED", "EXECUTING", "VERIFYING", "MERGED"):
            board.move_plan(self.conn, "demo", "2049", target)

        result = self.scan()
        states = gitscan.plan_states(self.conn, "demo")
        row = self.by_branch(result)["worker-b"]

        self.assertEqual(row["plan_ref"], "2049")
        self.assertEqual((row["merged"], row["dirty"]), (1, 0))
        self.assertEqual(states.get(("demo", row["plan_ref"])), "MERGED")
        self.assertTrue(gitscan.cleanup_candidate(
            row, states.get(("demo", row["plan_ref"]))
        ))
        self.assertEqual(gitscan.worktree_status(
            row, states.get(("demo", row["plan_ref"]))
        ), "cleanup")
        self.assertEqual(result["cleanup_count"], 1)

    def test_unlinked_merged_clean_worktree_is_merged(self):
        repo = self.init_repo()
        wt = self.root / "wt-loose"
        self.run_git(repo, "worktree", "add", "-q", "-b", "loose", str(wt))
        self.add_project(repo)

        result = self.scan()
        row = self.by_branch(result)["loose"]

        self.assertIsNone(row["plan_ref"])
        self.assertEqual((row["merged"], row["dirty"]), (1, 0))
        self.assertFalse(gitscan.cleanup_candidate(row, None))
        self.assertEqual(gitscan.worktree_status(row, None), "merged")
        self.assertEqual(result["cleanup_count"], 0)

    def test_target_size_and_sizes_false_preserves(self):
        repo = self.init_repo()
        target = repo / "target"
        target.mkdir()
        (target / "big.bin").write_bytes(b"x" * 102400)
        self.add_project(repo)

        self.assertGreaterEqual(gitscan.target_size(repo), 102400)
        self.assertIsNone(gitscan.target_size(self.root / "nope"))

        first = self.scan(sizes=True)
        main = first["worktrees"][0]
        self.assertGreaterEqual(main["target_bytes"], 102400)
        self.assertGreater(first["targets_gb"], 0.0)

        shutil.rmtree(target)
        second = self.scan(sizes=False)
        self.assertEqual(second["worktrees"][0]["target_bytes"], main["target_bytes"])

        third = self.scan(sizes=True)
        self.assertIsNone(third["worktrees"][0]["target_bytes"])

    def test_plan_link_paths(self):
        repo = self.init_repo()
        self.add_project(repo, name="links")
        path_by_worktree = self.root / "wt-by-worktree"
        path_by_branch = self.root / "wt-by-branch"
        path_by_number = self.root / "wt-by-number"
        path_unlinked = self.root / "wt-unlinked"
        path_relative = self.root / "wt-relative"
        symlink_target = self.root / "wt-symlink-target"
        symlink_path = self.root / "wt-symlink-link"
        self.run_git(repo, "worktree", "add", "-q", "-b", "feature-a",
                     str(path_by_worktree))
        self.run_git(repo, "worktree", "add", "-q", "-b", "topic-b",
                     str(path_by_branch))
        self.run_git(repo, "worktree", "add", "-q", "-b", "plan/1051-t1",
                     str(path_by_number))
        self.run_git(repo, "worktree", "add", "-q", "-b", "plan/9999-x",
                     str(path_unlinked))
        self.run_git(repo, "worktree", "add", "-q", "-b", "rel-b",
                     str(path_relative))
        self.run_git(repo, "worktree", "add", "-q", "-b", "sym-b",
                     str(symlink_target))
        try:
            symlink_path.symlink_to(symlink_target, target_is_directory=True)
        except OSError:
            self.skipTest("cannot create symlinks on this platform")

        board.add_plan(self.conn, "links", "0001", "by worktree")
        board.set_plan_fields(self.conn, "links", "0001",
                              worktree=str(path_by_worktree))
        board.add_plan(self.conn, "links", "0002", "by branch")
        board.set_plan_fields(self.conn, "links", "0002", branch="topic-b")
        board.add_plan(self.conn, "links", "1051", "by number")
        board.add_plan(self.conn, "links", "0003", "by relative worktree")
        board.set_plan_fields(self.conn, "links", "0003",
                              worktree=os.path.relpath(path_relative))
        board.add_plan(self.conn, "links", "0004", "by symlinked worktree")
        board.set_plan_fields(self.conn, "links", "0004",
                              worktree=str(symlink_path))

        result = self.scan("links")
        rows = self.by_branch(result)
        self.assertEqual(rows["feature-a"]["plan_ref"], "0001")
        self.assertEqual(rows["topic-b"]["plan_ref"], "0002")
        self.assertEqual(rows["plan/1051-t1"]["plan_ref"], "1051")
        self.assertIsNone(rows["plan/9999-x"]["plan_ref"])
        self.assertIsNone(rows["main"]["plan_ref"])
        self.assertEqual(rows["rel-b"]["plan_ref"], "0003")
        self.assertEqual(rows["sym-b"]["plan_ref"], "0004")

        # A relative plans.worktree matches the absolute scan path.
        self.assertEqual(
            gitscan.link_plan(self.conn, "links", str(path_relative), None),
            "0003",
        )
        # A symlinked and a real path match each other in either direction.
        self.assertEqual(
            gitscan.link_plan(self.conn, "links", str(symlink_path), None),
            "0004",
        )
        self.assertEqual(
            gitscan.link_plan(self.conn, "links", str(symlink_target), None),
            "0004",
        )
        board.set_plan_fields(self.conn, "links", "0004",
                              worktree=str(symlink_target))
        self.assertEqual(
            gitscan.link_plan(self.conn, "links", str(symlink_path), None),
            "0004",
        )

        # The worktree-path rule wins over the branch rules.
        self.assertEqual(
            gitscan.link_plan(self.conn, "links", str(path_by_worktree), "topic-b"),
            "0001",
        )
        self.assertEqual(
            gitscan.link_plan(self.conn, "links", "/no/such/path", "topic-b"),
            "0002",
        )
        self.assertIsNone(gitscan.link_plan(self.conn, "links", "/no/such/path", None))

    def test_branches_snapshot(self):
        repo = self.init_repo()
        self.add_project(repo, name="branches")
        active_wt = self.root / "wt-active"
        self.run_git(repo, "worktree", "add", "-q", "-b", "wt-branch", str(active_wt))
        self.run_git(repo, "branch", "loose-merged")
        temp_wt = self.root / "wt-temp"
        self.run_git(repo, "worktree", "add", "-q", "-b", "plan/1051-lo", str(temp_wt))
        self.commit(temp_wt, "only")
        self.run_git(repo, "worktree", "remove", "--force", str(temp_wt))

        board.add_plan(self.conn, "branches", "0007", "merged branch plan")
        board.set_plan_fields(self.conn, "branches", "0007", branch="loose-merged")
        board.add_plan(self.conn, "branches", "1051", "number plan")

        result = self.scan("branches")
        rows = {row["name"]: row for row in result["branches"]}
        for row in result["branches"]:
            self.assertEqual(set(row), BRANCH_KEYS)
            self.assertEqual(row["project"], "branches")
            self.assertRegex(row["last_commit_at"], STAMP)
            util.parse_ts(row["last_commit_at"])

        self.assertEqual(set(rows), {"loose-merged", "plan/1051-lo"})
        self.assertNotIn("main", rows)
        self.assertNotIn("wt-branch", rows)

        merged = rows["loose-merged"]
        self.assertEqual(merged["merged"], 1)
        self.assertEqual(merged["ahead"], 0)
        self.assertEqual(merged["plan_ref"], "0007")

        unmerged = rows["plan/1051-lo"]
        self.assertEqual(unmerged["merged"], 0)
        self.assertEqual(unmerged["ahead"], 1)
        self.assertEqual(unmerged["plan_ref"], "1051")

        self.assertEqual(result["unmerged_count"], 1)
        self.assertEqual(result["merged_count"], 1)
        self.assertEqual(
            result["text"],
            "branches: 2 worktrees (0 cleanup), 1 unmerged branches, "
            "1 merged branches, targets 0.0 GB",
        )
        stored = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM branches WHERE project = ?", ("branches",)
            )
        }
        self.assertEqual(stored, {"loose-merged", "plan/1051-lo"})

    def test_rescan_drops_removed_worktree(self):
        repo = self.init_repo()
        self.add_project(repo, name="rescan")
        side_wt = self.root / "wt-side"
        self.run_git(repo, "worktree", "add", "-q", "-b", "side", str(side_wt))

        first = self.scan("rescan")
        self.assertEqual(first["worktree_count"], 2)

        self.run_git(repo, "worktree", "remove", "--force", str(side_wt))
        second = self.scan("rescan")
        self.assertEqual(second["worktree_count"], 1)
        self.assertEqual(
            [row["path"] for row in second["worktrees"]], [str(side_wt.parent / "repo")]
        )
        stored = self.conn.execute(
            "SELECT COUNT(*) FROM worktrees WHERE project = ?", ("rescan",)
        ).fetchone()[0]
        self.assertEqual(stored, 1)

    def test_deleted_worktree_dir_is_missing(self):
        repo = self.init_repo()
        self.add_project(repo, name="missing-wt")
        gone_wt = self.root / "wt-gone"
        self.run_git(repo, "worktree", "add", "-q", "-b", "gone", str(gone_wt))
        shutil.rmtree(gone_wt)

        result = self.scan("missing-wt")
        rows = self.by_branch(result)
        gone = rows["gone"]
        self.assertEqual(gone["missing"], 1)
        self.assertIsNone(gone["dirty"])
        self.assertIsNone(gone["target_bytes"])
        self.assertIsNone(gone["last_commit_at"])
        self.assertEqual((gone["ahead"], gone["behind"]), (0, 0))
        self.assertEqual(gone["merged"], 1)
        self.assertFalse(gitscan.cleanup_candidate(gone, None))
        self.assertEqual(rows["main"]["missing"], 0)

    def test_missing_project_and_repo_dir(self):
        with self.assertRaises(LookupError):
            gitscan.scan_project(self.conn, "nope")

        repo = self.init_repo()
        self.add_project(repo, name="gone")
        stored_path = self.conn.execute(
            "SELECT repo_path FROM projects WHERE name = ?", ("gone",)
        ).fetchone()["repo_path"]
        shutil.rmtree(repo)
        with self.assertRaises(ValueError) as raised:
            self.scan("gone")
        self.assertEqual(
            str(raised.exception), f"gone: repo {stored_path} not found"
        )

    def test_one_scan_event_per_scan(self):
        repo = self.init_repo()
        self.add_project(repo, name="events")
        self.scan("events")
        self.scan("events")
        rows = self.conn.execute(
            "SELECT text FROM events WHERE project = ? AND kind = 'scan'", ("events",)
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["text"], rows[1]["text"])
        self.assertTrue(rows[0]["text"].startswith("events: 1 worktrees ("))
        kinds = {
            row["kind"]
            for row in self.conn.execute(
                "SELECT DISTINCT kind FROM events WHERE project = ?", ("events",)
            )
        }
        self.assertEqual(kinds, {"project", "scan"})


class GitTimeoutTest(unittest.TestCase):
    def test_git_timeout_becomes_value_error(self):
        expired = subprocess.TimeoutExpired(cmd="git", timeout=0.001)
        with mock.patch("swarm.gitscan.subprocess.run", side_effect=expired):
            with self.assertRaises(ValueError) as raised:
                gitscan.git("/repo", "status", "--porcelain", timeout=0.001)
        self.assertEqual(
            str(raised.exception),
            "git status --porcelain: timed out after 0.001 s",
        )

    def test_branch_exists_timeout_becomes_value_error(self):
        expired = subprocess.TimeoutExpired(cmd="git", timeout=gitscan.GIT_TIMEOUT)
        with mock.patch("swarm.gitscan.subprocess.run", side_effect=expired):
            with self.assertRaises(ValueError) as raised:
                gitscan._branch_exists("/repo", "main")
        self.assertEqual(
            str(raised.exception),
            "git rev-parse --verify --quiet refs/heads/main: "
            f"timed out after {gitscan.GIT_TIMEOUT} s",
        )

    def test_is_merged_timeout_becomes_value_error(self):
        expired = subprocess.TimeoutExpired(cmd="git", timeout=gitscan.GIT_TIMEOUT)
        with mock.patch("swarm.gitscan.subprocess.run", side_effect=expired):
            with self.assertRaises(ValueError) as raised:
                gitscan._is_merged("/repo", "abc123", "main")
        self.assertEqual(
            str(raised.exception),
            "git merge-base --is-ancestor abc123 main: "
            f"timed out after {gitscan.GIT_TIMEOUT} s",
        )


if __name__ == "__main__":
    unittest.main()
