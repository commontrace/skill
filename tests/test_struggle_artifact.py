"""Struggle-grid share line emitted by the Stop hook on contribution."""

from base import HookTestCase, append_event

import artifacts
import stop


class TestStruggleArtifact(HookTestCase):
    def _candidate(self):
        return {"metadata_json": {"time_to_resolution_minutes": 47,
                                  "error_count": 8}}

    def test_writes_artifact_and_returns_line(self):
        t0 = 1_750_000_000.0
        for i in range(3):
            append_event(self.state_dir, "errors.jsonl", {"t": t0 + i * 60})
        append_event(self.state_dir, "changes.jsonl", {"t": t0 + 300})
        line = stop._struggle_artifact(self._candidate(), self.state_dir,
                                       "abc123")
        self.assertIn("47min · 8 errors · solved", line)
        self.assertIn("https://commontrace.org/t/abc123", line)
        saved = (artifacts.ARTIFACTS_DIR / "last-struggle.txt").read_text(
            encoding="utf-8")
        self.assertEqual(saved, line + "\n")

    def test_no_trace_id_omits_url(self):
        line = stop._struggle_artifact(self._candidate(), self.state_dir)
        self.assertNotIn("commontrace.org", line)

    def test_never_raises_on_bad_candidate(self):
        self.assertIsNone(stop._struggle_artifact(None, self.state_dir))
