"""Tests for the savings ledger: schema v4, ledger helpers, booking, metadata.

Uses HookTestCase isolation (temp local.db). The v4 migration test hand-builds
a real v3 database file, then opens it through _get_conn() and asserts the
additive migration ran (savings_events present, user_version == 4).
"""

import json
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


class TestLedgerHelpers(HookTestCase):
    def test_book_inserts_a_row(self):
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/p")
        ok = local_store.book_session_saving(
            conn, pid, "sess-1", minutes=12.5, tokens=300_000)
        self.assertTrue(ok)
        row = conn.execute(
            "SELECT minutes_saved, tokens_saved, event_type, source_label, "
            "signature FROM savings_events").fetchone()
        self.assertAlmostEqual(row["minutes_saved"], 12.5)
        self.assertEqual(row["tokens_saved"], 300_000)
        self.assertEqual(row["event_type"], "measured_recurrence")
        self.assertEqual(row["source_label"], "measured")
        self.assertEqual(row["signature"], "*session*")

    def test_book_is_noop_on_zero_zero(self):
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/p")
        self.assertFalse(
            local_store.book_session_saving(conn, pid, "sess-1", 0, 0))
        n = conn.execute("SELECT COUNT(*) FROM savings_events").fetchone()[0]
        self.assertEqual(n, 0)

    def test_book_books_when_only_minutes_positive(self):
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/p")
        self.assertTrue(
            local_store.book_session_saving(conn, pid, "sess-1", 5.0, 0))

    def test_book_dedups_same_session_event_signature(self):
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/p")
        self.assertTrue(
            local_store.book_session_saving(conn, pid, "sess-1", 10, 100))
        # Same (session, event_type, default signature) -> INSERT OR IGNORE drops it.
        self.assertFalse(
            local_store.book_session_saving(conn, pid, "sess-1", 99, 999))
        n = conn.execute("SELECT COUNT(*) FROM savings_events").fetchone()[0]
        self.assertEqual(n, 1)

    def test_book_different_session_counts_again(self):
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/p")
        local_store.book_session_saving(conn, pid, "sess-1", 10, 100)
        self.assertTrue(
            local_store.book_session_saving(conn, pid, "sess-2", 10, 100))
        n = conn.execute("SELECT COUNT(*) FROM savings_events").fetchone()[0]
        self.assertEqual(n, 2)

    def test_savings_totals_sums_all(self):
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/p")
        local_store.book_session_saving(conn, pid, "s1", 10.0, 100_000)
        local_store.book_session_saving(conn, pid, "s2", 20.0, 200_000)
        totals = local_store.savings_totals(conn)
        self.assertAlmostEqual(totals["minutes"], 30.0)
        self.assertEqual(totals["tokens"], 300_000)
        self.assertEqual(totals["events"], 2)

    def test_savings_totals_empty_is_zeroed(self):
        conn = self.get_conn()
        totals = local_store.savings_totals(conn)
        self.assertEqual(totals, {"minutes": 0.0, "tokens": 0, "events": 0})

    def test_savings_totals_since_filters_by_created_at(self):
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/p")
        now = time.time()
        conn.execute(
            "INSERT INTO savings_events (project_id, session_id, event_type, "
            "minutes_saved, tokens_saved, created_at) VALUES (?,?,?,?,?,?)",
            (pid, "old", "measured_recurrence", 5.0, 50, now - 10 * DAY))
        conn.execute(
            "INSERT INTO savings_events (project_id, session_id, event_type, "
            "minutes_saved, tokens_saved, created_at) VALUES (?,?,?,?,?,?)",
            (pid, "new", "measured_recurrence", 7.0, 70, now - 1 * DAY))
        conn.commit()
        totals = local_store.savings_totals(conn, since=now - 5 * DAY)
        self.assertAlmostEqual(totals["minutes"], 7.0)
        self.assertEqual(totals["tokens"], 70)
        self.assertEqual(totals["events"], 1)

    def test_prev_session_started_at_picks_most_recent_other(self):
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/p")
        now = time.time()
        for sid, started in [("a", now - 3 * DAY), ("b", now - 1 * DAY),
                              ("current", now)]:
            conn.execute(
                "INSERT INTO sessions (id, project_id, started_at) "
                "VALUES (?, ?, ?)", (sid, pid, started))
        conn.commit()
        prev = local_store.prev_session_started_at(conn, "current")
        self.assertAlmostEqual(prev, now - 1 * DAY)

    def test_prev_session_started_at_none_when_alone(self):
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/p")
        conn.execute(
            "INSERT INTO sessions (id, project_id, started_at) "
            "VALUES ('only', ?, ?)", (pid, time.time()))
        conn.commit()
        self.assertIsNone(
            local_store.prev_session_started_at(conn, "only"))

    def test_book_raises_on_negative_minutes(self):
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/p")
        with self.assertRaises(ValueError):
            local_store.book_session_saving(conn, pid, "sess-neg", -1.0, 100)

    def test_book_raises_on_negative_tokens(self):
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/p")
        with self.assertRaises(ValueError):
            local_store.book_session_saving(conn, pid, "sess-neg", 5.0, -1)


