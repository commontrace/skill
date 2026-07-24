"""fail_then_succeed must be scored, not just detected.

Regression: post_tool_use.py emits a `fail_then_succeed` candidate
(error → change → bash success) and artifacts.py labels it, but
compute_importance had no scoring branch — the signal was inert and never
contributed to the contribution-threshold total or seeded proximity boosts.
"""

import unittest

from base import HookTestCase
from session_state import append_event
import stop


class FailThenSucceedScoring(HookTestCase):
    def test_scored_at_workaround_tier(self):
        sd = self.state_dir
        append_event(sd, "errors.jsonl", {"t": 100, "output_tail": "boom"})
        append_event(sd, "changes.jsonl", {"t": 150, "file": "a.py"})
        append_event(sd, "candidates.jsonl", {
            "pattern": "fail_then_succeed",
            "error_count": 2,
            "error_summary": "boom",
            "fix_files": ["a.py"],
            "verification": "ok",
            "t": 200,
        })
        total, top, ev = stop.compute_importance(sd)
        self.assertEqual(top, "fail_then_succeed")
        self.assertAlmostEqual(total, 1.5, places=5)
        self.assertEqual(ev.get("fix_files"), ["a.py"])
        self.assertEqual(ev.get("error_count"), 2)

    def test_absent_candidate_does_not_score(self):
        sd = self.state_dir
        append_event(sd, "errors.jsonl", {"t": 100})
        append_event(sd, "changes.jsonl", {"t": 150, "file": "a.py"})
        _, top, _ = stop.compute_importance(sd)
        self.assertNotEqual(top, "fail_then_succeed")

    def test_suppressed_when_error_resolution_present(self):
        """A full error_resolution supersedes the weaker fail_then_succeed.

        Compared against an identical session lacking the candidate: the
        fail_then_succeed candidate must add nothing extra when the stronger
        error_resolution signal already fired (avoids double-counting the same
        error → fix → success story).
        """
        def seed(sd, with_candidate):
            append_event(sd, "errors.jsonl", {"t": 100})
            append_event(sd, "changes.jsonl", {"t": 150, "file": "a.py"})
            append_event(sd, "resolutions.jsonl", {"t": 200})
            if with_candidate:
                append_event(sd, "candidates.jsonl", {
                    "pattern": "fail_then_succeed", "error_count": 1,
                    "fix_files": ["a.py"], "t": 210,
                })

        base_dir = self.tmp_path / "s-base"
        base_dir.mkdir()
        seed(base_dir, with_candidate=False)
        base_total, base_top, _ = stop.compute_importance(base_dir)

        cand_dir = self.tmp_path / "s-cand"
        cand_dir.mkdir()
        seed(cand_dir, with_candidate=True)
        cand_total, cand_top, _ = stop.compute_importance(cand_dir)

        self.assertEqual(base_top, "error_resolution")
        self.assertEqual(cand_top, "error_resolution")
        self.assertAlmostEqual(cand_total, base_total, places=5)


if __name__ == "__main__":
    unittest.main()
