"""End-to-end: error → fix → recurrence in a new session → injection →
assisted resolution recorded. Entirely offline (no API key resolvable)."""

import unittest

from tests.base import HookTestCase, append_event, post_tool_use


def _bash_event(command, exit_code, stdout="", stderr=""):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {
            "output": stdout, "stderr": stderr, "exitCode": exit_code,
        },
    }


class TestErrorFixRecurrenceLoop(HookTestCase):
    def test_full_loop(self):
        conn = self.get_conn()
        pid = self.write_project_bridge(conn)

        # ── Session 1: error → edit → same command succeeds ──
        out = post_tool_use.handle_bash(
            _bash_event("pytest tests/", 1,
                        stderr="ImportError: No module named foo"),
            self.state_dir)
        self.assertIsNone(out)  # first encounter: nothing known yet
        append_event(self.state_dir, "changes.jsonl",
                     {"tool": "Edit", "file": "/repo/foo.py"})
        out = post_tool_use.handle_bash(
            _bash_event("pytest tests/", 0, stdout="3 passed"),
            self.state_dir)
        self.assertIsNone(out)
        row = conn.execute(
            "SELECT resolved_at FROM error_signatures").fetchone()
        self.assertIsNotNone(row["resolved_at"])  # fix stored

        # ── Session 2 (fresh state dir, same project): error recurs ──
        s2 = self.tmp_path / "session-2"
        s2.mkdir()
        (s2 / "project_id").write_text(str(pid), encoding="utf-8")
        out = post_tool_use.handle_bash(
            _bash_event("pytest tests/", 1,
                        stderr="ImportError: No module named foo"),
            s2)
        self.assertIsNotNone(out)  # THE injection — the product moment
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("pytest tests/", ctx)
        self.assertIn("foo.py", ctx)
        self.assertIn("local CommonTrace history", ctx)

        # ── Agent applies the known fix; verification passes ──
        append_event(s2, "changes.jsonl",
                     {"tool": "Edit", "file": "/repo/foo.py"})
        post_tool_use.handle_bash(
            _bash_event("pytest tests/", 0, stdout="3 passed"), s2)
        row = conn.execute(
            "SELECT trace_consumed_id FROM trigger_feedback "
            "WHERE session_id = ?", (s2.name,)).fetchone()
        # Assisted resolution recorded — this is the north-star telemetry
        self.assertTrue(row["trace_consumed_id"].startswith("local:"))


if __name__ == "__main__":
    unittest.main()
