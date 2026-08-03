"""End-to-end consumption loop through the REAL hook entry points.

Every other test in this suite calls the handlers (or the recorders)
directly with a hand-built ``tool_response``. That is why they pass while
production reports ``traces_consumed: 0`` and ``resolutions_total: 0``.

This test feeds ``post_tool_use.main()`` on stdin — the actual hook
contract — using the payload shape Claude Code really sends for
PostToolUse:Bash::

    {"stdout": ..., "stderr": ..., "interrupted": false,
     "isImage": false, "noOutputExpected": false}

Note what is NOT there: no ``output`` key, no ``exitCode`` key.

The loop asserted here is the north-star metric:
trigger fires -> fix injected -> agent applies it -> verification command
succeeds -> consumption recorded -> stop.py's session counters report it.
"""

import contextlib
import io
import json
import sys
import unittest
from unittest import mock

from tests.base import HookTestCase, HOOKS_DIR, read_events  # noqa: F401

import post_tool_use  # noqa: E402
import session_state  # noqa: E402
import session_report  # noqa: E402
import stop  # noqa: E402


def bash_payload(session_id, command, stdout="", stderr=""):
    """The exact PostToolUse:Bash payload Claude Code emits.

    Captured from a live transcript (~/.claude/projects/*/*.jsonl):
    toolUseResult keys are stdout/stderr/interrupted/isImage/
    noOutputExpected. There is no exit code and no "output" key.
    """
    return {
        "session_id": session_id,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {
            "stdout": stdout,
            "stderr": stderr,
            "interrupted": False,
            "isImage": False,
            "noOutputExpected": False,
        },
    }


def edit_payload(session_id, file_path):
    return {
        "session_id": session_id,
        "tool_name": "Edit",
        "tool_input": {
            "file_path": file_path,
            "old_string": "import foo",
            "new_string": "import foo_bar as foo",
        },
        "tool_response": {"filePath": file_path, "userModified": False},
    }


class TestConsumptionEndToEnd(HookTestCase):
    def setUp(self):
        super().setUp()
        # Keep session state inside the temp dir — get_state_dir() reads the
        # module global at call time, so patching the module attr is enough.
        patcher = mock.patch.object(
            session_state, "STATE_ROOT", self.tmp_path / "sessions")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_hook(self, payload):
        """Invoke the hook exactly as Claude Code does: JSON on stdin."""
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            with contextlib.redirect_stdout(out):
                post_tool_use.main()
        raw = out.getvalue().strip()
        return json.loads(raw) if raw else None

    def state_dir_for(self, session_id, project_id=None):
        d = session_state.get_state_dir({"session_id": session_id})
        if project_id is not None:
            (d / "project_id").write_text(str(project_id), encoding="utf-8")
        return d

    def test_consumption_loop_closes_with_real_payloads(self):
        conn = self.get_conn()
        pid = local_store_ensure(conn)

        s1 = "sess-e2e-one"
        d1 = self.state_dir_for(s1, pid)

        # ── 1. A real command fails (stderr only — no exit code field) ──
        self.run_hook(bash_payload(
            s1, "pytest tests/", stderr="ImportError: No module named foo"))
        self.assertEqual(len(read_events(d1, "errors.jsonl")), 1,
                         "bash failure must be captured")

        # ── 2. Agent edits a file ──
        self.run_hook(edit_payload(s1, "/repo/foo.py"))

        # ── 3. The same command now succeeds (stdout only, empty stderr) ──
        self.run_hook(bash_payload(s1, "pytest tests/", stdout="3 passed"))

        self.assertEqual(
            len(read_events(d1, "resolutions.jsonl")), 1,
            "a succeeding command after a failure must be recorded as a "
            "resolution — this is what feeds resolutions_total")

        row = conn.execute(
            "SELECT resolved_at, fix_command FROM error_signatures").fetchone()
        self.assertIsNotNone(row, "error signature must exist")
        self.assertIsNotNone(
            row["resolved_at"],
            "the verified fix must be attached to the signature — without it "
            "the recurrence injection can never fire again")

        # ── 4. New session, same project: the error comes back ──
        s2 = "sess-e2e-two"
        d2 = self.state_dir_for(s2, pid)
        injected = self.run_hook(bash_payload(
            s2, "pytest tests/", stderr="ImportError: No module named foo"))
        self.assertIsNotNone(
            injected, "known fix must be injected on recurrence")
        ctx = injected["hookSpecificOutput"]["additionalContext"]
        self.assertIn("pytest tests/", ctx)
        self.assertIn("foo.py", ctx)

        # ── 5. Agent applies the injected fix and re-verifies ──
        self.run_hook(edit_payload(s2, "/repo/foo.py"))
        self.run_hook(bash_payload(s2, "pytest tests/", stdout="3 passed"))

        consumed = conn.execute(
            "SELECT trace_consumed_id FROM trigger_feedback "
            "WHERE session_id = ? AND trace_consumed_id IS NOT NULL",
            (s2,)).fetchone()
        self.assertIsNotNone(
            consumed,
            "assisted resolution must mark the trigger as consumed — this is "
            "the consumption_rate the analytics endpoint reports")

        # ── 6. What stop.py actually POSTs to /api/v1/telemetry/triggers ──
        counters = session_report._session_counters(conn, d2, pid)
        self.assertGreater(counters["searches_fired"], 0)
        self.assertGreater(counters["traces_consumed"], 0,
                           "traces_consumed is the flat-zero product metric")
        self.assertGreater(counters["resolutions_total"], 0)
        self.assertGreater(counters["resolutions_assisted"], 0)


def local_store_ensure(conn):
    import local_store
    return local_store.ensure_project(conn, "/test-project")


if __name__ == "__main__":
    unittest.main()
