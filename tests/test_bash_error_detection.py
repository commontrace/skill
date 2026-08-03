"""Issues 5 + 8: the core capture loop.

Issue 8: detect_bash_error treated ANY non-empty stderr as failure. But
jest/pytest/cargo/go/npm/git write NORMAL output to stderr on a GREEN run,
so a passing test was stored as an error (with a success string as its
signature) and resolutions.jsonl was never written — fail→succeed could
never fire.

Issue 5: agents pipe test output through tail/head; a pipeline's exit code
is the LAST command's, so `npx jest | tail` exits 0 even when jest failed.
detect_bash_error must scan the combined output for precise failure markers
when the exit code is 0/None.
"""

import sys
import unittest
from pathlib import Path

from tests.base import HookTestCase, append_event, post_tool_use
from session_state import read_events

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))


def _resp(exit_code=None, output="", stderr=""):
    d = {"output": output, "stderr": stderr}
    if exit_code is not None:
        d["exitCode"] = exit_code
    return {"tool_response": d}


class Issue8StderrOnGreenRun(unittest.TestCase):
    """Non-empty stderr on a clean exit is NOT a failure."""

    def test_passing_jest_with_stderr_is_not_error(self):
        # jest writes its report (PASS lines, summary) to stderr on success.
        stderr = ("PASS src/sum.test.js\n"
                  "  ✓ adds 1 + 2 (2 ms)\n\n"
                  "Test Suites: 1 passed, 1 total\n"
                  "Tests:       5 passed, 5 total\n")
        is_error, _out, _err = post_tool_use.detect_bash_error(
            _resp(exit_code=0, output="", stderr=stderr))
        self.assertFalse(is_error)

    def test_passing_cargo_reporting_zero_failed_is_not_error(self):
        # cargo prints "0 failed" on success — must not trip the failed marker.
        out = "test result: ok. 12 passed; 0 failed; 0 ignored"
        is_error, _o, _e = post_tool_use.detect_bash_error(
            _resp(exit_code=0, output=out, stderr=""))
        self.assertFalse(is_error)

    def test_git_progress_on_stderr_exit_zero_is_not_error(self):
        # git writes progress to stderr; exit 0 = success.
        is_error, _o, _e = post_tool_use.detect_bash_error(
            _resp(exit_code=0, output="",
                  stderr="Switched to branch 'main'\nYour branch is up to date."))
        self.assertFalse(is_error)

    def test_nonzero_exit_still_error(self):
        is_error, _o, err = post_tool_use.detect_bash_error(
            _resp(exit_code=1, output="", stderr="boom"))
        self.assertTrue(is_error)
        self.assertIn("boom", err)


class Issue5PipedFailureMarkers(unittest.TestCase):
    """Exit code 0/None but real failure text present => captured."""

    def test_node_exit1_piped_through_tail_is_captured(self):
        # `node -e '...; process.exit(1)' | tail -3` — pipe exit is tail's (0),
        # but node's error text survives.
        stderr = "Error: boom\n    at Object.<anonymous> (/x/a.js:2:7)"
        is_error, _o, err = post_tool_use.detect_bash_error(
            _resp(exit_code=0, output="", stderr=stderr))
        self.assertTrue(is_error)
        self.assertIn("boom", err)

    def test_jest_failed_summary_with_exit_zero_is_captured(self):
        stderr = ("FAIL src/sum.test.js\n"
                  "  ✗ adds (3 ms)\n\n"
                  "Tests:       1 failed, 4 passed, 5 total\n")
        is_error, _o, _e = post_tool_use.detect_bash_error(
            _resp(exit_code=0, output="", stderr=stderr))
        self.assertTrue(is_error)

    def test_pytest_failed_with_exit_zero_is_captured(self):
        out = ("FAILED tests/test_x.py::test_y - AssertionError\n"
               "=== 1 failed, 2 passed in 0.30s ===")
        is_error, _o, _e = post_tool_use.detect_bash_error(
            _resp(exit_code=0, output=out, stderr=""))
        self.assertTrue(is_error)

    def test_traceback_with_no_exit_code_is_captured(self):
        out = "Traceback (most recent call last):\n  File ...\nValueError: x"
        is_error, _o, _e = post_tool_use.detect_bash_error(
            _resp(output=out, stderr=""))  # no exitCode at all
        self.assertTrue(is_error)

    def test_string_response_piped_failure_is_captured(self):
        # tool_response as a plain string with a failure marker, no exit code.
        data = {"tool_response": "FAIL src/a.test.js\nTests: 2 failed"}
        is_error, _o, _e = post_tool_use.detect_bash_error(data)
        self.assertTrue(is_error)

    def test_mere_word_error_in_passing_run_not_captured(self):
        # A green run that only mentions the word "error" must not fire.
        out = "Checked error handling paths: all good\n5 passed"
        is_error, _o, _e = post_tool_use.detect_bash_error(
            _resp(exit_code=0, output=out, stderr=""))
        self.assertFalse(is_error)


