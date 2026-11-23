import os
import shutil
import tempfile
import unittest

from swarm import board, db

NOW = "2026-11-17T04:39:00Z"
LATER = "2026-11-17T05:00:00Z"


class _EmptyCursor:
    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __iter__(self):
        return iter(())


class _RaceConn:
    """Proxy whose first matching pre-check SELECT finds nothing."""

    def __init__(self, conn, sql_prefix):
        self._conn = conn
        self._prefix = sql_prefix

    def execute(self, sql, params=()):
        if self._prefix and sql.startswith(self._prefix):
            self._prefix = None
            return _EmptyCursor()
        return self._conn.execute(sql, params)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._conn, name)



class BoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SWARM_HOME")
        self._old_now = os.environ.get("SWARM_NOW")
        os.environ["SWARM_HOME"] = self.tmp.name
        os.environ["SWARM_NOW"] = NOW
        self.repo = tempfile.mkdtemp()
        self.conn = db.connect()
        board.add_project(self.conn, "demo", self.repo)
        self.base = self._last_event_id()

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

    def _last_event_id(self):
        return self.conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM events"
        ).fetchone()[0]

    def _events(self):
        return board.events_since(self.conn, self.base, limit=1000)

    def _goal(self, title="boot check", done_when="check restored"):
        return board.add_goal(self.conn, "demo", title, done_when)

    def _plan(self, ref="1048", title="Boot check restore"):
        return board.add_plan(self.conn, "demo", ref, title)

    def _move(self, *targets, ref="1048", note=None):
        for target in targets:
            board.move_plan(self.conn, "demo", ref, target, note=note)

    def _to_fixing(self):
        self._plan()
        self._move("REVIEW", "APPROVED", "EXECUTING", "VERIFYING", "FIXING")

    def test_add_project_stores_absolute_path_and_event(self):
        row = self.conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        self.assertEqual(row["repo_path"], os.path.realpath(self.repo))
        self.assertEqual(row["plans_dir"], "docs/plans")
        self.assertEqual(row["integration_branch"], "main")
        event = board.events_since(self.conn, 0)[-1]
        self.assertEqual(event["kind"], "project")
        self.assertIn("demo", event["text"])

    def test_add_project_missing_dir_refused(self):
        with self.assertRaises(ValueError):
            board.add_project(self.conn, "ghost", "/nonexistent/repo/path")

    def test_add_project_duplicate_name_refused(self):
        with self.assertRaises(ValueError) as ctx:
            board.add_project(self.conn, "demo", self.repo)
        self.assertEqual(str(ctx.exception), "project demo already exists")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1
        )
        self.assertEqual(len(self._events()), 0)
        board.add_project(self.conn, "second", self.repo)

    def test_get_plan(self):
        plan_id = self._plan()
        row = board.get_plan(self.conn, "demo", "1048")
        self.assertEqual(row["id"], plan_id)
        self.assertEqual(row["state"], "DRAFT")
        with self.assertRaises(LookupError) as ctx:
            board.get_plan(self.conn, "demo", "9999")
        self.assertEqual(str(ctx.exception), "no plan demo/9999")

    def test_add_goal_and_set_state(self):
        goal_id = self._goal()
        row = self.conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        self.assertEqual(row["state"], "QUEUED")
        self.assertEqual(row["priority"], 0)
        board.set_goal_state(self.conn, goal_id, "ACTIVE")
        os.environ["SWARM_NOW"] = LATER
        board.set_goal_state(self.conn, goal_id, "DONE")
        row = self.conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        self.assertEqual(row["state"], "DONE")
        self.assertEqual(row["updated_at"], LATER)
        self.assertEqual([e["kind"] for e in self._events()], ["goal", "goal", "goal"])
        with self.assertRaises(ValueError):
            board.set_goal_state(self.conn, goal_id, "RUNNING")
        with self.assertRaises(ValueError):
            board.set_goal_state(self.conn, 999, "ACTIVE")

    def test_add_plan_defaults_and_event(self):
        goal_id = self._goal()
        plan_id = board.add_plan(self.conn, "demo", "1048", "Boot check",
                                 path="docs/plans/1048.md", goal_id=goal_id)
        row = board.get_plan(self.conn, "demo", "1048")
        self.assertEqual(row["id"], plan_id)
        self.assertEqual(row["state"], "DRAFT")
        self.assertEqual(row["path"], "docs/plans/1048.md")
        self.assertEqual(row["goal_id"], goal_id)
        event = self._events()[-1]
        self.assertEqual(event["kind"], "plan")
        self.assertEqual(event["project"], "demo")
        self.assertEqual(event["plan_id"], plan_id)

    def test_add_plan_duplicate_ref_refused(self):
        self._plan()
        with self.assertRaises(ValueError):
            self._plan(title="again")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 1
        )
        board.add_project(self.conn, "other", self.repo)
        board.add_plan(self.conn, "other", "1048", "same ref, other project")

    def test_add_plan_unknown_project_or_goal_refused(self):
        with self.assertRaises(ValueError):
            board.add_plan(self.conn, "ghost", "1", "t")
        with self.assertRaises(ValueError):
            board.add_plan(self.conn, "demo", "1", "t", goal_id=42)

    def test_add_plan_lost_race_raises_value_error(self):
        self._plan()
        racer = _RaceConn(self.conn, "SELECT 1 FROM plans")
        with self.assertRaises(ValueError) as ctx:
            board.add_plan(racer, "demo", "1048", "again")
        self.assertEqual(str(ctx.exception), "plan demo/1048 already exists")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 1
        )
        self.assertEqual(len(self._events()), 1)
        board.add_plan(self.conn, "demo", "1049", "next")

    def test_move_plan_updates_updated_at_and_fields(self):
        self._plan()
        os.environ["SWARM_NOW"] = LATER
        board.move_plan(self.conn, "demo", "1048", "REVIEW",
                        branch="plan/1048-x", worktree="/tmp/wt", note="ready")
        row = board.get_plan(self.conn, "demo", "1048")
        self.assertEqual(row["state"], "REVIEW")
        self.assertEqual(row["updated_at"], LATER)
        self.assertEqual(row["branch"], "plan/1048-x")
        self.assertEqual(row["worktree"], "/tmp/wt")

    def test_move_plan_event_text(self):
        self._plan()
        board.move_plan(self.conn, "demo", "1048", "REVIEW", note="looks good")
        event = self._events()[-1]
        self.assertEqual(event["kind"], "move")
        self.assertEqual(event["text"], "demo/1048 DRAFT → REVIEW: looks good")
        self.assertEqual(event["project"], "demo")
        self.assertEqual(event["plan_id"], board.get_plan(self.conn, "demo", "1048")["id"])

        board.move_plan(self.conn, "demo", "1048", "APPROVED")
        self.assertEqual(
            self._events()[-1]["text"], "demo/1048 REVIEW → APPROVED"
        )

    def test_move_plan_illegal_transition_refused(self):
        self._plan()
        with self.assertRaises(ValueError):
            board.move_plan(self.conn, "demo", "1048", "EXECUTING")
        self.assertEqual(board.get_plan(self.conn, "demo", "1048")["state"], "DRAFT")

    def test_move_plan_rejects_unknown_field(self):
        self._plan()
        with self.assertRaises(ValueError):
            board.move_plan(self.conn, "demo", "1048", "REVIEW", colour="red")

    def test_move_plan_blocked_roundtrip(self):
        self._plan()
        self._move("REVIEW", "APPROVED", "EXECUTING")
        board.move_plan(self.conn, "demo", "1048", "BLOCKED", note="needs operator")
        row = board.get_plan(self.conn, "demo", "1048")
        self.assertEqual(row["state"], "BLOCKED")
        self.assertEqual(row["blocked_from"], "EXECUTING")
        with self.assertRaises(ValueError):
            board.move_plan(self.conn, "demo", "1048", "APPROVED")
        board.move_plan(self.conn, "demo", "1048", "EXECUTING")
        row = board.get_plan(self.conn, "demo", "1048")
        self.assertEqual(row["state"], "EXECUTING")
        self.assertIsNone(row["blocked_from"])

    def test_move_plan_fix_rounds_counter(self):
        self._to_fixing()
        self.assertEqual(board.get_plan(self.conn, "demo", "1048")["fix_rounds"], 1)
        self._move("VERIFYING", "FIXING")
        self.assertEqual(board.get_plan(self.conn, "demo", "1048")["fix_rounds"], 2)
        self._move("VERIFYING")
        with self.assertRaises(ValueError):
            board.move_plan(self.conn, "demo", "1048", "FIXING")

    def test_set_plan_fields(self):
        self._plan()
        os.environ["SWARM_NOW"] = LATER
        board.set_plan_fields(self.conn, "demo", "1048", branch="plan/1048-x",
                              base_sha="abc123")
        row = board.get_plan(self.conn, "demo", "1048")
        self.assertEqual(row["branch"], "plan/1048-x")
        self.assertEqual(row["base_sha"], "abc123")
        self.assertEqual(row["state"], "DRAFT")
        self.assertEqual(row["updated_at"], LATER)
        event = self._events()[-1]
        self.assertEqual(event["kind"], "plan")
        self.assertIn("demo/1048", event["text"])
        board.set_plan_fields(self.conn, "demo", "1048", merge_sha="def456")
        with self.assertRaises(ValueError):
            board.set_plan_fields(self.conn, "demo", "1048", bogus="x")

    def test_start_run(self):
        plan_id = self._plan()
        run_id = board.start_run(self.conn, "implementer", plan_id=plan_id,
                                 session="sess-1", log_path="/tmp/run.log")
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "RUNNING")
        self.assertEqual(row["kind"], "implementer")
        self.assertEqual(row["started_at"], NOW)
        self.assertIsNone(row["ended_at"])
        event = self._events()[-1]
        self.assertEqual(event["kind"], "run")
        self.assertEqual(event["project"], "demo")
        self.assertEqual(event["plan_id"], plan_id)
        with self.assertRaises(ValueError):
            board.start_run(self.conn, "bogus", plan_id=plan_id)
        with self.assertRaises(ValueError):
            board.start_run(self.conn, "other", plan_id=999)
        board.start_run(self.conn, "other")

    def test_end_run(self):
        run_id = board.start_run(self.conn, "implementer")
        os.environ["SWARM_NOW"] = LATER
        board.end_run(self.conn, run_id, "OK", cost_usd=1.25, summary="done",
                      session="sess-2")
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["cost_usd"], 1.25)
        self.assertEqual(row["summary"], "done")
        self.assertEqual(row["session"], "sess-2")
        self.assertEqual(row["ended_at"], LATER)
        event = self._events()[-1]
        self.assertEqual(event["kind"], "run")
        self.assertIn("OK", event["text"])
        with self.assertRaises(ValueError):
            board.end_run(self.conn, run_id, "OK")
        with self.assertRaises(ValueError):
            board.end_run(self.conn, 999, "OK")
        other = board.start_run(self.conn, "other")
        with self.assertRaises(ValueError):
            board.end_run(self.conn, other, "WHATEVER")
        with self.assertRaises(ValueError):
            board.end_run(self.conn, other, "OK", cost_usd=-1)

    def test_add_ask_and_answer(self):
        plan_id = self._plan()
        ask_id = board.add_ask(self.conn, "Push to origin?", project="demo",
                               plan_id=plan_id, options="yes|no", destructive=True)
        row = self.conn.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        self.assertEqual(row["state"], "OPEN")
        self.assertEqual(row["options"], "yes|no")
        self.assertEqual(row["destructive"], 1)
        self.assertEqual(row["project"], "demo")
        self.assertEqual(row["plan_id"], plan_id)
        self.assertEqual(self._events()[-1]["kind"], "ask")

        os.environ["SWARM_NOW"] = LATER
        board.answer_ask(self.conn, ask_id, "yes, push")
        row = self.conn.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        self.assertEqual(row["state"], "ANSWERED")
        self.assertEqual(row["answer"], "yes, push")
        self.assertEqual(row["answered_at"], LATER)
        self.assertEqual(self._events()[-1]["kind"], "answer")

        with self.assertRaises(ValueError):
            board.answer_ask(self.conn, ask_id, "again")
        with self.assertRaises(ValueError):
            board.answer_ask(self.conn, 999, "x")

    def test_locks(self):
        self.assertTrue(board.take_lock(self.conn, "release-build:demo", "A"))
        self.assertEqual(len(self._events()), 1)
        acquired = self.conn.execute(
            "SELECT acquired_at FROM locks WHERE name = 'release-build:demo'"
        ).fetchone()["acquired_at"]

        os.environ["SWARM_NOW"] = LATER
        self.assertTrue(board.take_lock(self.conn, "release-build:demo", "A"))
        self.assertEqual(len(self._events()), 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT acquired_at FROM locks WHERE name = 'release-build:demo'"
            ).fetchone()["acquired_at"],
            acquired,
        )

        self.assertFalse(board.take_lock(self.conn, "release-build:demo", "B"))
        with self.assertRaises(ValueError):
            board.drop_lock(self.conn, "release-build:demo", "B")
        self.assertEqual(
            self.conn.execute(
                "SELECT holder FROM locks WHERE name = 'release-build:demo'"
            ).fetchone()["holder"],
            "A",
        )

        board.drop_lock(self.conn, "release-build:demo", "A", force=True)
        self.assertIsNone(
            self.conn.execute(
                "SELECT * FROM locks WHERE name = 'release-build:demo'"
            ).fetchone()
        )
        self.assertEqual(len(self._events()), 2)
        self.assertTrue(board.take_lock(self.conn, "release-build:demo", "B"))
        board.drop_lock(self.conn, "release-build:demo", "B")

    def test_take_lock_lost_race_returns_false(self):
        self.assertTrue(board.take_lock(self.conn, "release-build:demo", "A"))
        racer = _RaceConn(self.conn, "SELECT holder FROM locks")
        self.assertFalse(board.take_lock(racer, "release-build:demo", "B"))
        self.assertEqual(
            self.conn.execute(
                "SELECT holder FROM locks WHERE name = 'release-build:demo'"
            ).fetchone()["holder"],
            "A",
        )
        self.assertEqual(len(self._events()), 1)
        self.assertTrue(board.take_lock(self.conn, "fresh", "C"))

    def test_add_note(self):
        board.add_note(self.conn, "plain note")
        board.add_note(self.conn, "chose sqlite", project="demo", kind="decision")
        board.add_note(self.conn, "handing over", kind="handoff")
        board.add_note(self.conn, "refactored the scheduler", project="demo",
                       kind="arch-review")
        kinds = [event["kind"] for event in self._events()]
        self.assertEqual(kinds, ["note", "decision", "handoff", "arch-review"])
        with self.assertRaises(ValueError):
            board.add_note(self.conn, "x", kind="musing")

    def test_month_spend(self):
        first = board.start_run(self.conn, "implementer")
        second = board.start_run(self.conn, "implementer")
        prior = board.start_run(self.conn, "implementer")
        reviewer = board.start_run(self.conn, "reviewer")
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET started_at = ?, cost_usd = ? WHERE id = ?",
                ("2026-11-02T10:00:00Z", 1.5, first),
            )
            self.conn.execute(
                "UPDATE runs SET started_at = ?, cost_usd = ? WHERE id = ?",
                ("2026-11-16T10:00:00Z", 2.0, second),
            )
            self.conn.execute(
                "UPDATE runs SET started_at = ?, cost_usd = ? WHERE id = ?",
                ("2026-10-31T10:00:00Z", 4.0, prior),
            )
            self.conn.execute(
                "UPDATE runs SET started_at = ?, cost_usd = ? WHERE id = ?",
                ("2026-11-10T10:00:00Z", 8.0, reviewer),
            )
        self.assertAlmostEqual(board.month_spend(self.conn), 3.5)
        self.assertAlmostEqual(board.month_spend(self.conn, "2026-10"), 4.0)
        self.assertAlmostEqual(board.month_spend(self.conn, "2025-01"), 0.0)

    def test_events_since(self):
        board.add_note(self.conn, "one")
        board.add_note(self.conn, "two", kind="decision")
        board.add_note(self.conn, "three")
        events = board.events_since(self.conn, self.base, limit=2)
        self.assertEqual([event["text"] for event in events], ["one", "two"])
        events = board.events_since(self.conn, self.base, kinds={"note"})
        self.assertEqual([event["text"] for event in events], ["one", "three"])
        self.assertEqual(board.events_since(self.conn, self._last_event_id()), [])

    def test_set_project_changes_only_given_fields(self):
        before = self._last_event_id()
        board.set_project(self.conn, "demo", auto=True)
        row = self.conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        self.assertEqual(row["auto"], 1)
        self.assertIsNone(row["verify_cmd"])
        self.assertIsNone(row["verify_allow"])
        self.assertEqual(self._last_event_id(), before + 1)

        board.set_project(self.conn, "demo", verify_cmd="make test")
        row = self.conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        self.assertEqual(row["auto"], 1)
        self.assertEqual(row["verify_cmd"], "make test")

        board.set_project(self.conn, "demo", verify_allow="Bash(make:*)")
        row = self.conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        self.assertEqual(row["verify_allow"], "Bash(make:*)")
        event = self._events()[-1]
        self.assertEqual(event["kind"], "project")
        self.assertIn("verify_allow", event["text"])

        before = self._last_event_id()
        board.set_project(self.conn, "demo")
        self.assertEqual(self._last_event_id(), before)
        with self.assertRaises(ValueError):
            board.set_project(self.conn, "ghost", auto=True)

    def test_set_project_stores_implementer_and_rules(self):
        board.set_project(self.conn, "demo", implementer="claude")
        row = self.conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        self.assertEqual(row["implementer"], "claude")
        event = self._events()[-1]
        self.assertEqual(event["kind"], "project")
        self.assertEqual(event["text"], "project demo set implementer")

        board.set_project(self.conn, "demo", rules=["rm -rf /", "git push"])
        row = self.conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        self.assertEqual(row["rules"], '["rm -rf /", "git push"]')
        self.assertEqual(board.project_rules(row), ["rm -rf /", "git push"])
        self.assertIn("rules", self._events()[-1]["text"])

        board.set_project(self.conn, "demo", rules=[])
        row = self.conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        self.assertIsNone(row["rules"])
        self.assertEqual(row["implementer"], "claude")

        with self.assertRaises(ValueError) as ctx:
            board.set_project(self.conn, "demo", implementer="gpt")
        self.assertEqual(str(ctx.exception), "implementer must be omp or claude")
        row = self.conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        self.assertEqual(row["implementer"], "claude")

    def test_archive_project_turns_auto_off(self):
        board.set_project(self.conn, "demo", auto=True)
        board.archive_project(self.conn, "demo")
        row = self.conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        self.assertEqual(row["archived"], 1)
        self.assertEqual(row["auto"], 0)
        event = self._events()[-1]
        self.assertEqual(event["kind"], "project")
        self.assertEqual(event["text"], "project demo disconnected")
        with self.assertRaises(LookupError) as ctx:
            board.archive_project(self.conn, "ghost")
        self.assertEqual(str(ctx.exception), "no project ghost")

    def test_unarchive_project(self):
        board.archive_project(self.conn, "demo")
        board.unarchive_project(self.conn, "demo")
        row = self.conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        self.assertEqual(row["archived"], 0)
        self.assertEqual(row["auto"], 0)
        event = self._events()[-1]
        self.assertEqual(event["kind"], "project")
        self.assertEqual(event["text"], "project demo reconnected")
        with self.assertRaises(LookupError) as ctx:
            board.unarchive_project(self.conn, "ghost")
        self.assertEqual(str(ctx.exception), "no project ghost")

    def test_import_plan_with_done_state(self):
        plan_id = board.import_plan(
            self.conn, "demo", "1050", "Deployed once",
            "docs/plans/1050.md", "DONE",
        )
        row = board.get_plan(self.conn, "demo", "1050")
        self.assertEqual(row["id"], plan_id)
        self.assertEqual(row["state"], "DONE")
        self.assertEqual(row["path"], "docs/plans/1050.md")
        event = self._events()[-1]
        self.assertEqual(event["kind"], "plan")
        self.assertEqual(event["plan_id"], plan_id)
        self.assertEqual(
            event["text"], "demo/1050 imported as DONE: Deployed once"
        )

    def test_import_plan_with_draft_state(self):
        board.import_plan(
            self.conn, "demo", "1051", "Draft plan", "docs/plans/1051.md", "DRAFT"
        )
        row = board.get_plan(self.conn, "demo", "1051")
        self.assertEqual(row["state"], "DRAFT")

    def test_import_plan_refuses_blocked_and_unknown_states(self):
        for state in ("BLOCKED", "RUNNING"):
            with self.assertRaises(ValueError):
                board.import_plan(
                    self.conn, "demo", "1052", "Bad state", "p", state
                )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 0
        )
        with self.assertRaises(ValueError):
            board.import_plan(self.conn, "ghost", "1052", "No project", "p", "DONE")

    def test_import_plan_twice_refused(self):
        board.import_plan(self.conn, "demo", "1053", "Once", "p", "DONE")
        with self.assertRaises(ValueError) as ctx:
            board.import_plan(self.conn, "demo", "1053", "Twice", "p", "DONE")
        self.assertEqual(str(ctx.exception), "plan demo/1053 already exists")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 1
        )

    def test_project_rules_parsing(self):
        for stored, expected in (
            (None, []),
            ('["a"]', ["a"]),
            ("oops", []),
            ('[1, "b"]', ["b"]),
        ):
            with self.conn:
                self.conn.execute(
                    "UPDATE projects SET rules = ? WHERE name = 'demo'",
                    (stored,),
                )
            row = self.conn.execute(
                "SELECT * FROM projects WHERE name = 'demo'"
            ).fetchone()
            self.assertEqual(board.project_rules(row), expected, stored)

    def test_set_plan_ref(self):
        plan_id = self._plan()
        os.environ["SWARM_NOW"] = LATER
        board.set_plan_ref(self.conn, "demo", "1048", "1049")
        row = board.get_plan(self.conn, "demo", "1049")
        self.assertEqual(row["id"], plan_id)
        self.assertEqual(row["updated_at"], LATER)
        with self.assertRaises(LookupError):
            board.get_plan(self.conn, "demo", "1048")
        event = self._events()[-1]
        self.assertEqual(event["kind"], "plan")
        self.assertIn("1049", event["text"])
        with self.assertRaises(LookupError):
            board.set_plan_ref(self.conn, "demo", "404", "500")

    def test_set_plan_ref_collision_refused(self):
        self._plan()
        self._plan(ref="1049")
        with self.assertRaises(ValueError):
            board.set_plan_ref(self.conn, "demo", "1048", "1049")
        board.get_plan(self.conn, "demo", "1048")
        board.get_plan(self.conn, "demo", "1049")

    def test_set_plan_fields_accepts_new_columns(self):
        self._plan()
        board.set_plan_fields(
            self.conn, "demo", "1048", omp_session="sess-9", review_rounds=1,
            last_review_path="/tmp/review.md", operator_note="asked about CI",
            merge_attempts=1, ib_sha="abc123", title="new title",
        )
        row = board.get_plan(self.conn, "demo", "1048")
        self.assertEqual(row["omp_session"], "sess-9")
        self.assertEqual(row["review_rounds"], 1)
        self.assertEqual(row["last_review_path"], "/tmp/review.md")
        self.assertEqual(row["operator_note"], "asked about CI")
        self.assertEqual(row["merge_attempts"], 1)
        self.assertEqual(row["ib_sha"], "abc123")
        self.assertEqual(row["title"], "new title")

    def test_add_ask_dedupes_by_action(self):
        first = board.add_ask(self.conn, "review this?", project="demo",
                              action="review:1048")
        before = self._last_event_id()
        again = board.add_ask(self.conn, "review again?", project="demo",
                              action="review:1048")
        self.assertEqual(again, first)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM asks").fetchone()[0], 1
        )
        self.assertEqual(self._last_event_id(), before)
        self.assertEqual(board.events_since(self.conn, 0)[-1]["kind"], "ask")

        board.answer_ask(self.conn, first, "done")
        fresh = board.add_ask(self.conn, "review next?", project="demo",
                              action="review:1048")
        self.assertNotEqual(fresh, first)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM asks").fetchone()[0], 2
        )

        deduped = board.add_ask(self.conn, "x", project="demo",
                                action="review:1048", dedupe_any_state=True)
        self.assertEqual(deduped, first)
        self.assertEqual(
            self.conn.execute(
                "SELECT action FROM asks WHERE id = ?", (first,)
            ).fetchone()["action"],
            "review:1048",
        )

        plain_a = board.add_ask(self.conn, "no action")
        plain_b = board.add_ask(self.conn, "no action")
        self.assertNotEqual(plain_a, plain_b)

    def test_pending_answers_and_mark_handled(self):
        plan_id = self._plan()
        answered = board.add_ask(self.conn, "push?", project="demo",
                                 plan_id=plan_id, action="push:1048")
        board.add_ask(self.conn, "later?", project="demo", action="later:1048")
        plain = board.add_ask(self.conn, "plain?")
        board.answer_ask(self.conn, answered, "yes")
        board.answer_ask(self.conn, plain, "yes")
        rows = board.pending_answers(self.conn)
        self.assertEqual([row["id"] for row in rows], [answered])
        self.assertEqual(rows[0]["answer"], "yes")
        self.assertIsNone(rows[0]["handled_at"])

        before = self._last_event_id()
        os.environ["SWARM_NOW"] = LATER
        board.mark_handled(self.conn, answered)
        self.assertEqual(self._last_event_id(), before)
        self.assertEqual(board.pending_answers(self.conn), [])
        row = self.conn.execute(
            "SELECT handled_at FROM asks WHERE id = ?", (answered,)
        ).fetchone()
        self.assertEqual(row["handled_at"], LATER)
        with self.assertRaises(ValueError):
            board.mark_handled(self.conn, 999)

    def test_start_run_records_worker_columns(self):
        plan_id = self._plan()
        run_id = board.start_run(
            self.conn, "implementer", plan_id=plan_id, pid=4242,
            report_path="/tmp/run.md", timeout_s=900, purpose="implement",
        )
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        self.assertEqual(row["pid"], 4242)
        self.assertEqual(row["report_path"], "/tmp/run.md")
        self.assertEqual(row["timeout_s"], 900)
        self.assertEqual(row["purpose"], "implement")
        self.assertIsNone(row["exit_code"])

        before = self._last_event_id()
        board.set_run_pid(self.conn, run_id, 5252)
        self.assertEqual(self._last_event_id(), before)
        self.assertEqual(
            self.conn.execute(
                "SELECT pid FROM runs WHERE id = ?", (run_id,)
            ).fetchone()["pid"],
            5252,
        )
        with self.assertRaises(ValueError):
            board.set_run_pid(self.conn, 999, 1)

    def test_end_run_records_exit_code_and_report_path(self):
        run_id = board.start_run(self.conn, "verifier", report_path="/tmp/keep.md")
        board.end_run(self.conn, run_id, "OK", exit_code=0)
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        self.assertEqual(row["exit_code"], 0)
        self.assertEqual(row["report_path"], "/tmp/keep.md")

        other = board.start_run(self.conn, "verifier")
        board.end_run(self.conn, other, "FAILED", exit_code=2,
                      report_path="/tmp/other.md")
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (other,)).fetchone()
        self.assertEqual(row["exit_code"], 2)
        self.assertEqual(row["report_path"], "/tmp/other.md")

    def test_running_runs(self):
        plan_id = self._plan()
        first = board.start_run(self.conn, "implementer", plan_id=plan_id)
        second = board.start_run(self.conn, "verifier")
        board.start_run(self.conn, "reviewer", plan_id=plan_id)
        board.end_run(self.conn, second, "OK")
        ids = [row["id"] for row in board.running_runs(self.conn)]
        self.assertNotIn(second, ids)
        self.assertIn(first, ids)
        self.assertEqual(len(ids), 2)
        plan_ids = [row["id"] for row in board.running_runs(self.conn, plan_id=plan_id)]
        self.assertEqual(len(plan_ids), 2)
        verifiers = [
            row["id"] for row in board.running_runs(self.conn, kinds={"verifier"})
        ]
        self.assertEqual(verifiers, [])
        implementers = [
            row["id"] for row in board.running_runs(self.conn, kinds={"implementer"})
        ]
        self.assertEqual(implementers, [first])

    def test_block_plan_moves_and_asks(self):
        self._plan()
        self._move("REVIEW", "APPROVED", "EXECUTING")
        ask_id = board.block_plan(
            self.conn, "demo", "1048", "missing token\nadd it to config"
        )
        row = board.get_plan(self.conn, "demo", "1048")
        self.assertEqual(row["state"], "BLOCKED")
        self.assertEqual(row["blocked_from"], "EXECUTING")
        ask = self.conn.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        self.assertEqual(ask["action"], "unblock:demo/1048")
        self.assertEqual(ask["question"], "missing token\nadd it to config")
        self.assertEqual(ask["plan_id"], row["id"])
        self.assertEqual(ask["state"], "OPEN")
        self.assertIn("missing token", self._events()[-2]["text"])
        self.assertEqual(self._events()[-1]["kind"], "ask")

    def test_block_plan_on_already_blocked_only_asks(self):
        self._plan()
        self._move("REVIEW", "APPROVED", "EXECUTING")
        board.block_plan(self.conn, "demo", "1048", "first problem")
        row = board.get_plan(self.conn, "demo", "1048")
        before = self._last_event_id()
        ask_id = board.block_plan(self.conn, "demo", "1048", "second problem",
                                  action="unblock:1048:2")
        self.assertEqual(self._last_event_id(), before + 1)
        self.assertEqual(self._events()[-1]["kind"], "ask")
        after = board.get_plan(self.conn, "demo", "1048")
        self.assertEqual(after["state"], "BLOCKED")
        self.assertEqual(after["blocked_from"], row["blocked_from"])
        ask = self.conn.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        self.assertEqual(ask["action"], "unblock:1048:2")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM asks").fetchone()[0], 2
        )

    def test_every_mutating_call_writes_an_event(self):
        before = self._last_event_id()
        goal_id = self._goal()
        self.assertEqual(self._last_event_id(), before + 1)
        before = self._last_event_id()
        plan_id = self._plan()
        self.assertEqual(self._last_event_id(), before + 1)
        before = self._last_event_id()
        board.move_plan(self.conn, "demo", "1048", "REVIEW")
        self.assertEqual(self._last_event_id(), before + 1)
        before = self._last_event_id()
        board.set_plan_fields(self.conn, "demo", "1048", branch="b")
        self.assertEqual(self._last_event_id(), before + 1)
        before = self._last_event_id()
        run_id = board.start_run(self.conn, "implementer", plan_id=plan_id)
        self.assertEqual(self._last_event_id(), before + 1)
        before = self._last_event_id()
        board.end_run(self.conn, run_id, "OK", cost_usd=0.5)
        self.assertEqual(self._last_event_id(), before + 1)
        before = self._last_event_id()
        ask_id = board.add_ask(self.conn, "ok?")
        self.assertEqual(self._last_event_id(), before + 1)
        before = self._last_event_id()
        board.answer_ask(self.conn, ask_id, "yes")
        self.assertEqual(self._last_event_id(), before + 1)
        before = self._last_event_id()
        board.take_lock(self.conn, "instance:demo", "runner")
        self.assertEqual(self._last_event_id(), before + 1)
        before = self._last_event_id()
        board.drop_lock(self.conn, "instance:demo", "runner")
        self.assertEqual(self._last_event_id(), before + 1)
        before = self._last_event_id()
        board.add_note(self.conn, "noted")
        self.assertEqual(self._last_event_id(), before + 1)

        kinds = {event["kind"] for event in self._events()}
        self.assertTrue(
            {"goal", "plan", "move", "run", "ask", "answer", "lock", "note"} <= kinds
        )


if __name__ == "__main__":
    unittest.main()