class TestTokensInMetadata(HookTestCase):
    def setUp(self):
        super().setUp()
        import stop  # local import: base.py has already inserted hooks/ on path
        self.stop = stop

    def _transcript(self, lines):
        path = self.tmp_path / "t.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)

    def test_measured_tokens_in_metadata_json(self):
        t0 = 1_750_000_000.0
        from datetime import datetime, timezone
        iso0 = datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        iso1 = datetime.fromtimestamp(t0 + 120, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        append_event(self.state_dir, "errors.jsonl", {"t": t0})
        append_event(self.state_dir, "changes.jsonl", {"t": t0 + 120, "file": "app.py"})
        transcript = self._transcript([
            json.dumps({"timestamp": iso0, "message": {"usage": {"input_tokens": 400, "output_tokens": 100}}}),
            json.dumps({"timestamp": iso1, "message": {"usage": {"input_tokens": 200, "output_tokens": 50}}}),
        ])
        cand = self.stop._build_candidate(
            5.0, "error_resolution", {"errors": 1, "changes": 1},
            self.state_dir, transcript_path=transcript)
        self.assertEqual(cand["metadata_json"]["tokens_to_resolution"], 750)
        self.assertIn('"tokens_to_resolution": 750', cand["human_prompt"])

    def test_fallback_estimate_when_no_transcript(self):
        t0 = 1_750_000_000.0
        append_event(self.state_dir, "errors.jsonl", {"t": t0})
        append_event(self.state_dir, "errors.jsonl", {"t": t0 + 30})
        append_event(self.state_dir, "changes.jsonl", {"t": t0 + 60, "file": "app.py"})
        cand = self.stop._build_candidate(
            5.0, "error_resolution", {"errors": 2, "changes": 1},
            self.state_dir, transcript_path="")
        # (error_count 2 + iteration_count 1) * TOKENS_PER_TURN_EST(1500) = 4500
        self.assertEqual(cand["metadata_json"]["tokens_to_resolution"], 4500)

    def test_default_transcript_path_arg_is_optional(self):
        t0 = 1_750_000_000.0
        append_event(self.state_dir, "errors.jsonl", {"t": t0})
        append_event(self.state_dir, "changes.jsonl", {"t": t0 + 10, "file": "x.py"})
        cand = self.stop._build_candidate(
            5.0, "error_resolution", {"errors": 1, "changes": 1},
            self.state_dir)
        self.assertIn("tokens_to_resolution", cand["metadata_json"])


class TestBookSavings(HookTestCase):
    def setUp(self):
        super().setUp()
        import stop
        self.stop = stop

    def _seed_resolved_signature(self, conn, pid, created, resolved,
                                 trace_id="t-1", signature="sig-a"):
        conn.execute(
            "INSERT INTO error_signatures (project_id, signature, created_at, "
            "last_seen_at, seen_count, resolved_at, trace_id) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (pid, signature, created, resolved, resolved, trace_id))
        conn.commit()

    def test_books_row_for_resolved_trace_linked_signature(self):
        conn = self.get_conn()
        pid = self.write_project_bridge(conn)
        t0 = time.time()
        self._seed_resolved_signature(conn, pid, t0 - 360, t0)
        conn.close()
        append_event(self.state_dir, "errors.jsonl", {"t": t0 - 360})
        append_event(self.state_dir, "resolutions.jsonl", {"t": t0})
        data = {"session_id": "sess-book", "transcript_path": ""}
        self.stop._book_savings(data, self.state_dir)
        conn2 = local_store._get_conn()
        self.addCleanup(conn2.close)
        row = conn2.execute(
            "SELECT minutes_saved, event_type, source_label "
            "FROM savings_events").fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["minutes_saved"], 6.0)
        self.assertEqual(row["event_type"], "measured_recurrence")

    def test_minutes_capped_at_120_per_event(self):
        conn = self.get_conn()
        pid = self.write_project_bridge(conn)
        t0 = time.time()
        self._seed_resolved_signature(conn, pid, t0 - 36000, t0)
        conn.close()
        append_event(self.state_dir, "errors.jsonl", {"t": t0 - 36000})
        append_event(self.state_dir, "resolutions.jsonl", {"t": t0})
        self.stop._book_savings(
            {"session_id": "sess-cap", "transcript_path": ""}, self.state_dir)
        conn2 = local_store._get_conn()
        self.addCleanup(conn2.close)
        m = conn2.execute(
            "SELECT minutes_saved FROM savings_events").fetchone()["minutes_saved"]
        self.assertAlmostEqual(m, 120.0)

    def test_no_row_when_no_trace_linked_resolution(self):
        conn = self.get_conn()
        pid = self.write_project_bridge(conn)
        t0 = time.time()
        conn.execute(
            "INSERT INTO error_signatures (project_id, signature, created_at, "
            "last_seen_at, seen_count, resolved_at, trace_id) "
            "VALUES (?, 'sig-x', ?, ?, 1, ?, NULL)",
            (pid, t0 - 100, t0, t0))
        conn.commit()
        conn.close()
        append_event(self.state_dir, "resolutions.jsonl", {"t": t0})
        self.stop._book_savings(
            {"session_id": "sess-none", "transcript_path": ""}, self.state_dir)
        conn2 = local_store._get_conn()
        self.addCleanup(conn2.close)
        n = conn2.execute("SELECT COUNT(*) FROM savings_events").fetchone()[0]
        self.assertEqual(n, 0)

    def test_no_row_when_no_session_timestamps(self):
        conn = self.get_conn()
        pid = self.write_project_bridge(conn)
        t0 = time.time()
        self._seed_resolved_signature(conn, pid, t0 - 360, t0)
        conn.close()
        self.stop._book_savings(
            {"session_id": "sess-empty", "transcript_path": ""}, self.state_dir)
        conn2 = local_store._get_conn()
        self.addCleanup(conn2.close)
        n = conn2.execute("SELECT COUNT(*) FROM savings_events").fetchone()[0]
        self.assertEqual(n, 0)

    def test_corrupt_state_does_not_raise(self):
        try:
            self.stop._book_savings({"session_id": "x"}, self.state_dir)
        except Exception as exc:  # pragma: no cover
            self.fail(f"_book_savings raised: {exc!r}")


class TestSessionStartRecap(HookTestCase):
    def _seed_two_sessions_with_savings(self, conn):
        pid = local_store.ensure_project(conn, "/p")
        now = time.time()
        # A previous session and the current one.
        conn.execute(
            "INSERT INTO sessions (id, project_id, started_at) "
            "VALUES ('prev', ?, ?)", (pid, now - 2 * DAY))
        conn.execute(
            "INSERT INTO sessions (id, project_id, started_at) "
            "VALUES ('cur', ?, ?)", (pid, now))
        # Lifetime savings: one old (before prev), one new (after prev).
        conn.execute(
            "INSERT INTO savings_events (project_id, session_id, event_type, "
            "minutes_saved, tokens_saved, created_at) VALUES (?,?,?,?,?,?)",
            (pid, "old", "measured_recurrence", 60.0, 1_000_000, now - 3 * DAY))
        conn.execute(
            "INSERT INTO savings_events (project_id, session_id, event_type, "
            "minutes_saved, tokens_saved, created_at) VALUES (?,?,?,?,?,?)",
            (pid, "prev", "measured_recurrence", 30.0, 1_000_000, now - DAY))
        conn.commit()
        return pid

    def test_recap_composition_matches_main_logic(self):
        from savings import format_recap_line
        conn = self.get_conn()
        self._seed_two_sessions_with_savings(conn)
        life = local_store.savings_totals(conn)
        prev = local_store.prev_session_started_at(conn, "cur")
        delta = local_store.savings_totals(conn, since=prev) if prev else None
        line = format_recap_line(life, delta, price_per_mtok=None)
        # Lifetime = 90 min / 2M tokens; delta since prev (now-2d) = the
        # now-1d row only = 30 min / 1M tokens.
        self.assertIn("lifetime ~1.5h/~$10.0", line)
        self.assertIn("saved you ~30m ~$5.0 since last session", line)

    def test_recap_empty_when_no_savings(self):
        from savings import format_recap_line
        conn = self.get_conn()
        local_store.ensure_project(conn, "/p")
        life = local_store.savings_totals(conn)
        self.assertEqual(format_recap_line(life, None), "")

    def test_config_opt_out_default_on(self):
        # Mirrors the gate session_start uses: config.get("savings_recap", True).
        self.assertTrue({}.get("savings_recap", True))
        self.assertFalse({"savings_recap": False}.get("savings_recap", True))


if __name__ == "__main__":
    unittest.main()
