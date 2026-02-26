"""Persistent local store for cross-session memory.

SQLite database at ~/.commontrace/local.db that survives session exits.
Tracks projects, sessions, entities (languages, frameworks, error patterns),
and events (errors, resolutions, changes, research, contributions).

All functions open/close their own connection (fast, no pooling needed).
All operations are wrapped in try/except — callers should fall back to
JSONL behavior on any failure.
"""

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path.home() / ".commontrace" / "local.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    primary_language TEXT,
    primary_framework TEXT,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    session_count INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    started_at REAL NOT NULL,
    ended_at REAL,
    error_count INTEGER DEFAULT 0,
    resolution_count INTEGER DEFAULT 0,
    contribution_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id),
    entity_type TEXT NOT NULL,
    entity_value TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    occurrence_count INTEGER DEFAULT 1,
    UNIQUE(project_id, entity_type, entity_value)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    event_type TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def _get_conn() -> sqlite3.Connection:
    """Open SQLite connection with self-migrating schema."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=3)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_project(conn: sqlite3.Connection, cwd: str,
                   language: str | None = None,
                   framework: str | None = None) -> int:
    """Register or update a project. Returns project_id."""
    now = time.time()
    row = conn.execute(
        "SELECT id, session_count FROM projects WHERE path = ?", (cwd,)
    ).fetchone()

    if row:
        updates = {"last_seen_at": now, "session_count": row["session_count"] + 1}
        if language:
            updates["primary_language"] = language
        if framework:
            updates["primary_framework"] = framework
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE projects SET {set_clause} WHERE id = ?",
            (*updates.values(), row["id"]),
        )
        conn.commit()
        return row["id"]

    conn.execute(
        "INSERT INTO projects (path, primary_language, primary_framework, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
        (cwd, language, framework, now, now),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def start_session(conn: sqlite3.Connection, session_id: str,
                  project_id: int) -> None:
    """Record a new session start."""
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, project_id, started_at) "
        "VALUES (?, ?, ?)",
        (session_id, project_id, time.time()),
    )
    conn.commit()


def end_session(conn: sqlite3.Connection, session_id: str,
                stats: dict) -> None:
    """Update session with final stats."""
    conn.execute(
        "UPDATE sessions SET ended_at = ?, error_count = ?, "
        "resolution_count = ?, contribution_count = ? WHERE id = ?",
        (
            time.time(),
            stats.get("error_count", 0),
            stats.get("resolution_count", 0),
            stats.get("contribution_count", 0),
            session_id,
        ),
    )
    conn.commit()


def record_entity(conn: sqlite3.Connection, project_id: int,
                  entity_type: str, entity_value: str) -> None:
    """Upsert an entity (language, framework, error_pattern, domain)."""
    now = time.time()
    existing = conn.execute(
        "SELECT id, occurrence_count FROM entities "
        "WHERE project_id = ? AND entity_type = ? AND entity_value = ?",
        (project_id, entity_type, entity_value),
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE entities SET last_seen_at = ?, occurrence_count = ? "
            "WHERE id = ?",
            (now, existing["occurrence_count"] + 1, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO entities (project_id, entity_type, entity_value, "
            "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
            (project_id, entity_type, entity_value, now, now),
        )
    conn.commit()


def migrate_jsonl_events(conn: sqlite3.Connection, session_id: str,
                         state_dir: Path) -> int:
    """Bulk-import JSONL state files into events table. Returns count."""
    # Check if already migrated (idempotent)
    existing = conn.execute(
        "SELECT COUNT(*) FROM events WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    if existing > 0:
        return 0

    file_type_map = {
        "errors.jsonl": "error",
        "resolutions.jsonl": "resolution",
        "changes.jsonl": "change",
        "research.jsonl": "research",
        "contributions.jsonl": "contribution",
    }

    count = 0
    for filename, event_type in file_type_map.items():
        path = state_dir / filename
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    created_at = entry.pop("t", time.time())
                    conn.execute(
                        "INSERT INTO events (session_id, event_type, data_json, "
                        "created_at) VALUES (?, ?, ?, ?)",
                        (session_id, event_type, json.dumps(entry), created_at),
                    )
                    count += 1
                except (json.JSONDecodeError, sqlite3.Error):
                    continue
        except OSError:
            continue

    conn.commit()
    return count


def get_project_context(conn: sqlite3.Connection, cwd: str) -> dict | None:
    """Build a context fingerprint from accumulated project history."""
    row = conn.execute(
        "SELECT id, primary_language, primary_framework FROM projects "
        "WHERE path = ?", (cwd,)
    ).fetchone()
    if not row:
        return None

    project_id = row["id"]
    context: dict = {}

    if row["primary_language"]:
        context["language"] = row["primary_language"]
    if row["primary_framework"]:
        context["framework"] = row["primary_framework"]

    # Enrich with entity history
    entities = conn.execute(
        "SELECT entity_type, entity_value, occurrence_count FROM entities "
        "WHERE project_id = ? ORDER BY occurrence_count DESC",
        (project_id,),
    ).fetchall()

    for e in entities:
        etype = e["entity_type"]
        if etype == "language" and "language" not in context:
            context["language"] = e["entity_value"]
        elif etype == "framework" and "framework" not in context:
            context["framework"] = e["entity_value"]
        elif etype == "domain":
            context.setdefault("domains", [])
            if len(context["domains"]) < 5:
                context["domains"].append(e["entity_value"])

    # Session history stats
    session_count = conn.execute(
        "SELECT session_count FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if session_count:
        context["session_count"] = session_count["session_count"]

    return context if context else None


def get_error_history(conn: sqlite3.Connection, project_id: int,
                      error_pattern: str) -> list[dict]:
    """Find previous events matching an error pattern (by substring in data_json)."""
    rows = conn.execute(
        "SELECT e.data_json, e.created_at, s.id as session_id "
        "FROM events e JOIN sessions s ON e.session_id = s.id "
        "WHERE s.project_id = ? AND e.event_type = 'error' "
        "AND e.data_json LIKE ? "
        "ORDER BY e.created_at DESC LIMIT 10",
        (project_id, f"%{error_pattern[:100]}%"),
    ).fetchall()

    return [
        {
            "data": json.loads(r["data_json"]),
            "created_at": r["created_at"],
            "session_id": r["session_id"],
        }
        for r in rows
    ]


def get_known_languages(conn: sqlite3.Connection,
                        project_id: int) -> set[str]:
    """Return all languages seen in this project."""
    rows = conn.execute(
        "SELECT entity_value FROM entities "
        "WHERE project_id = ? AND entity_type = 'language'",
        (project_id,),
    ).fetchall()
    return {r["entity_value"] for r in rows}
