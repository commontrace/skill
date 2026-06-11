"""Error-time injection: recurrence of a resolved signature injects the fix."""

import unittest

from tests.base import HookTestCase, local_store, post_tool_use


class TestErrorRecurrence(HookTestCase):
    def test_first_error_injects_nothing(self):
        conn = self.get_conn()
        self.write_project_bridge(conn)
        out = post_tool_use._check_error_recurrence("sig-x", self.state_dir)
        self.assertIsNone(out)

    def test_unresolved_recurrence_injects_nothing(self):
        conn = self.get_conn()
        pid = self.write_project_bridge(conn)
        local_store.record_error_signature(conn, pid, "sig-x")
        out = post_tool_use._check_error_recurrence("sig-x", self.state_dir)
        self.assertIsNone(out)

    def test_resolved_recurrence_injects_fix(self):
        conn = self.get_conn()
        pid = self.write_project_bridge(conn)
        local_store.record_error_signature(conn, pid, "sig-x")
        local_store.record_resolution(
            conn, pid, "sig-x", fix_command="npm test",
            fix_files=["api.ts"], trace_id="t-9")
        out = post_tool_use._check_error_recurrence("sig-x", self.state_dir)
        self.assertIsNotNone(out)
        self.assertEqual(
            out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("npm test", ctx)
        self.assertIn("api.ts", ctx)
        self.assertIn("t-9", ctx)
        self.assertIn("local CommonTrace history", ctx)  # provenance always-on

    def test_injection_records_trigger_and_injection_marker(self):
        conn = self.get_conn()
        pid = self.write_project_bridge(conn)
        local_store.record_error_signature(conn, pid, "sig-x")
        local_store.record_resolution(conn, pid, "sig-x", fix_command="make")
        post_tool_use._check_error_recurrence("sig-x", self.state_dir)
        row = conn.execute(
            "SELECT trigger_name FROM trigger_feedback WHERE session_id = ?",
            (self.state_dir.name,)).fetchone()
        self.assertEqual(row["trigger_name"], "error_recurrence")
        from tests.base import read_events
        injected = read_events(self.state_dir, "recurrence_injected.jsonl")
        self.assertEqual(injected[0]["sig"], "sig-x")

    def test_cooldown_blocks_injection_but_still_records_occurrence(self):
        conn = self.get_conn()
        pid = self.write_project_bridge(conn)
        local_store.record_error_signature(conn, pid, "sig-x")
        local_store.record_resolution(conn, pid, "sig-x", fix_command="make")
        first = post_tool_use._check_error_recurrence("sig-x", self.state_dir)
        self.assertIsNotNone(first)
        second = post_tool_use._check_error_recurrence("sig-x", self.state_dir)
        self.assertIsNone(second)  # 60s cooldown active
        row = conn.execute(
            "SELECT seen_count FROM error_signatures WHERE signature = ?",
            ("sig-x",)).fetchone()
        self.assertEqual(row["seen_count"], 3)  # 1 manual + 2 checks

    def test_no_project_bridge_is_silent(self):
        out = post_tool_use._check_error_recurrence("sig-x", self.state_dir)
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
