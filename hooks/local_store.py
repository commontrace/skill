"""Persistent local store for cross-session working memory.

SQLite database at ~/.commontrace/local.db (5-table working memory cache).
The remote PostgreSQL API is the source of truth. This store tracks only:
  - projects: identity and language/framework metadata
  - sessions: per-session stats and top pattern
  - trace_cache: pointers (id + title) for recently seen traces
  - trigger_feedback: which triggers led to trace consumption
  - error_signatures: error fingerprints for recurrence detection

All functions accept an open connection (callers call _get_conn()).
All write operations call conn.commit() immediately.
All operations are wrapped in try/except by callers — fall back to JSONL.
"""

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path.home() / ".commontrace" / "local.db"

CURRENT_SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    language TEXT,
    framework TEXT,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    session_count INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_projects_path ON projects(path);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    started_at REAL NOT NULL,
    ended_at REAL,
    error_count INTEGER DEFAULT 0,
    resolution_count INTEGER DEFAULT 0,
    contribution_count INTEGER DEFAULT 0,
    top_pattern TEXT,
    importance_score REAL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, started_at DESC);

CREATE TABLE IF NOT EXISTS trace_cache (
    trace_id TEXT NOT NULL,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'search',
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    use_count INTEGER DEFAULT 0,
    vote TEXT,
    PRIMARY KEY (trace_id, project_id)
);
CREATE INDEX IF NOT EXISTS idx_trace_cache_project ON trace_cache(project_id, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS trigger_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    trigger_name TEXT NOT NULL,
    triggered_at REAL NOT NULL,
    trace_consumed_id TEXT,
    consumed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_trigger_feedback_session ON trigger_feedback(session_id);

CREATE TABLE IF NOT EXISTS error_signatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    signature TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_error_sig_project ON error_signatures(project_id, created_at DESC);
"""


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply schema migrations based on PRAGMA user_version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= CURRENT_SCHEMA_VERSION:
        return
    if version < 2:
        _migrate_to_v2(conn)
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        conn.commit()


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Migrate from 10-table v1 schema to 5-table v2 schema.

    Steps:
    1. Rebuild projects (rename primary_language/primary_framework -> language/framework)
    2. Add top_pattern and importance_score columns to sessions (additive — safe)
    3. Rebuild error_signatures (drop session_id and raw_tail columns)
    4. Drop 6 obsolete tables
    """
    # Step 1: Rebuild projects table (rename columns)
    proj_cols = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
    if "primary_language" in proj_cols:
        conn.executescript("""
            BEGIN EXCLUSIVE;
            CREATE TABLE IF NOT EXISTS projects_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                language TEXT,
                framework TEXT,
                first_seen_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                session_count INTEGER DEFAULT 1
            );
            INSERT INTO projects_new
                SELECT id, path, primary_language, primary_framework,
                       first_seen_at, last_seen_at, session_count FROM projects;
            DROP TABLE projects;
            ALTER TABLE projects_new RENAME TO projects;
            COMMIT;
        """)

    # Step 2: Add columns to sessions (additive — safe with ALTER TABLE)
    for col_def in ["top_pattern TEXT", "importance_score REAL DEFAULT 0.0"]:
        try:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # Already exists

    # Step 3: Rebuild error_signatures (drop session_id and raw_tail)
    sig_cols = {row[1] for row in conn.execute("PRAGMA table_info(error_signatures)")}
    if "raw_tail" in sig_cols or "session_id" in sig_cols:
        conn.executescript("""
            BEGIN EXCLUSIVE;
            CREATE TABLE IF NOT EXISTS error_signatures_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                signature TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            INSERT INTO error_signatures_new (id, project_id, signature, created_at)
                SELECT id, project_id, signature, created_at
                FROM error_signatures;
            DROP TABLE error_signatures;
            ALTER TABLE error_signatures_new RENAME TO error_signatures;
            COMMIT;
        """)

    # Step 4: Drop obsolete tables (order: dependents before parents)
    for table in [
        "session_insights",
        "local_knowledge",
        "discovered_knowledge",
        "error_resolutions",
        "events",
        "entities",
    ]:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


def _get_conn() -> sqlite3.Connection:
    """Open the local SQLite database with WAL mode and migration gate.

    Backs up the database before any migration. Creates the schema (CREATE IF
    NOT EXISTS) on every connection — safe because all DDL is idempotent.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Check version and backup if migration is needed
    current_ver = 0
    if DB_PATH.exists():
        try:
            tmp = sqlite3.connect(str(DB_PATH))
            current_ver = tmp.execute("PRAGMA user_version").fetchone()[0]
            tmp.close()
        except Exception:
            pass
        if current_ver < CURRENT_SCHEMA_VERSION:
            import shutil
            try:
                shutil.copy2(str(DB_PATH), str(DB_PATH) + ".bak")
            except OSError:
                pass

    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    # Set WAL mode FIRST — before any DDL (Pitfall 7)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-8000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.row_factory = sqlite3.Row
    _apply_migrations(conn)
    conn.executescript(_SCHEMA)  # CREATE IF NOT EXISTS for all 5 tables
    return conn


# ---------------------------------------------------------------------------
# Project and session lifecycle
# ---------------------------------------------------------------------------

def ensure_project(conn: sqlite3.Connection, cwd: str,
                   language: str = None, framework: str = None) -> int:
    """Upsert project record and return project_id."""
    now = time.time()
    conn.execute(
        "INSERT INTO projects (path, language, framework, first_seen_at, last_seen_at, session_count) "
        "VALUES (?, ?, ?, ?, ?, 1) "
        "ON CONFLICT(path) DO UPDATE SET "
        "language = COALESCE(excluded.language, language), "
        "framework = COALESCE(excluded.framework, framework), "
        "last_seen_at = excluded.last_seen_at, "
        "session_count = session_count + 1",
        (cwd, language, framework, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM projects WHERE path = ?", (cwd,)).fetchone()
    return row["id"]


def start_session(conn: sqlite3.Connection, session_id: str, project_id: int) -> None:
    """Record session start."""
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, project_id, started_at) VALUES (?, ?, ?)",
        (session_id, project_id, time.time()),
    )
    conn.commit()


def end_session(conn: sqlite3.Connection, session_id: str, stats: dict,
                top_pattern: str = None, importance_score: float = 0.0) -> None:
    """Record session end with stats, top pattern, and importance score."""
    conn.execute(
        "UPDATE sessions SET ended_at = ?, error_count = ?, "
        "resolution_count = ?, contribution_count = ?, "
        "top_pattern = ?, importance_score = ? WHERE id = ?",
        (
            time.time(),
            stats.get("error_count", 0),
            stats.get("resolution_count", 0),
            stats.get("contribution_count", 0),
            top_pattern,
            importance_score,
            session_id,
        ),
    )
    conn.commit()


def get_project_context(conn: sqlite3.Connection, cwd: str) -> dict | None:
    """Return project context dict or None if project not seen before.

    Reads only from the projects table (no entities join).
    Returns: {project_id, session_count, language?, framework?}
    """
    row = conn.execute(
        "SELECT id, language, framework, session_count FROM projects WHERE path = ?",
        (cwd,),
    ).fetchone()
    if not row:
        return None
    ctx = {"project_id": row["id"], "session_count": row["session_count"]}
    if row["language"]:
        ctx["language"] = row["language"]
    if row["framework"]:
        ctx["framework"] = row["framework"]
    return ctx


# ---------------------------------------------------------------------------
# Error signatures
# ---------------------------------------------------------------------------

def record_error_signature(conn: sqlite3.Connection, project_id: int,
                           signature: str) -> None:
    """Record an error signature for recurrence detection."""
    conn.execute(
        "INSERT INTO error_signatures (project_id, signature, created_at) VALUES (?, ?, ?)",
        (project_id, signature, time.time()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Trigger feedback
# ---------------------------------------------------------------------------

def record_trigger(conn: sqlite3.Connection, session_id: str,
                   trigger_name: str) -> None:
    """Record that a trigger fired this session."""
    conn.execute(
        "INSERT INTO trigger_feedback (session_id, trigger_name, triggered_at) "
        "VALUES (?, ?, ?)",
        (session_id, trigger_name, time.time()),
    )
    conn.commit()


def record_trace_consumed(conn: sqlite3.Connection, session_id: str,
                          trace_id: str) -> None:
    """Mark that a trace was consumed after a trigger fired this session."""
    row = conn.execute(
        "SELECT id FROM trigger_feedback "
        "WHERE session_id = ? AND trace_consumed_id IS NULL "
        "ORDER BY triggered_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE trigger_feedback SET trace_consumed_id = ?, consumed_at = ? WHERE id = ?",
            (trace_id, time.time(), row["id"]),
        )
        conn.commit()


def get_trigger_effectiveness(conn: sqlite3.Connection,
                              project_id: int) -> dict:
    """Return trigger effectiveness stats for this project.

    Returns dict: {trigger_name: {fired: N, consumed: M, rate: 0.0-1.0}}
    """
    rows = conn.execute(
        "SELECT tf.trigger_name, "
        "COUNT(*) AS fired, "
        "SUM(CASE WHEN tf.trace_consumed_id IS NOT NULL THEN 1 ELSE 0 END) AS consumed "
        "FROM trigger_feedback tf "
        "JOIN sessions s ON s.id = tf.session_id "
        "WHERE s.project_id = ? "
        "GROUP BY tf.trigger_name",
        (project_id,),
    ).fetchall()
    result = {}
    for row in rows:
        fired = row["fired"]
        consumed = row["consumed"]
        result[row["trigger_name"]] = {
            "fired": fired,
            "consumed": consumed,
            "rate": round(consumed / fired, 2) if fired > 0 else 0.0,
        }
    return result


# ---------------------------------------------------------------------------
# Trace cache (pointer cache — id + title only, no content)
# ---------------------------------------------------------------------------

def cache_trace_pointer(conn: sqlite3.Connection, trace_id: str,
                        project_id: int | None, title: str,
                        source: str = "search") -> None:
    """Store a trace pointer (ID + title only). No content stored locally."""
    now = time.time()
    conn.execute(
        "INSERT INTO trace_cache (trace_id, project_id, title, source, "
        "first_seen_at, last_seen_at, use_count) VALUES (?, ?, ?, ?, ?, ?, 0) "
        "ON CONFLICT(trace_id, project_id) DO UPDATE SET "
        "last_seen_at = excluded.last_seen_at, title = excluded.title",
        (trace_id, project_id, title[:120], source, now, now),
    )
    conn.commit()


def mark_trace_used_v2(conn: sqlite3.Connection, trace_id: str,
                       project_id: int) -> None:
    """Increment use_count for a cached trace pointer."""
    conn.execute(
        "UPDATE trace_cache SET use_count = use_count + 1, last_seen_at = ? "
        "WHERE trace_id = ? AND project_id = ?",
        (time.time(), trace_id, project_id),
    )
    conn.commit()


def record_trace_vote_v2(conn: sqlite3.Connection, trace_id: str,
                         vote_type: str) -> None:
    """Record a vote on a cached trace ('up' or 'down')."""
    conn.execute(
        "UPDATE trace_cache SET vote = ? WHERE trace_id = ?",
        (vote_type, trace_id),
    )
    conn.commit()


def get_cached_traces(conn: sqlite3.Connection, project_id: int,
                      limit: int = 3) -> list:
    """Return recently used trace pointers for session-start recall.

    Only returns traces with use_count > 0 (actually used, not just cached).
    """
    rows = conn.execute(
        "SELECT trace_id, title, use_count FROM trace_cache "
        "WHERE project_id = ? AND use_count > 0 "
        "ORDER BY last_seen_at DESC LIMIT ?",
        (project_id, limit),
    ).fetchall()
    return [{"trace_id": r["trace_id"], "title": r["title"],
             "use_count": r["use_count"]} for r in rows]


def prune_stale_cache(conn: sqlite3.Connection) -> None:
    """Run TTL pruning at session exit. Never block session start.

    Retention policy:
    - sessions: 90 days after end
    - trace_cache (unused): 30 days after first seen
    - trace_cache (downvoted): 7 days after last seen
    - trigger_feedback: 60 days
    - error_signatures: 90 days
    """
    now = time.time()
    conn.execute(
        "DELETE FROM sessions WHERE ended_at IS NOT NULL AND ended_at < ?",
        (now - 90 * 86400,),
    )
    conn.execute(
        "DELETE FROM trace_cache WHERE use_count = 0 AND first_seen_at < ?",
        (now - 30 * 86400,),
    )
    conn.execute(
        "DELETE FROM trace_cache WHERE vote = 'down' AND last_seen_at < ?",
        (now - 7 * 86400,),
    )
    conn.execute(
        "DELETE FROM trigger_feedback WHERE triggered_at < ?",
        (now - 60 * 86400,),
    )
    conn.execute(
        "DELETE FROM error_signatures WHERE created_at < ?",
        (now - 90 * 86400,),
    )
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    conn.commit()
