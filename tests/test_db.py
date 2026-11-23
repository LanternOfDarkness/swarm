import os
import sqlite3
import tempfile
import unittest

from swarm import db

TABLES = {
    "projects",
    "goals",
    "plans",
    "runs",
    "asks",
    "locks",
    "events",
    "worktrees",
    "branches",
}

WORKTREE_COLUMNS = [
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
]

BRANCH_COLUMNS = [
    "project",
    "name",
    "head",
    "merged",
    "ahead",
    "last_commit_at",
    "plan_ref",
    "scanned_at",
]


V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  name TEXT PRIMARY KEY,
  repo_path TEXT NOT NULL,
  plans_dir TEXT NOT NULL DEFAULT 'docs/plans',
  gates_file TEXT,
  verify_cmd TEXT,
  integration_branch TEXT NOT NULL DEFAULT 'main',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL REFERENCES projects(name),
  title TEXT NOT NULL,
  done_when TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  est_minutes INTEGER,
  state TEXT NOT NULL DEFAULT 'QUEUED',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL REFERENCES projects(name),
  ref TEXT NOT NULL,
  title TEXT NOT NULL,
  path TEXT,
  goal_id INTEGER REFERENCES goals(id),
  state TEXT NOT NULL DEFAULT 'DRAFT',
  blocked_from TEXT,
  fix_rounds INTEGER NOT NULL DEFAULT 0,
  branch TEXT,
  worktree TEXT,
  base_sha TEXT,
  merge_sha TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project, ref)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  plan_id INTEGER REFERENCES plans(id),
  kind TEXT NOT NULL,
  session TEXT,
  status TEXT NOT NULL DEFAULT 'RUNNING',
  cost_usd REAL NOT NULL DEFAULT 0,
  log_path TEXT,
  summary TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT
);
CREATE TABLE IF NOT EXISTS asks (
  id INTEGER PRIMARY KEY,
  project TEXT REFERENCES projects(name),
  plan_id INTEGER REFERENCES plans(id),
  question TEXT NOT NULL,
  options TEXT,
  destructive INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'OPEN',
  answer TEXT,
  created_at TEXT NOT NULL,
  answered_at TEXT
);
CREATE TABLE IF NOT EXISTS locks (
  name TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  note TEXT,
  acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  project TEXT,
  plan_id INTEGER,
  kind TEXT NOT NULL,
  text TEXT NOT NULL
);
"""

V2_EXTRA = """
CREATE TABLE IF NOT EXISTS worktrees (
  project TEXT NOT NULL REFERENCES projects(name),
  path TEXT NOT NULL,
  branch TEXT,
  head TEXT NOT NULL,
  is_main INTEGER NOT NULL DEFAULT 0,
  ahead INTEGER,
  behind INTEGER,
  merged INTEGER NOT NULL DEFAULT 0,
  dirty INTEGER,
  target_bytes INTEGER,
  last_commit_at TEXT,
  plan_ref TEXT,
  missing INTEGER NOT NULL DEFAULT 0,
  scanned_at TEXT NOT NULL,
  PRIMARY KEY (project, path)
);
CREATE TABLE IF NOT EXISTS branches (
  project TEXT NOT NULL REFERENCES projects(name),
  name TEXT NOT NULL,
  head TEXT NOT NULL,
  merged INTEGER NOT NULL DEFAULT 0,
  ahead INTEGER,
  last_commit_at TEXT,
  plan_ref TEXT,
  scanned_at TEXT NOT NULL,
  PRIMARY KEY (project, name)
);
"""

V3_EXTRA = """
ALTER TABLE projects ADD COLUMN auto INTEGER NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN verify_allow TEXT;
ALTER TABLE plans ADD COLUMN omp_session TEXT;
ALTER TABLE plans ADD COLUMN review_rounds INTEGER NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN last_review_path TEXT;
ALTER TABLE plans ADD COLUMN operator_note TEXT;
ALTER TABLE plans ADD COLUMN merge_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN ib_sha TEXT;
ALTER TABLE runs ADD COLUMN pid INTEGER;
ALTER TABLE runs ADD COLUMN exit_code INTEGER;
ALTER TABLE runs ADD COLUMN report_path TEXT;
ALTER TABLE runs ADD COLUMN timeout_s INTEGER;
ALTER TABLE runs ADD COLUMN purpose TEXT;
ALTER TABLE asks ADD COLUMN action TEXT;
ALTER TABLE asks ADD COLUMN handled_at TEXT;
"""

V3_COLUMNS = {
    "projects": {"auto", "verify_allow"},
    "plans": {"omp_session", "review_rounds", "last_review_path", "operator_note",
              "merge_attempts", "ib_sha"},
    "runs": {"pid", "exit_code", "report_path", "timeout_s", "purpose"},
    "asks": {"action", "handled_at"},
}

V4_EXTRA = """
ALTER TABLE projects ADD COLUMN implementer TEXT NOT NULL DEFAULT 'omp';
ALTER TABLE projects ADD COLUMN rules TEXT;
ALTER TABLE projects ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
"""

V4_COLUMNS = {
    "projects": {"implementer", "rules", "archived"},
}

V5_COLUMNS = {
    "plans": {"window", "window_ok", "measure_from"},
    "runs": {"step"},
}


class DbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_home = os.environ.get("SWARM_HOME")
        os.environ["SWARM_HOME"] = self.tmp.name
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old_home is None:
            os.environ.pop("SWARM_HOME", None)
        else:
            os.environ["SWARM_HOME"] = self._old_home

    def _columns(self, conn, table):
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

    def _legacy_connect(self, version):
        path = os.path.join(self.tmp.name, "swarm.db")
        old = sqlite3.connect(path)
        old.executescript(
            V1_SCHEMA
            + (V2_EXTRA if version >= 2 else "")
            + (V3_EXTRA if version >= 3 else "")
            + (V4_EXTRA if version >= 4 else "")
        )
        old.execute(f"PRAGMA user_version={version}")
        old.execute(
            "INSERT INTO projects (name, repo_path, created_at) "
            "VALUES ('legacy', '/tmp/legacy', '2026-01-01T00:00:00Z')"
        )
        old.commit()
        old.close()
        conn = db.connect()
        self.addCleanup(conn.close)
        return conn

    def _assert_current(self, conn):
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.VERSION)
        for table, expected in V3_COLUMNS.items():
            self.assertLessEqual(expected, self._columns(conn, table), table)
        for table, expected in V4_COLUMNS.items():
            self.assertLessEqual(expected, self._columns(conn, table), table)
        for table, expected in V5_COLUMNS.items():
            self.assertLessEqual(expected, self._columns(conn, table), table)
        row = conn.execute("SELECT * FROM projects WHERE name = 'legacy'").fetchone()
        self.assertEqual(row["repo_path"], "/tmp/legacy")
        self.assertEqual(row["auto"], 0)
        self.assertIsNone(row["verify_allow"])
        self.assertEqual(row["implementer"], "omp")
        self.assertIsNone(row["rules"])
        self.assertEqual(row["archived"], 0)

    def test_journal_mode_is_wal(self):
        conn = db.connect()
        self.addCleanup(conn.close)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")

    def test_connect_twice_is_idempotent(self):
        first = db.connect()
        first.close()
        second = db.connect()
        self.addCleanup(second.close)
        rows = second.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        self.assertEqual(len(rows), 10)  # 9 tables plus sqlite_sequence
        self.assertEqual(second.execute("PRAGMA user_version").fetchone()[0], db.VERSION)

    def test_all_tables_present(self):
        conn = db.connect()
        self.addCleanup(conn.close)
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.assertEqual(TABLES - names, set())

    def test_worktree_tables_have_expected_columns(self):
        conn = db.connect()
        self.addCleanup(conn.close)
        for table, expected in (
            ("worktrees", WORKTREE_COLUMNS),
            ("branches", BRANCH_COLUMNS),
        ):
            columns = [
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            ]
            self.assertEqual(columns, expected)

    def test_worktrees_primary_key_is_project_and_path(self):
        conn = db.connect()
        self.addCleanup(conn.close)
        keys = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(worktrees)")
            if row["pk"]
        ]
        self.assertEqual(keys, ["project", "path"])

    def test_existing_v1_db_gets_new_tables(self):
        path = os.path.join(self.tmp.name, "swarm.db")
        old = sqlite3.connect(path)
        old.executescript(V1_SCHEMA)
        old.execute("PRAGMA user_version=1")
        old.commit()
        old.close()

        conn = db.connect()
        self.addCleanup(conn.close)
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.assertEqual(names & {"worktrees", "branches"}, {"worktrees", "branches"})
        self.assertEqual(len(names), 10)  # 9 tables plus sqlite_sequence
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.VERSION)

    def test_fresh_db_has_current_columns(self):
        conn = db.connect()
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.VERSION)
        for table, expected in V3_COLUMNS.items():
            self.assertLessEqual(expected, self._columns(conn, table), table)
        for table, expected in V4_COLUMNS.items():
            self.assertLessEqual(expected, self._columns(conn, table), table)
        for table, expected in V5_COLUMNS.items():
            self.assertLessEqual(expected, self._columns(conn, table), table)
        with conn:
            conn.execute(
                "INSERT INTO projects (name, repo_path, created_at) VALUES (?, ?, ?)",
                ("fresh", "/tmp/fresh", "2026-01-01T00:00:00Z"),
            )
        row = conn.execute("SELECT * FROM projects WHERE name = 'fresh'").fetchone()
        self.assertEqual(row["implementer"], "omp")
        self.assertIsNone(row["rules"])
        self.assertEqual(row["archived"], 0)

    def test_v1_db_migrates_to_current(self):
        self._assert_current(self._legacy_connect(1))

    def test_v2_db_migrates_to_current(self):
        self._assert_current(self._legacy_connect(2))

    def test_v3_db_migrates_to_current(self):
        self._assert_current(self._legacy_connect(3))

    def test_v4_db_migrates_to_current(self):
        conn = self._legacy_connect(4)
        self._assert_current(conn)
        with conn:
            conn.execute(
                "INSERT INTO plans (project, ref, title, created_at, updated_at) "
                "VALUES ('legacy', '0001', 't', '2026-01-01T00:00:00Z', "
                "'2026-01-01T00:00:00Z')"
            )
        row = conn.execute("SELECT * FROM plans WHERE ref = '0001'").fetchone()
        self.assertIsNone(row["window"])
        self.assertEqual(row["window_ok"], 0)
        self.assertEqual(row["measure_from"], 0)

    def test_migrated_db_matches_fresh_column_order(self):
        migrated = self._legacy_connect(1)
        fresh_home = os.path.join(self.tmp.name, "fresh")
        os.mkdir(fresh_home)
        old_home = os.environ["SWARM_HOME"]
        os.environ["SWARM_HOME"] = fresh_home
        try:
            fresh = db.connect()
        finally:
            os.environ["SWARM_HOME"] = old_home
        self.addCleanup(fresh.close)
        for table in {**V3_COLUMNS, **V4_COLUMNS, **V5_COLUMNS}:
            self.assertEqual(
                [row["name"] for row in migrated.execute(f"PRAGMA table_info({table})")],
                [row["name"] for row in fresh.execute(f"PRAGMA table_info({table})")],
            )

    def test_add_event_returns_increasing_ids(self):
        conn = db.connect()
        self.addCleanup(conn.close)
        first = db.add_event(conn, "note", "first")
        second = db.add_event(conn, "note", "second", project="p", plan_id=1)
        self.assertIsInstance(first, int)
        self.assertGreater(second, first)
        rows = conn.execute("SELECT id, ts, kind, text FROM events ORDER BY id").fetchall()
        self.assertEqual([r["id"] for r in rows], [first, second])
        self.assertEqual(rows[0]["kind"], "note")
        self.assertEqual(rows[1]["text"], "second")

    def test_schema_rejects_duplicate_plan_ref(self):
        conn = db.connect()
        self.addCleanup(conn.close)
        with conn:
            conn.execute(
                "INSERT INTO projects (name, repo_path, created_at) VALUES (?, ?, ?)",
                ("p", "/tmp/p", "2026-01-01T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO plans (project, ref, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("p", "1", "t", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    "INSERT INTO plans (project, ref, title, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("p", "1", "t", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                )


if __name__ == "__main__":
    unittest.main()