class Issue8ResolutionRecorded(HookTestCase):
    """After the fix, a passing test run following an error records a
    resolution (the fail->succeed precondition that was impossible before)."""

    def test_passing_test_after_error_writes_resolution(self):
        conn = self.get_conn()
        self.write_project_bridge(conn)

        # 1) test fails (non-zero exit)
        post_tool_use.handle_bash({
            "tool_name": "Bash",
            "tool_input": {"command": "npx jest"},
            "tool_response": {"output": "", "stderr": "FAIL a.test.js\nTests: 1 failed",
                              "exitCode": 1},
        }, self.state_dir)
        # 2) agent edits code
        append_event(self.state_dir, "changes.jsonl",
                     {"tool": "Edit", "file": "/repo/a.js"})
        # 3) test passes — jest writes report to stderr, exit 0
        out = post_tool_use.handle_bash({
            "tool_name": "Bash",
            "tool_input": {"command": "npx jest"},
            "tool_response": {"output": "",
                              "stderr": "PASS a.test.js\nTests: 5 passed, 5 total",
                              "exitCode": 0},
        }, self.state_dir)

        resolutions = read_events(self.state_dir, "resolutions.jsonl")
        self.assertTrue(resolutions, "a passing test after an error must record a resolution")


class PostTurnRevisionDocsExclusion(HookTestCase):
    """Docs-only (*.md) edits across a user turn are not post_turn_revision."""

    def _edit(self, file_path, t):
        return {
            "tool_name": "Edit",
            "tool_input": {"file_path": file_path},
            "tool_response": {},
            "_t": t,
        }

    def test_markdown_edit_does_not_produce_post_turn_revision(self):
        # Pre-turn edit to the same markdown file
        append_event(self.state_dir, "changes.jsonl",
                     {"tool": "Edit", "file": "/repo/README.md", "t": 100})
        append_event(self.state_dir, "changes.jsonl",
                     {"tool": "Edit", "file": "/repo/notes.md", "t": 101})
        append_event(self.state_dir, "user_turns.jsonl", {"t": 200})
        # Post-turn edit to the same markdown file (current tool use)
        post_tool_use._detect_knowledge_candidates(
            "Edit",
            {"tool_name": "Edit", "tool_input": {"file_path": "/repo/README.md"}},
            self.state_dir)
        candidates = read_events(self.state_dir, "candidates.jsonl")
        patterns = {c.get("pattern") for c in candidates}
        self.assertNotIn("post_turn_revision", patterns)

    def test_code_edit_still_produces_post_turn_revision(self):
        append_event(self.state_dir, "changes.jsonl",
                     {"tool": "Edit", "file": "/repo/app.py", "t": 100})
        append_event(self.state_dir, "changes.jsonl",
                     {"tool": "Edit", "file": "/repo/other.py", "t": 101})
        append_event(self.state_dir, "user_turns.jsonl", {"t": 200})
        post_tool_use._detect_knowledge_candidates(
            "Edit",
            {"tool_name": "Edit", "tool_input": {"file_path": "/repo/app.py"}},
            self.state_dir)
        candidates = read_events(self.state_dir, "candidates.jsonl")
        patterns = {c.get("pattern") for c in candidates}
        self.assertIn("post_turn_revision", patterns)


if __name__ == "__main__":
    unittest.main()
