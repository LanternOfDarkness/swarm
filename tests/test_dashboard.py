import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swarm import board, config, dashboard, db, util

NOW = "2026-11-17T04:39:00Z"
COLUMNS = ("Planned", "Executing", "Verifying", "Waiting on you",
           "Done / measuring")


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SWARM_HOME")
        self._old_now = os.environ.get("SWARM_NOW")
        os.environ["SWARM_HOME"] = self.tmp.name
        os.environ["SWARM_NOW"] = NOW
        self.repo = tempfile.mkdtemp()
        self.disk = Path(self.tmp.name) / "disk"
        self.disk.mkdir()
        self.cfg = config.load()
        self.cfg.disk_path = str(self.disk)
        self.conn = db.connect()
        board.add_project(self.conn, "demo", self.repo)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.repo, ignore_errors=True)
        self.tmp.cleanup()
        if self._old_home is None:
            os.environ.pop("SWARM_HOME", None)
        else:
            os.environ["SWARM_HOME"] = self._old_home
        if self._old_now is None:
            os.environ.pop("SWARM_NOW", None)
        else:
            os.environ["SWARM_NOW"] = self._old_now

    def _plan(self, ref, title, *targets):
        board.add_plan(self.conn, "demo", ref, title)
        for target in targets:
            board.move_plan(self.conn, "demo", ref, target)

    def _seed(self):
        self._plan("draft", "Draft plan")
        self._plan("executing", "Executing plan", "REVIEW", "APPROVED",
                   "EXECUTING")
        self._plan("fixing", "Fixing plan", "REVIEW", "APPROVED", "EXECUTING",
                   "VERIFYING", "FIXING")
        self._plan("verifying", "Verifying plan", "REVIEW", "APPROVED",
                   "EXECUTING", "VERIFYING")
        self._plan("blocked", "Blocked plan", "REVIEW", "BLOCKED")
        self._plan("merged", "Merged plan", "REVIEW", "APPROVED", "EXECUTING",
                   "VERIFYING", "MERGED")
        self._plan("done", "Done plan", "REVIEW", "APPROVED", "EXECUTING",
                   "VERIFYING", "MERGED", "DONE")
        board.add_goal(self.conn, "demo", "Ship the board",
                       "dashboard renders", priority=2, est_minutes=30)
        board.add_ask(self.conn, "Deploy now?", project="demo",
                      options="yes/no", destructive=True)
        board.add_note(self.conn, "seeded event")

    def test_render_has_columns_and_cards(self):
        self._seed()
        text = dashboard.render(self.conn, self.cfg)

        for title in COLUMNS:
            self.assertIn(title, text)
        self.assertIn("<title>Swarm board</title>", text)
        self.assertNotIn("<script", text)

        self.assertIn("demo/draft", text)
        self.assertIn("Draft plan", text)
        self.assertIn("demo/fixing", text)
        self.assertIn("fix rounds 1", text)
        self.assertIn("updated 0 min ago", text)

        self.assertIn("demo/blocked", text)
        self.assertIn("Deploy now?", text)
        self.assertIn("Ship the board", text)
        self.assertIn("dashboard renders", text)
        self.assertIn("seeded event", text)
        self.assertIn("updated " + NOW, text)
        self.assertIn("Goals", text)
        self.assertIn("Recent events", text)

    def test_script_title_is_escaped(self):
        title = "<script>alert(1)</script>"
        board.add_plan(self.conn, "demo", "xss", title)
        board.add_note(self.conn, title)
        text = dashboard.render(self.conn, self.cfg)

        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", text)
        self.assertNotIn("<script>alert", text)
        self.assertNotIn("<script", text)

    def test_write_default_path_has_refresh_and_no_tmp(self):
        self._seed()
        self.cfg.dashboard_refresh_s = 42
        written = dashboard.write(self.conn, self.cfg)

        self.assertEqual(written, util.home() / "dashboard.html")
        self.assertTrue(written.exists())
        content = written.read_text(encoding="utf-8")
        self.assertIn('<meta http-equiv="refresh" content="42">', content)
        self.assertIn("Draft plan", content)
        self.assertEqual(list(written.parent.glob("*.tmp")), [])

    def test_write_to_explicit_path(self):
        out = Path(self.tmp.name) / "out" / "board.html"
        self.cfg.dashboard_refresh_s = 7
        written = dashboard.write(self.conn, self.cfg, out)

        self.assertEqual(written, out)
        self.assertTrue(out.exists())
        self.assertIn('<meta http-equiv="refresh" content="7">',
                      out.read_text(encoding="utf-8"))
        self.assertEqual(list(out.parent.glob("*.tmp")), [])
        help_path = out.parent / "help.html"
        self.assertTrue(help_path.exists())
        self.assertIn('href="board.html"',
                      help_path.read_text(encoding="utf-8"))

    def test_write_writes_help_next_to_dashboard(self):
        out = Path(self.tmp.name) / "out" / "dashboard.html"
        dashboard.write(self.conn, self.cfg, out)

        help_path = out.parent / "help.html"
        self.assertTrue(help_path.exists())
        self.assertIn('href="help.html"', out.read_text(encoding="utf-8"))
        self.assertIn("Swarm help", help_path.read_text(encoding="utf-8"))
        self.assertNotIn('http-equiv="refresh"',
                         help_path.read_text(encoding="utf-8"))

    def test_second_write_leaves_help_mtime_unchanged(self):
        out = Path(self.tmp.name) / "out" / "dashboard.html"
        dashboard.write(self.conn, self.cfg, out)
        help_path = out.parent / "help.html"
        os.utime(help_path, (1000000000, 1000000000))
        before = help_path.stat().st_mtime

        dashboard.write(self.conn, self.cfg, out)

        self.assertEqual(help_path.stat().st_mtime, before)

    def test_help_write_failure_is_a_warning(self):
        out = Path(self.tmp.name) / "out" / "dashboard.html"
        err = io.StringIO()
        with mock.patch("swarm.helppage.render", side_effect=OSError("boom")), \
                contextlib.redirect_stderr(err):
            written = dashboard.write(self.conn, self.cfg, out)

        self.assertEqual(written, out)
        self.assertTrue(out.exists())
        self.assertIn("swarm: warning: help page:", err.getvalue())
        self.assertEqual(list(out.parent.glob("*.tmp")), [])

    def test_undecodable_help_is_overwritten(self):
        out = Path(self.tmp.name) / "out" / "dashboard.html"
        dashboard.write(self.conn, self.cfg, out)
        help_path = out.parent / "help.html"
        help_path.write_bytes(b"\xff\xfe\x00bad")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            written = dashboard.write(self.conn, self.cfg, out)

        self.assertEqual(written, out)
        self.assertEqual(err.getvalue(), "")
        content = help_path.read_text(encoding="utf-8")
        self.assertIn('href="dashboard.html"', content)

    def test_help_cleanup_failure_is_a_warning(self):
        out_dir = Path(self.tmp.name) / "out"
        out_dir.mkdir()
        (out_dir / "help.html.tmp").mkdir()
        out = out_dir / "dashboard.html"
        err = io.StringIO()
        with mock.patch("swarm.helppage.render", side_effect=OSError("boom")), \
                contextlib.redirect_stderr(err):
            written = dashboard.write(self.conn, self.cfg, out)

        self.assertEqual(written, out)
        self.assertTrue(out.exists())
        self.assertIn("swarm: warning: help page:", err.getvalue())


    def _worktree(self, path, branch="main", is_main=0, ahead=0, behind=0,
                  merged=1, dirty=0, target_bytes=None, last_commit_at=None,
                  plan_ref=None, missing=0, project="demo",
                  scanned_at="2026-11-17T04:09:00Z"):
        self.conn.execute(
            "INSERT INTO worktrees (project, path, branch, head, is_main, "
            "ahead, behind, merged, dirty, target_bytes, last_commit_at, "
            "plan_ref, missing, scanned_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project, path, branch, "0123456789abcdef", is_main, ahead, behind,
             merged, dirty, target_bytes, last_commit_at, plan_ref, missing,
             scanned_at),
        )
        self.conn.commit()

    def _branch(self, name, merged=0, ahead=None, last_commit_at=None,
                plan_ref=None, project="demo",
                scanned_at="2026-11-17T04:09:00Z"):
        self.conn.execute(
            "INSERT INTO branches (project, name, head, merged, ahead, "
            "last_commit_at, plan_ref, scanned_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project, name, "0123456789abcdef", merged, ahead, last_commit_at,
             plan_ref, scanned_at),
        )
        self.conn.commit()

    def _seed_worktrees(self):
        self._plan("draft", "Draft plan")
        self._plan("executing", "Executing plan", "REVIEW", "APPROVED",
                   "EXECUTING")
        self._plan("done", "Done plan", "REVIEW", "APPROVED", "EXECUTING",
                   "VERIFYING", "MERGED", "DONE")
        base = Path(self.repo)
        self._worktree(str(base / "main-wt"), branch="main", is_main=1,
                       merged=1, dirty=0, target_bytes=1_500_000_000,
                       last_commit_at="2026-11-17T04:09:00Z")
        self._worktree(str(base / "executing-wt"), branch="plan/2048-work",
                       merged=1, dirty=0, ahead=0, behind=0,
                       last_commit_at="2026-11-17T04:00:00Z",
                       plan_ref="executing")
        self._worktree(str(base / "active-wt"), branch="feature",
                       merged=0, dirty=2, ahead=2, behind=None,
                       last_commit_at="2026-11-17T03:09:00Z",
                       plan_ref="draft")
        self._worktree(str(base / "merged-wt"), branch="loose-merged",
                       merged=1, dirty=0, ahead=0, behind=0,
                       last_commit_at="2026-11-17T02:39:00Z")
        self._worktree(str(base / "cleanup-wt"), branch="plan/9-done",
                       merged=1, dirty=0, ahead=0, behind=3,
                       last_commit_at="2026-11-17T01:39:00Z",
                       plan_ref="done")
        self._worktree(str(base / "missing-wt"), branch="gone",
                       merged=0, dirty=None, ahead=None, behind=None,
                       missing=1)
        self._branch("feature-x", merged=0, ahead=2,
                     last_commit_at="2026-11-17T03:29:00Z",
                     plan_ref="draft")
        self._branch("old-thing", merged=1, ahead=0,
                     last_commit_at="2026-11-17T01:39:00Z")
        self._branch("running-thing", merged=1, ahead=0,
                     last_commit_at="2026-11-17T02:39:00Z",
                     plan_ref="executing")

    def test_worktrees_section(self):
        self._seed_worktrees()
        text = dashboard.render(self.conn, self.cfg)

        self.assertIn("<h2>Worktrees</h2>", text)
        self.assertIn("demo — scanned 30 min ago", text)
        self.assertIn('overflow-x: auto', text)
        for header in ("Worktree", "Branch", "Plan", "vs main", "Changes",
                       "Target", "Last commit", "Status"):
            self.assertIn(f"<th>{header}</th>", text)

        self.assertIn(f'title="{Path(self.repo) / "main-wt"}"', text)
        self.assertIn('<span class="badge">main</span>', text)
        self.assertIn('<span class="badge warn">cleanup</span>', text)
        self.assertIn('<span class="badge">missing</span>', text)
        self.assertIn('<span class="badge">merged</span>', text)
        self.assertIn('<span class="badge">active</span>', text)
        self.assertIn('done <span class="badge">DONE</span>', text)

        self.assertIn("1.5 GB", text)
        self.assertIn("3 h ago", text)
        self.assertIn("2 changes", text)
        self.assertIn("+2 / −?", text)
        self.assertIn("+0 / −3", text)
        self.assertIn('draft <span class="badge">DRAFT</span>', text)

        self.assertIn(
            '<p class="muted">Loose branches (not merged): '
            "feature-x (draft, +2, 1 h ago)</p>",
            text,
        )
        self.assertIn(
            '<p class="muted">1 merged branches can be deleted.</p>', text
        )
        self.assertNotIn("2 merged branches can be deleted.", text)

        section = text[text.index("<h2>Worktrees</h2>"):]
        main_at = section.index("main-wt")
        executing_at = section.index("executing-wt")
        active_at = section.index("active-wt")
        merged_at = section.index("merged-wt")
        cleanup_at = section.index("cleanup-wt")
        missing_at = section.index("missing-wt")
        self.assertLess(main_at, executing_at)
        self.assertLess(executing_at, active_at)
        self.assertLess(active_at, merged_at)
        self.assertLess(merged_at, cleanup_at)
        self.assertLess(cleanup_at, missing_at)

        executing_row = section[executing_at:]
        executing_row = executing_row[:executing_row.index("</tr>")]
        self.assertIn('<span class="badge">active</span>', executing_row)
        self.assertIn('executing <span class="badge">EXECUTING</span>',
                      executing_row)

        merged_row = section[merged_at:]
        merged_row = merged_row[:merged_row.index("</tr>")]
        self.assertIn('<span class="badge">merged</span>', merged_row)

        cleanup_row = section[cleanup_at:]
        cleanup_row = cleanup_row[:cleanup_row.index("</tr>")]
        self.assertIn('<span class="badge warn">cleanup</span>', cleanup_row)

        self.assertIn("worktree active-wt", text)

    def test_worktree_branch_escaped(self):
        self._worktree(str(Path(self.repo) / "xss-wt"), branch="x<b>y",
                       merged=0, dirty=0)
        text = dashboard.render(self.conn, self.cfg)

        self.assertIn("x&lt;b&gt;y", text)
        self.assertNotIn("x<b>y", text)

    def test_detached_worktree_shows_detached(self):
        self._worktree(str(Path(self.repo) / "detached-wt"), branch=None,
                       merged=0, dirty=0)
        text = dashboard.render(self.conn, self.cfg)

        self.assertIn("<td>detached</td>", text)

    def test_never_scanned_project(self):
        board.add_project(self.conn, "other", self.repo)
        text = dashboard.render(self.conn, self.cfg)

        self.assertIn("other — never scanned — run swarm scan", text)
        self.assertIn("demo — never scanned — run swarm scan", text)

    def test_header_uses_custom_disk_label(self):
        self.cfg.disk_label = "D-lab"
        text = dashboard.render(self.conn, self.cfg)
        self.assertIn("Disk D-lab", text)

    def test_archived_project_is_hidden(self):
        board.add_project(self.conn, "gone", self.repo)
        board.add_plan(self.conn, "gone", "hidden", "Hidden plan")
        board.move_plan(self.conn, "gone", "hidden", "REVIEW")
        board.move_plan(self.conn, "gone", "hidden", "APPROVED")
        board.add_goal(self.conn, "gone", "Gone goal", "done")
        board.archive_project(self.conn, "gone")
        text = dashboard.render(self.conn, self.cfg)
        self.assertNotIn('<span class="ref">hidden</span>', text)
        self.assertNotIn("<td>Gone goal</td>", text)
        self.assertNotIn("gone — never scanned", text)

    def test_archived_project_keeps_projectless_asks(self):
        board.add_project(self.conn, "gone", self.repo)
        board.add_ask(self.conn, "Approve the budget?")
        board.add_ask(self.conn, "Gone ask?", project="gone")
        board.archive_project(self.conn, "gone")
        text = dashboard.render(self.conn, self.cfg)
        self.assertIn('<div class="title">Approve the budget?</div>', text)
        self.assertNotIn('<div class="title">Gone ask?</div>', text)

    def test_header_shows_daemon_on(self):
        util.home().joinpath("daemon.heartbeat").write_text("2026-11-17T04:38:40Z")
        text = dashboard.render(self.conn, self.cfg)
        self.assertIn("daemon: on (20s ago)", text)

    def test_header_shows_daemon_off(self):
        text = dashboard.render(self.conn, self.cfg)
        self.assertIn("daemon: off", text)

    def test_executing_and_verifying_cards_show_running_run(self):
        self._plan("executing", "Executing plan", "REVIEW", "APPROVED",
                   "EXECUTING")
        self._plan("verifying", "Verifying plan", "REVIEW", "APPROVED",
                   "EXECUTING", "VERIFYING")
        executing = board.get_plan(self.conn, "demo", "executing")
        verifying = board.get_plan(self.conn, "demo", "verifying")
        board.start_run(self.conn, "implementer", plan_id=executing["id"],
                        purpose="implement")
        board.start_run(self.conn, "verifier", plan_id=verifying["id"],
                        purpose="verify")
        text = dashboard.render(self.conn, self.cfg)

        self.assertIn("run #1 implementer implement, 0 min ago", text)
        self.assertIn("run #2 verifier verify, 0 min ago", text)
        section = text[text.index("Executing"):text.index("Verifying")]
        self.assertIn("run #1 implementer implement", section)


if __name__ == "__main__":
    unittest.main()
