"""Issue 2: tool-failure events must be redacted before they land in
errors.jsonl, which feeds the contribution payload.

Root cause: post_tool_failure.py wrote error[:500] and str(tool_input)[:200]
RAW (it never imported redact), and stop.py reads errors.jsonl into the
journey/context that is transmitted. Fix = redact at the write site (before
truncating) plus a belt-and-braces choke point in stop._build_journey_context.
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import post_tool_failure  # noqa: E402
import session_state  # noqa: E402
import stop  # noqa: E402
from session_state import append_event, read_events  # noqa: E402


class ToolFailureWriteRedactionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        p = mock.patch.object(session_state, "STATE_ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, data):
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(data))):
            post_tool_failure.main()
        sd = self.root / data["session_id"]
        return read_events(sd, "errors.jsonl")

    def test_secret_in_error_and_input_is_redacted(self):
        events = self._run({
            "session_id": "sess-secret",
            "tool_name": "Bash",
            "error": 'curl -H "x-api-key: sk_live_ABCDEF1234567890" -> 401',
            "tool_input": {
                "command": 'curl -H "Authorization: Bearer sk_live_TOPSECRETTOKEN12345"',
            },
        })
        self.assertEqual(len(events), 1)
        blob = json.dumps(events[0])
        self.assertNotIn("sk_live_ABCDEF1234567890", blob)
        self.assertNotIn("sk_live_TOPSECRETTOKEN12345", blob)
        self.assertIn("[REDACTED]", events[0]["error"])
        self.assertIn("[REDACTED]", events[0]["input_summary"])

    def test_harness_noise_stripped_from_tool_failure_error(self):
        events = self._run({
            "session_id": "sess-noise",
            "tool_name": "Edit",
            "error": "Shell cwd was reset to /home/USER/proj\nRealError: boom",
            "tool_input": {"file_path": "/x/app.py"},
        })
        self.assertEqual(len(events), 1)
        self.assertNotIn("Shell cwd was reset", events[0]["error"])
        self.assertNotIn("/home/USER/proj", events[0]["error"])
        self.assertIn("boom", events[0]["error"])


class JourneyContextChokePointTests(unittest.TestCase):
    """Belt-and-braces: even if something reaches errors.jsonl unscrubbed,
    the stop.py contribution boundary must redact + strip before transmit."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sd = Path(self._tmp.name) / "journey-sess"
        self.sd.mkdir(parents=True)

    def test_journey_context_redacts_and_strips_raw_error(self):
        # Simulate an entry that bypassed write-time redaction.
        append_event(self.sd, "errors.jsonl", {
            "output_tail": ("token: sk_live_RAWLEAK1234567890 leaked\n"
                            "Shell cwd was reset to /home/USER/x"),
        })
        journey = stop._build_journey_context(self.sd)
        msgs = journey.get("error_messages", [])
        self.assertTrue(msgs)
        joined = " ".join(msgs)
        self.assertNotIn("sk_live_RAWLEAK1234567890", joined)
        self.assertNotIn("Shell cwd was reset", joined)
        self.assertIn("[REDACTED]", joined)

    def test_journey_context_reads_tool_failure_error_key(self):
        append_event(self.sd, "errors.jsonl", {
            "source": "tool_failure",
            "error": "password: hunter2secretlongvalue in the config",
        })
        journey = stop._build_journey_context(self.sd)
        joined = " ".join(journey.get("error_messages", []))
        self.assertNotIn("hunter2secretlongvalue", joined)
        self.assertIn("[REDACTED]", joined)


if __name__ == "__main__":
    unittest.main()
