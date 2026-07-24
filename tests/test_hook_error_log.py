"""log_hook_error: the learning loop degrades loudly, never fatally.

The local-store learning loop (recurrence injection, reinforcement, savings,
stats) is wrapped in broad excepts so a locked/corrupt local.db can never
crash the user's session. Previously those excepts were bare `pass` — silent.
Now they call log_hook_error, which must append a diagnosable line and never
raise (logging must not become the thing that breaks the session).
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import session_state


class LogHookErrorTests(unittest.TestCase):
    def test_appends_one_line_per_call(self):
        with tempfile.TemporaryDirectory() as d:
            logp = Path(d) / "hook-errors.log"
            with mock.patch.object(session_state, "HOOK_ERROR_LOG", logp):
                session_state.log_hook_error("record_trigger", ValueError("boom"))
                session_state.log_hook_error("book_savings", KeyError("k"))
                content = logp.read_text(encoding="utf-8")
        lines = content.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("[record_trigger]", lines[0])
        self.assertIn("ValueError: boom", lines[0])
        self.assertIn("[book_savings]", lines[1])

    def test_multiline_message_collapsed(self):
        with tempfile.TemporaryDirectory() as d:
            logp = Path(d) / "hook-errors.log"
            with mock.patch.object(session_state, "HOOK_ERROR_LOG", logp):
                session_state.log_hook_error("x", RuntimeError("a\nb\nc"))
                content = logp.read_text(encoding="utf-8")
        self.assertEqual(len(content.strip().splitlines()), 1)
        self.assertIn("a b c", content)

    def test_never_raises_when_path_unwritable(self):
        with tempfile.TemporaryDirectory() as d:
            # An ancestor is a regular file → mkdir(parents=True) fails.
            blocker = Path(d) / "blocker"
            blocker.write_text("x", encoding="utf-8")
            logp = blocker / "sub" / "hook-errors.log"
            with mock.patch.object(session_state, "HOOK_ERROR_LOG", logp):
                # Must swallow its own failure and return normally.
                session_state.log_hook_error("x", RuntimeError("y"))


if __name__ == "__main__":
    unittest.main()
