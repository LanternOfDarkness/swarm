import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from swarm import board, config, db, status, util

NOW = "2026-11-17T04:39:00Z"
HEADINGS = ("Waiting on you", "Running", "Next", "Done (last 24 h)")


class StatusTest(unittest.TestCase):
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

    def _section(self, text, title):
        lines = text.splitlines()
        start = lines.index(title)
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if (lines[index] in HEADINGS
                    or lines[index] in status.BRIEF_SECTIONS
                    or lines[index] == status.ARCH_SECTION):
                end = index
                break
        return lines[start + 1:end]

    def _seed_all_states(self):
        self._plan("draft", "Draft plan")
        self._plan("review", "Review plan", "REVIEW")
        self._plan("approved", "Approved plan", "REVIEW", "APPROVED")
        self._plan("executing", "Executing plan", "REVIEW", "APPROVED", "EXECUTING")
        self._plan("verifying", "Verifying plan", "REVIEW", "APPROVED", "EXECUTING",
                   "VERIFYING")
        self._plan("fixing", "Fixing plan", "REVIEW", "APPROVED", "EXECUTING",
                   "VERIFYING", "FIXING")
        self._plan("merged", "Merged plan", "REVIEW", "APPROVED", "EXECUTING",
                   "VERIFYING", "MERGED")
        self._plan("measuring", "Measuring plan", "REVIEW", "APPROVED", "EXECUTING",
                   "VERIFYING", "MERGED", "MEASURING")
        self._plan("done", "Done plan", "REVIEW", "APPROVED", "EXECUTING",
                   "VERIFYING", "MERGED", "DONE")
        self._plan("blocked", "Blocked plan", "REVIEW", "BLOCKED")
        self._plan("dropped", "Dropped plan", "DROPPED")

    def test_build_status_shape_and_states(self):
        self._seed_all_states()
        d = status.build_status(self.conn, self.cfg)

        self.assertEqual(
            set(d), {"schema", "waiting", "running", "next", "done", "arch_due",
                     "projects", "header"}
        )
        self.assertEqual(
            set(d["header"]),
            {"disk_free_gb", "disk_label", "disk_floor_gb", "month_spend_usd",
             "monthly_budget_usd", "budget_warn", "locks", "daemon",
             "worktrees", "context_warn"},
        )

        text = status.format_status(d)
        self.assertTrue(text.startswith(f"Swarm status — {NOW}\n"))
        for title in HEADINGS:
            self.assertIn(title, text)

        self.assertIn("- demo/blocked — Blocked plan: blocked (was REVIEW)",
                      self._section(text, "Waiting on you"))
        running = self._section(text, "Running")
        self.assertIn("- demo/review — Review plan: REVIEW", running)
        self.assertIn("- demo/executing — Executing plan: EXECUTING", running)
        self.assertIn("- demo/verifying — Verifying plan: VERIFYING", running)
        self.assertIn("- demo/fixing — Fixing plan: FIXING", running)

        nxt = self._section(text, "Next")
        self.assertIn("- demo/draft — Draft plan: DRAFT", nxt)
        self.assertIn("- demo/approved — Approved plan: APPROVED", nxt)
        self.assertIn("- demo/merged — Merged plan: MERGED", nxt)
        self.assertIn("- demo/measuring — Measuring plan: MEASURING", nxt)

        done = self._section(text, "Done (last 24 h)")
        self.assertIn(f"- demo/done — Done plan: DONE ({NOW})", done)
        self.assertIn(f"- demo/merged — Merged plan: MERGED ({NOW})", done)

        self.assertNotIn("demo/dropped", text)

    def test_empty_board_prints_nothing_under_each_heading(self):
        text = status.format_status(status.build_status(self.conn, self.cfg))
        for title in HEADINGS:
            self.assertEqual(self._section(text, title), ["- nothing"])
        self.assertIn(f"Swarm status — {NOW}", text)
        self.assertIn("locks: none", text)

    def test_missing_disk_path_is_unknown(self):
        self.cfg.disk_path = str(Path(self.tmp.name) / "missing")
        d = status.build_status(self.conn, self.cfg)
        self.assertIsNone(d["header"]["disk_free_gb"])
        text = status.format_status(d)
        self.assertIn("disk: unknown", text)
        self.assertNotIn("Disk C:", text)

    def test_header_has_disk_label_and_text(self):
        self.cfg.disk_label = "C:"
        d = status.build_status(self.conn, self.cfg)
        self.assertEqual(d["header"]["disk_label"], "C:")
        self.assertIn("Disk C:", status.format_status(d))
        self.assertIn('"disk_label": "C:"', json.dumps(d))

    def _merged_plan(self, ref):
        self._plan(ref, ref, "REVIEW", "APPROVED", "EXECUTING", "VERIFYING",
                   "MERGED")

    def test_arch_due_empty_without_projects(self):
        self.conn.execute("DELETE FROM projects")
        self.conn.commit()
        self.assertEqual(status.arch_due(self.conn, self.cfg), [])

    def test_arch_due_eight_merges_without_review(self):
        for index in range(8):
            self._merged_plan(f"merged-{index}")
        self.assertEqual(status.arch_due(self.conn, self.cfg), [
            {"project": "demo", "merged_since": 8, "last_review": None},
        ])

    def test_arch_due_review_after_five_merges_resets_count(self):
        for index in range(5):
            self._merged_plan(f"merged-{index}")
        board.add_note(self.conn, "reviewed", project="demo", kind="arch-review")
        for index in range(5, 7):
            self._merged_plan(f"merged-{index}")
        self.assertEqual(status.arch_due(self.conn, self.cfg), [])

    def test_arch_due_counts_each_plan_once(self):
        for index in range(7):
            self._merged_plan(f"merged-{index}")
        plan_row = self.conn.execute(
            "SELECT id FROM plans WHERE project = 'demo' AND ref = 'merged-0'"
        ).fetchone()
        db.add_event(self.conn, "move", "demo/merged-0 DRAFT → MERGED",
                     project="demo", plan_id=plan_row["id"])
        self.assertEqual(status.arch_due(self.conn, self.cfg), [])
        self._merged_plan("merged-7")
        due = status.arch_due(self.conn, self.cfg)
        self.assertEqual(due[0]["merged_since"], 8)

    def test_arch_due_has_no_time_window(self):
        for index in range(8):
            self._merged_plan(f"merged-{index}")
        with self.conn:
            self.conn.execute(
                "UPDATE events SET ts = '2026-11-10T04:39:00Z' WHERE kind = 'move'"
            )
        self.assertEqual(status.arch_due(self.conn, self.cfg)[0]["merged_since"], 8)

    def test_arch_due_disabled_by_zero(self):
        self.cfg.arch_review_every = 0
        for index in range(8):
            self._merged_plan(f"merged-{index}")
        self.assertEqual(status.arch_due(self.conn, self.cfg), [])

    def test_arch_due_ignores_archived_projects(self):
        for index in range(8):
            self._merged_plan(f"merged-{index}")
        board.archive_project(self.conn, "demo")
        self.assertEqual(status.arch_due(self.conn, self.cfg), [])

    def test_arch_due_project_filter(self):
        other_repo = tempfile.mkdtemp()
        try:
            board.add_project(self.conn, "other", other_repo)
            for index in range(8):
                self._merged_plan(f"merged-{index}")
                board.add_plan(self.conn, "other", f"theirs-{index}", "Theirs")
                board.move_plan(self.conn, "other", f"theirs-{index}", "REVIEW")
                board.move_plan(self.conn, "other", f"theirs-{index}", "APPROVED")
                board.move_plan(self.conn, "other", f"theirs-{index}", "EXECUTING")
                board.move_plan(self.conn, "other", f"theirs-{index}", "VERIFYING")
                board.move_plan(self.conn, "other", f"theirs-{index}", "MERGED")
            due = status.arch_due(self.conn, self.cfg, project="demo")
            self.assertEqual([item["project"] for item in due], ["demo"])
            self.assertEqual(status.arch_due(self.conn, self.cfg, project="other"),
                             [{"project": "other", "merged_since": 8,
                               "last_review": None}])
        finally:
            shutil.rmtree(other_repo, ignore_errors=True)

    def test_arch_due_in_build_status_and_brief(self):
        self._merged_plan("merged-0")
        text = status.format_brief(self.conn, self.cfg)
        self.assertNotIn(status.ARCH_SECTION, text)
        for index in range(1, 8):
            self._merged_plan(f"merged-{index}")
        board.set_goal_state(
            self.conn, board.add_goal(self.conn, "demo", "open goal", "done"),
            "ACTIVE")
        d = status.build_status(self.conn, self.cfg)
        self.assertEqual(d["arch_due"], [
            {"project": "demo", "merged_since": 8, "last_review": None},
        ])
        text = status.format_brief(self.conn, self.cfg)
        self.assertIn(status.ARCH_SECTION, text)
        self.assertEqual(self._section(text, "Active goals"), [
            "- #1 demo: open goal — done when: done",
        ])
        self.assertEqual(self._section(text, status.ARCH_SECTION), [
            "- demo: 8 plans merged since the last review (never) — "
            "suggest /improve-codebase-architecture",
        ])

    def test_arch_due_brief_uses_review_date(self):
        self.cfg.arch_review_every = 2
        for index in range(5):
            self._merged_plan(f"merged-{index}")
        with self.conn:
            self.conn.execute(
                "UPDATE events SET ts = '2026-11-16T10:00:00Z' WHERE kind = 'move'"
            )
        board.add_note(self.conn, "reviewed", project="demo", kind="arch-review")
        for index in range(5, 8):
            self._merged_plan(f"merged-{index}")
        text = status.format_brief(self.conn, self.cfg)
        self.assertEqual(self._section(text, status.ARCH_SECTION), [
            "- demo: 3 plans merged since the last review (2026-11-17) — "
            "suggest /improve-codebase-architecture",
        ])

    def test_archived_project_is_hidden(self):
        board.add_project(self.conn, "gone", self.repo)
        board.add_plan(self.conn, "gone", "hidden", "Hidden plan")
        board.move_plan(self.conn, "gone", "hidden", "REVIEW")
        board.move_plan(self.conn, "gone", "hidden", "APPROVED")
        board.archive_project(self.conn, "gone")

        d = status.build_status(self.conn, self.cfg)
        self.assertEqual([item["name"] for item in d["projects"]], ["demo"])
        self.assertFalse(
            [item for item in d["next"] if item.get("project") == "gone"]
        )
        text = status.format_status(d)
        self.assertNotIn("gone/hidden", text)
        self.assertNotIn('"hidden"', json.dumps(d))
        brief = status.format_brief(self.conn, self.cfg)
        self.assertNotIn("gone/hidden", brief)

    def test_archived_project_keeps_projectless_asks(self):
        board.add_project(self.conn, "gone", self.repo)
        board.add_ask(self.conn, "Approve the budget?")
        board.add_ask(self.conn, "Gone ask?", project="gone")
        board.archive_project(self.conn, "gone")

        d = status.build_status(self.conn, self.cfg)
        questions = [item["question"] for item in d["waiting"]
                     if item["type"] == "ask"]
        self.assertIn("Approve the budget?", questions)
        self.assertNotIn("Gone ask?", questions)
        text = status.format_status(d)
        self.assertIn("- ask #1: Approve the budget?", text)
        self.assertNotIn("Gone ask?", text)
        brief = status.format_brief(self.conn, self.cfg)
        self.assertIn("Approve the budget?", brief)
        self.assertNotIn("Gone ask?", brief)

    def test_header_reports_locks_and_spend(self):
        board.take_lock(self.conn, "instance:demo", "runner")
        d = status.build_status(self.conn, self.cfg)
        header = d["header"]
        self.assertIsInstance(header["disk_free_gb"], float)
        self.assertGreater(header["disk_free_gb"], 0)
        self.assertEqual(header["month_spend_usd"], 0.0)
        self.assertFalse(header["budget_warn"])
        self.assertEqual(header["locks"],
                         [{"name": "instance:demo", "holder": "runner",
                           "since": NOW}])
        text = status.format_status(d)
        self.assertIn("locks: instance:demo (runner, 0 min)", text)
        self.assertIn("opencode $0.00 / $80.00 this month", text)

    def _worktree(self, project, path, *, is_main=0, merged=0, dirty=0,
                  plan_ref=None, target_bytes=None, scanned_at=NOW):
        self.conn.execute(
            "INSERT INTO worktrees (project, path, branch, head, is_main, ahead, "
            "behind, merged, dirty, target_bytes, last_commit_at, plan_ref, "
            "missing, scanned_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project, path, "main" if is_main else "topic", "0" * 40, is_main,
             0, 0, merged, dirty, target_bytes, NOW, plan_ref, 0, scanned_at),
        )
        self.conn.commit()

    def test_header_worktrees_with_scan(self):
        self._plan("cleanup-plan", "Done plan", "REVIEW", "APPROVED", "EXECUTING",
                   "VERIFYING", "MERGED", "DONE")
        self._worktree("demo", "/w/main", is_main=1, merged=1, dirty=0,
                       scanned_at="2026-11-17T03:00:00Z")
        self._worktree("demo", "/w/cleanup", merged=1, dirty=0,
                       plan_ref="cleanup-plan",
                       target_bytes=40_600_000_000,
                       scanned_at="2026-11-17T04:20:00Z")
        self._worktree("demo", "/w/merged", merged=1, dirty=0,
                       scanned_at="2026-11-17T04:10:00Z")
        self._worktree("demo", "/w/active", merged=0, dirty=2,
                       scanned_at="2026-11-17T04:00:00Z")
        d = status.build_status(self.conn, self.cfg)
        self.assertEqual(
            d["header"]["worktrees"],
            {"count": 4, "cleanup": 1, "targets_gb": 40.6,
             "scanned_at": "2026-11-17T04:20:00Z"},
        )
        text = status.format_status(d)
        self.assertIn(" · worktrees: 4 (1 cleanup), targets 40.6 GB", text)

    def test_header_worktrees_without_scan(self):
        d = status.build_status(self.conn, self.cfg)
        self.assertIsNone(d["header"]["worktrees"])
        text = status.format_status(d)
        self.assertIn(" · worktrees: not scanned", text)

    def test_header_worktrees_scope_to_project(self):
        other_repo = tempfile.mkdtemp()
        try:
            board.add_project(self.conn, "other", other_repo)
            self._worktree("demo", "/w/main", is_main=1, merged=1, dirty=0)
            self._worktree("other", "/w/other", target_bytes=5_000_000_000)
            d = status.build_status(self.conn, self.cfg, project="demo")
            self.assertEqual(d["header"]["worktrees"]["count"], 1)
            self.assertEqual(d["header"]["worktrees"]["targets_gb"], 0.0)
            text = status.format_status(d)
            self.assertIn("worktrees: 1 (0 cleanup), targets 0.0 GB", text)
            self.assertNotIn("worktrees: 2 ", text)
            self.assertEqual(d["header"]["worktrees"]["cleanup"], 0)
        finally:
            shutil.rmtree(other_repo, ignore_errors=True)

    def test_brief_shows_last_ten_decisions(self):
        for index in range(30):
            board.add_note(self.conn, f"decision {index:02d}", kind="decision")
        text = status.format_brief(self.conn, self.cfg)
        decisions = self._section(text, "Recent decisions")
        self.assertEqual(len(decisions), 10)
        self.assertIn(f"- {NOW} decision 29", decisions)
        self.assertIn(f"- {NOW} decision 20", decisions)
        self.assertNotIn(f"- {NOW} decision 19", decisions)

    def test_brief_truncates_to_max_lines_and_keeps_headings(self):
        for index in range(30):
            board.add_note(self.conn, f"decision {index:02d}", kind="decision")
        text = status.format_brief(self.conn, self.cfg, max_lines=20)
        lines = text.splitlines()
        self.assertLessEqual(len(lines), 20)
        for title in HEADINGS + status.BRIEF_SECTIONS:
            self.assertIn(title, lines)
        self.assertEqual(lines[-1], status.CUT_LINE)

    def test_build_status_is_json_serialisable(self):
        self._seed_all_states()
        board.add_note(self.conn, "a decision", kind="decision")
        payload = json.dumps(status.build_status(self.conn, self.cfg))
        self.assertIn("demo/done", payload)

    def test_project_filter_narrows_rows(self):
        other_repo = tempfile.mkdtemp()
        try:
            board.add_project(self.conn, "other", other_repo)
            self._plan("mine", "Mine", "REVIEW")
            board.add_plan(self.conn, "other", "theirs", "Theirs")
            d = status.build_status(self.conn, self.cfg, project="demo")
            text = status.format_status(d)
            self.assertIn("demo/mine", text)
            self.assertNotIn("other/theirs", text)
        finally:
            shutil.rmtree(other_repo, ignore_errors=True)

    def _run_item(self, run_id):
        d = status.build_status(self.conn, self.cfg)
        return next(item for item in d["running"] if item["id"] == run_id)

    def claude_line(self, *, input_tokens=0, cache_creation=0, cache_read=0,
                    parent=None, kind="assistant"):
        return json.dumps({
            "type": kind,
            "parent_tool_use_id": parent,
            "message": {"usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            }},
        })

    def test_running_claude_run_reports_context(self):
        run_id = board.start_run(self.conn, "architect")
        log = util.home() / "runs" / f"{run_id}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("\n".join([
            json.dumps({"type": "system", "subtype": "init"}),
            self.claude_line(input_tokens=10, cache_creation=20, cache_read=30),
            self.claude_line(input_tokens=1000, cache_creation=200, cache_read=30000),
            self.claude_line(input_tokens=90000, parent="toolu_1"),
            json.dumps({"type": "result", "result": "ok", "total_cost_usd": 0.1}),
        ]) + "\n")

        self.assertEqual(self._run_item(run_id)["context"],
                         {"tokens": 31200, "window": 200000})

    def test_running_claude_implementer_reports_context(self):
        run_id = board.start_run(self.conn, "implementer",
                                 purpose="implement-claude")
        log = util.home() / "runs" / f"{run_id}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("\n".join([
            self.claude_line(input_tokens=100, cache_creation=200, cache_read=300),
            json.dumps({"type": "result", "result": "ok",
                        "total_cost_usd": 0.1}),
        ]) + "\n")

        self.assertEqual(self._run_item(run_id)["context"],
                         {"tokens": 600, "window": 200000})

    def test_running_omp_run_reports_context(self):
        run_id = board.start_run(self.conn, "implementer")
        log_dir = util.home() / "runs" / f"{run_id}-omp"
        log_dir.mkdir(parents=True)
        (log_dir / "20261117T040000.jsonl").write_text(json.dumps({
            "type": "message_end",
            "message": {"role": "assistant",
                        "usage": {"input": 1, "cacheRead": 1, "cacheWrite": 1}},
        }) + "\n")
        (log_dir / "20261117T041000.jsonl").write_text("\n".join([
            json.dumps({"type": "message_end",
                        "message": {"role": "assistant",
                                    "usage": {"input": 100, "cacheRead": 100,
                                              "cacheWrite": 100}}}),
            json.dumps({"type": "message_end",
                        "message": {"role": "assistant",
                                    "usage": {"input": 640, "cacheRead": 25472,
                                              "cacheWrite": 0}}}),
            json.dumps({"type": "agent_end"}),
        ]) + "\n")

        self.assertEqual(self._run_item(run_id)["context"],
                         {"tokens": 26112, "window": 1000000})

    def test_run_without_or_with_bad_log_has_no_context(self):
        no_log = board.start_run(self.conn, "reviewer")
        bad_log = board.start_run(self.conn, "reviewer")
        (util.home() / "runs").mkdir(parents=True, exist_ok=True)
        (util.home() / "runs" / f"{bad_log}.log").write_text("not json\n{}\n\n")
        runner = board.start_run(self.conn, "runner")
        (util.home() / "runs" / f"{runner}.log").write_text(
            self.claude_line(input_tokens=5000) + "\n")

        self.assertIsNone(self._run_item(no_log)["context"])
        self.assertIsNone(self._run_item(bad_log)["context"])
        self.assertIsNone(self._run_item(runner)["context"])

    def test_header_reports_context_warning_point(self):
        d = status.build_status(self.conn, self.cfg)
        self.assertEqual(d["header"]["context_warn"],
                         {"tokens": 150000, "step": 20000, "alert": 200000})

    def test_projects_report_counts(self):
        board.set_goal_state(
            self.conn, board.add_goal(self.conn, "demo", "open", "done"), "ACTIVE")
        board.set_goal_state(
            self.conn, board.add_goal(self.conn, "demo", "closed", "done"), "DONE")
        self._plan("draft-1", "Draft one")
        self._plan("draft-2", "Draft two")
        self._plan("merged-1", "Merged one", "REVIEW", "APPROVED", "EXECUTING",
                   "VERIFYING", "MERGED")
        other_repo = tempfile.mkdtemp()
        try:
            board.add_project(self.conn, "z", other_repo)
            d = status.build_status(self.conn, self.cfg)
            self.assertEqual(d["projects"], [
                {"name": "demo", "auto": False, "branch": "main",
                 "counts": {"DRAFT": 2, "MERGED": 1, "goals_open": 1}},
                {"name": "z", "auto": False, "branch": "main",
                 "counts": {"goals_open": 0}},
            ])
            filtered = status.build_status(self.conn, self.cfg, project="z")
            self.assertEqual(filtered["projects"], [
                {"name": "z", "auto": False, "branch": "main",
                 "counts": {"goals_open": 0}},
            ])
        finally:
            shutil.rmtree(other_repo, ignore_errors=True)

    def test_header_daemon_off_without_heartbeat(self):
        d = status.build_status(self.conn, self.cfg)
        self.assertEqual(d["header"]["daemon"], {"on": False, "age_s": None})
        self.assertIn("daemon: off", status.format_status(d))

    def test_header_daemon_on_with_fresh_heartbeat(self):
        util.home().joinpath("daemon.heartbeat").write_text("2026-11-17T04:38:30Z")
        d = status.build_status(self.conn, self.cfg)
        self.assertEqual(d["header"]["daemon"], {"on": True, "age_s": 30})
        self.assertIn("daemon: on (30s ago)", status.format_status(d))

    def test_header_daemon_off_with_stale_heartbeat(self):
        util.home().joinpath("daemon.heartbeat").write_text("2026-11-17T04:30:00Z")
        d = status.build_status(self.conn, self.cfg)
        self.assertFalse(d["header"]["daemon"]["on"])
        self.assertIn("daemon: off", status.format_status(d))


if __name__ == "__main__":
    unittest.main()
