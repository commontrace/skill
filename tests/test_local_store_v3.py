"""v3 schema: deduplicated error_signatures with resolution payload."""

import json
import sqlite3
import time
import unittest

from tests.base import HookTestCase, local_store

def _make_v2_db(path):
    """Build a real v2 database file with duplicate signature rows."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            language TEXT, framework TEXT,
            first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL,
            session_count INTEGER DEFAULT 1
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            started_at REAL NOT NULL, ended_at REAL,
            error_count INTEGER DEFAULT 0, resolution_count INTEGER DEFAULT 0,
            contribution_count INTEGER DEFAULT 0,
            top_pattern TEXT, importance_score REAL DEFAULT 0.0
        );
        CREATE TABLE trace_cache (
            trace_id TEXT NOT NULL,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'search',
            first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL,
            use_count INTEGER DEFAULT 0, vote TEXT,
            PRIMARY KEY (trace_id, project_id)
        );
        CREATE TABLE trigger_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, trigger_name TEXT NOT NULL,
            triggered_at REAL NOT NULL,
            trace_consumed_id TEXT, consumed_at REAL
        );
        CREATE TABLE error_signatures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            signature TEXT NOT NULL,
            created_at REAL NOT NULL
        );
    """)
    now = time.time()
    conn.execute(
        "INSERT INTO projects (path, first_seen_at, last_seen_at) "
        "VALUES ('/p', ?, ?)", (now, now))
    for i in range(3):
        conn.execute(
            "INSERT INTO error_signatures (project_id, signature, created_at) "
            "VALUES (1, 'sig-a', ?)", (now + i,))
    conn.execute(
        "INSERT INTO error_signatures (project_id, signature, created_at) "
        "VALUES (1, 'sig-b', ?)", (now,))
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

class TestV3Migration(HookTestCase):
    def test_v2_db_dedupes_into_seen_counts(self):
        _make_v2_db(local_store.DB_PATH)
        conn = self.get_conn()
        rows = conn.execute(
            "SELECT signature, seen_count FROM error_signatures "
            "ORDER BY signature").fetchall()
        self.assertEqual(
            [(r["signature"], r["seen_count"]) for r in rows],
            [("sig-a", 3), ("sig-b", 1)])
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_fresh_db_has_v3_columns(self):
        conn = self.get_conn()
        cols = {row[1] for row in
                conn.execute("PRAGMA table_info(error_signatures)")}
        self.assertLessEqual(
            {"seen_count", "last_seen_at", "resolved_at",
             "fix_command", "fix_files", "trace_id"}, cols)

    def test_migration_is_idempotent(self):
        _make_v2_db(local_store.DB_PATH)
        self.get_conn().close()
        conn = self.get_conn()  # second open must not break or re-migrate
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM error_signatures").fetchone()
        self.assertEqual(rows["n"], 2)

if __name__ == "__main__":
    unittest.main()
