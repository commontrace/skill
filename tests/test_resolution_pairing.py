"""Pairing a succeeding command back to the failed signature it resolves."""

import json
import unittest

from tests.base import (
    HookTestCase, append_event, local_store, post_tool_use, read_events,
)


class TestCommandHead(HookTestCase):
    def test_plain_command(self):
        self.assertEqual(post_tool_use._command_head("pytest tests/ -v"),
                         "pytest")

    def test_skips_env_assignment_prefix(self):
        self.assertEqual(post_tool_use._command_head("FOO=1 BAR=2 pytest -x"),
                         "pytest")

    def test_empty_command(self):
        self.assertEqual(post_tool_use._command_head(""), "")


class TestPairResolution(HookTestCase):
    def _seed_error(self, conn, sig="sig-x", command="pytest tests/"):
        pid = self.write_project_bridge(conn)
        local_store.record_error_signature(conn, pid, sig)
        append_event(self.state_dir, "errors.jsonl", {
            "source": "bash", "command": command, "sig": sig, "t": 100.0})
        return pid

    def test_pairing_stores_fix(self):
        conn = self.get_conn()
        self._seed_error(conn)
        append_event(self.state_dir, "changes.jsonl", {
            "tool": "Edit", "file": "/repo/src/api.py", "t": 150.0})
        post_tool_use._pair_resolution(
            self.state_dir, "pytest tests/ -v",
            read_events(self.state_dir, "errors.jsonl"))
        row = conn.execute(
            "SELECT fix_command, fix_files, resolved_at "
            "FROM error_signatures WHERE signature = 'sig-x'").fetchone()
        self.assertIsNotNone(row["resolved_at"])
        self.assertEqual(row["fix_command"], "pytest tests/ -v")
        self.assertEqual(json.loads(row["fix_files"]), ["api.py"])  # basename only

    def test_pairing_requires_same_command_head(self):
        conn = self.get_conn()
        self._seed_error(conn)
        post_tool_use._pair_resolution(
            self.state_dir, "ls -la",
            read_events(self.state_dir, "errors.jsonl"))
        row = conn.execute(
            "SELECT resolved_at FROM error_signatures "
            "WHERE signature = 'sig-x'").fetchone()
        self.assertIsNone(row["resolved_at"])

    def test_pairing_skips_tool_failure_entries(self):
        conn = self.get_conn()
        self.write_project_bridge(conn)
        append_event(self.state_dir, "errors.jsonl", {
            "source": "tool_failure", "tool": "Edit", "error": "x", "t": 90.0})
        # Must not raise and must not write anything
        post_tool_use._pair_resolution(
            self.state_dir, "pytest",
            read_events(self.state_dir, "errors.jsonl"))
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM error_signatures "
            "WHERE resolved_at IS NOT NULL").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_consumed_trace_is_attributed_to_fix(self):
        conn = self.get_conn()
        self._seed_error(conn)
        sid = self.state_dir.name
        local_store.record_trigger(conn, sid, "bash_error")
        local_store.record_trace_consumed(conn, sid, "trace-42")
        post_tool_use._pair_resolution(
            self.state_dir, "pytest tests/",
            read_events(self.state_dir, "errors.jsonl"))
        row = conn.execute(
            "SELECT trace_id FROM error_signatures "
            "WHERE signature = 'sig-x'").fetchone()
        self.assertEqual(row["trace_id"], "trace-42")

    def test_assisted_resolution_marks_trigger_consumed(self):
        conn = self.get_conn()
        self._seed_error(conn)
        sid = self.state_dir.name
        local_store.record_trigger(conn, sid, "error_recurrence")
        append_event(self.state_dir, "recurrence_injected.jsonl",
                     {"sig": "sig-x", "t": 101.0})
        post_tool_use._pair_resolution(
            self.state_dir, "pytest tests/",
            read_events(self.state_dir, "errors.jsonl"))
        row = conn.execute(
            "SELECT trace_consumed_id FROM trigger_feedback "
            "WHERE session_id = ?", (sid,)).fetchone()
        self.assertTrue(row["trace_consumed_id"].startswith("local:"))


if __name__ == "__main__":
    unittest.main()
