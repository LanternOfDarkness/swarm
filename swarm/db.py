import sqlite3

from swarm import util

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  name TEXT PRIMARY KEY,
  repo_path TEXT NOT NULL,
  plans_dir TEXT NOT NULL DEFAULT 'docs/plans',
  gates_file TEXT,
  verify_cmd TEXT,
  integration_branch TEXT NOT NULL DEFAULT 'main',
  created_at TEXT NOT NULL,
  auto INTEGER NOT NULL DEFAULT 0,
  verify_allow TEXT,
  implementer TEXT NOT NULL DEFAULT 'omp',
  rules TEXT,
  archived INTEGER NOT NULL DEFAULT 0
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
  omp_session TEXT,
  review_rounds INTEGER NOT NULL DEFAULT 0,
  last_review_path TEXT,
  operator_note TEXT,
  merge_attempts INTEGER NOT NULL DEFAULT 0,
  ib_sha TEXT,
  window TEXT,
  window_ok INTEGER NOT NULL DEFAULT 0,
  measure_from INTEGER NOT NULL DEFAULT 0,
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
  ended_at TEXT,
  pid INTEGER,
  exit_code INTEGER,
  report_path TEXT,
  timeout_s INTEGER,
  purpose TEXT,
  step TEXT
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
  answered_at TEXT,
  action TEXT,
  handled_at TEXT
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


VERSION = 5

NEW_COLUMNS = {
    "projects": (
        ("auto", "INTEGER NOT NULL DEFAULT 0"),
        ("verify_allow", "TEXT"),
        ("implementer", "TEXT NOT NULL DEFAULT 'omp'"),
        ("rules", "TEXT"),
        ("archived", "INTEGER NOT NULL DEFAULT 0"),
    ),
    "plans": (
        ("omp_session", "TEXT"),
        ("review_rounds", "INTEGER NOT NULL DEFAULT 0"),
        ("last_review_path", "TEXT"),
        ("operator_note", "TEXT"),
        ("merge_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("ib_sha", "TEXT"),
        ("window", "TEXT"),
        ("window_ok", "INTEGER NOT NULL DEFAULT 0"),
        ("measure_from", "INTEGER NOT NULL DEFAULT 0"),
    ),
    "runs": (
        ("pid", "INTEGER"),
        ("exit_code", "INTEGER"),
        ("report_path", "TEXT"),
        ("timeout_s", "INTEGER"),
        ("purpose", "TEXT"),
        ("step", "TEXT"),
    ),
    "asks": (
        ("action", "TEXT"),
        ("handled_at", "TEXT"),
    ),
}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(util.home() / "swarm.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.execute(f"PRAGMA user_version={VERSION}")
    conn.commit()
    return conn


def migrate(conn) -> None:
    """Add columns introduced after the table was created; no-op for fresh DBs."""
    for table, columns in NEW_COLUMNS.items():
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def add_event(conn, kind, text, project=None, plan_id=None) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO events (ts, project, plan_id, kind, text) VALUES (?, ?, ?, ?, ?)",
            (util.now(), project, plan_id, kind, text),
        )
        return cur.lastrowid
