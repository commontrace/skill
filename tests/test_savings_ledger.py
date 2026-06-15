"""Tests for the savings ledger: schema v4, ledger helpers, booking, metadata.

Uses HookTestCase isolation (temp local.db). The v4 migration test hand-builds
a real v3 database file, then opens it through _get_conn() and asserts the
additive migration ran (savings_events present, user_version == 4).
"""

import sqlite3
import time
import unittest

from base import HookTestCase, append_event  # noqa: F401

import local_store


DAY = 86400.0


def _make_v3_db(path):
    """Build a real v3 database file (5 tables, user_version=3, no savings)."""
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
            created_at REAL NOT NULL, last_seen_at REAL NOT NULL,
            seen_count INTEGER DEFAULT 1,
            resolved_at REAL, fix_command TEXT, fix_files TEXT, trace_id TEXT,
            UNIQUE(project_id, signature)
        );
    """)
    conn.execute(
        "INSERT INTO projects (path, first_seen_at, last_seen_at) "
        "VALUES ('/p', ?, ?)", (time.time(), time.time()))
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()


class TestSavingsSchema(HookTestCase):
    def test_current_schema_version_is_four(self):
        self.assertEqual(local_store.CURRENT_SCHEMA_VERSION, 4)

    def test_fresh_db_has_savings_events_table(self):
        conn = self.get_conn()
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("savings_events", tables)
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_savings_events_columns(self):
        conn = self.get_conn()
        cols = {row[1] for row in
                conn.execute("PRAGMA table_info(savings_events)")}
        self.assertLessEqual(
            {"id", "project_id", "session_id", "event_type", "minutes_saved",
             "tokens_saved", "source_label", "trace_id", "signature",
             "created_at"}, cols)

    def test_v3_db_migrates_to_v4_additively(self):
        _make_v3_db(local_store.DB_PATH)
        conn = self.get_conn()  # opening triggers _apply_migrations
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("savings_events", tables)
        # Pre-existing v3 data survived (additive migration, no rebuild).
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_v4_migration_is_idempotent(self):
        _make_v3_db(local_store.DB_PATH)
        self.get_conn().close()
        conn = self.get_conn()  # second open must not break
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("savings_events", tables)


class TestSavingsPrune(HookTestCase):
    def test_prune_stale_cache_deletes_old_savings_events(self):
        conn = self.get_conn()
        old_ts = time.time() - 91 * DAY
        conn.execute(
            "INSERT INTO savings_events (session_id, event_type, created_at) "
            "VALUES (?, ?, ?)",
            ("test-session-prune", "error_fix", old_ts),
        )
        conn.commit()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM savings_events").fetchone()[0], 1)
        local_store.prune_stale_cache(conn)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM savings_events").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
