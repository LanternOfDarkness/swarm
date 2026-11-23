import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from swarm import db, mdsync

NOW = "2026-11-17T04:39:00Z"


class MdsyncTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._old_home = os.environ.get("SWARM_HOME")
        os.environ["SWARM_HOME"] = str(self.root / "state")
        self.addCleanup(self._restore_home)

        self.repo = self.root / "repo"
        (self.repo / "docs" / "plans").mkdir(parents=True)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO projects (name, repo_path, created_at) VALUES (?, ?, ?)",
            ("demo", str(self.repo), NOW),
        )
        self.conn.commit()

    def _restore_home(self):
        if self._old_home is None:
            os.environ.pop("SWARM_HOME", None)
        else:
            os.environ["SWARM_HOME"] = self._old_home

    def add_plan(self, ref="1048", path="docs/plans/0001.md", state="EXECUTING",
                 branch="plan/1048-x", fix_rounds=1):
        cur = self.conn.execute(
            "INSERT INTO plans (project, ref, title, path, state, branch, fix_rounds,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("demo", ref, "Boot check restore", path, state, branch,
             fix_rounds, NOW, NOW),
        )
        self.conn.commit()
        return cur.lastrowid

    def write_plan_file(self, text, rel="docs/plans/0001.md"):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write(text)
        return path

    def test_status_line_exact(self):
        self.add_plan()
        row = self.conn.execute(
            "SELECT * FROM plans WHERE project=? AND ref=?", ("demo", "1048")
        ).fetchone()
        self.assertEqual(
            mdsync.status_line(row),
            "**Swarm status:** EXECUTING · branch plan/1048-x · fix rounds 1"
            " · updated " + NOW,
        )

    def test_status_line_omits_branch_and_fix_rounds(self):
        self.add_plan(state="DONE", branch=None, fix_rounds=0)
        row = self.conn.execute("SELECT * FROM plans").fetchone()
        self.assertEqual(
            mdsync.status_line(row),
            "**Swarm status:** DONE · updated " + NOW,
        )
        self.assertEqual(
            mdsync.status_line({"state": "DRAFT", "updated_at": NOW}),
            "**Swarm status:** DRAFT · updated " + NOW,
        )
        self.assertEqual(
            mdsync.status_line(
                {"state": "FIXING", "branch": "", "fix_rounds": None, "updated_at": NOW}
            ),
            "**Swarm status:** FIXING · updated " + NOW,
        )

    def test_insert_after_h1(self):
        self.add_plan()
        original = "# Plan title\n\nBody line\n"
        path = self.write_plan_file(original)

        result = mdsync.sync(self.conn, "demo", "1048")

        self.assertEqual(result, self.repo / "docs" / "plans" / "0001.md")
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            "# Plan title\n"
            "<!-- swarm:status -->\n"
            "**Swarm status:** EXECUTING · branch plan/1048-x · fix rounds 1"
            " · updated " + NOW + "\n"
            "<!-- /swarm:status -->\n"
            "\nBody line\n",
        )

    def test_insert_at_top_without_h1(self):
        self.add_plan()
        original = "Intro line\nBody line\n"
        path = self.write_plan_file(original)

        mdsync.sync(self.conn, "demo", "1048")

        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.endswith(original))
        self.assertEqual(
            text[: -len(original)],
            "<!-- swarm:status -->\n"
            "**Swarm status:** EXECUTING · branch plan/1048-x · fix rounds 1"
            " · updated " + NOW + "\n"
            "<!-- /swarm:status -->\n",
        )

    def test_second_sync_replaces_without_duplication(self):
        self.add_plan()
        path = self.write_plan_file("# Plan title\n\nTail text\n")

        mdsync.sync(self.conn, "demo", "1048")
        self.conn.execute(
            "UPDATE plans SET state='DONE', branch=NULL, fix_rounds=0 WHERE ref='1048'"
        )
        self.conn.commit()
        mdsync.sync(self.conn, "demo", "1048")

        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("<!-- swarm:status -->"), 1)
        self.assertEqual(text.count("<!-- /swarm:status -->"), 1)
        self.assertIn(
            "**Swarm status:** DONE · updated " + NOW,
            text,
        )
        self.assertNotIn("EXECUTING", text)
        self.assertTrue(text.startswith("# Plan title\n<!-- swarm:status -->\n"))
        self.assertTrue(text.endswith("\n\nTail text\n"))

    def test_surrounding_lines_unchanged(self):
        self.add_plan()
        original = "# Title\n\none  \ntwo\tthree\nfour"
        path = self.write_plan_file(original)

        mdsync.sync(self.conn, "demo", "1048")

        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Title\n"))
        self.assertTrue(text.endswith("\n\none  \ntwo\tthree\nfour"))

    def test_crlf_lines_kept(self):
        self.add_plan()
        original = "one\r\ntwo\r\n"
        path = self.write_plan_file(original)

        mdsync.sync(self.conn, "demo", "1048")

        with path.open("r", encoding="utf-8", newline="") as f:
            text = f.read()
        self.assertIn("<!-- swarm:status -->\r\n", text)
        self.assertTrue(text.endswith(original))

    def test_missing_path_raises(self):
        self.add_plan(path=None)
        with self.assertRaises(ValueError):
            mdsync.sync(self.conn, "demo", "1048")

    def test_missing_plan_file_raises(self):
        self.add_plan()
        with self.assertRaises(FileNotFoundError):
            mdsync.sync(self.conn, "demo", "1048")

    def test_unknown_plan_raises(self):
        with self.assertRaises(LookupError) as ctx:
            mdsync.sync(self.conn, "demo", "9999")
        self.assertEqual(str(ctx.exception), "no plan demo/9999")

    def test_writes_sync_event(self):
        self.add_plan()
        self.write_plan_file("# Plan\n")

        mdsync.sync(self.conn, "demo", "1048")

        rows = self.conn.execute(
            "SELECT * FROM events WHERE kind='sync'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["project"], "demo")
        self.assertEqual(rows[0]["plan_id"], 1)


if __name__ == "__main__":
    unittest.main()
