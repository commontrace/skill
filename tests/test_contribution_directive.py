"""Stop hook hands a contribution-worthy session to the agent via a
`decision: block` directive so the agent authors REAL content — instead of the
hook silently POSTing the mechanical journey template (a husk). This is the fix
for the auto-contribute jam and what makes a contribution visible in full-auto.
"""

import sys
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import stop  # noqa: E402

CAND = {
    "title": "Gandi DNS: use DNS-01 not HTTP-01",
    "top_pattern": "error_resolution",
    "suggested_tags": ["python", "error-resolution"],
    "evidence": {"files": ["/x/gandi_dns.py"]},
    "metadata_json": {
        "time_to_resolution_minutes": 40,
        "error_count": 3,
        "tokens_to_resolution": 520000,
    },
}


class ContributionDirectiveTests(unittest.TestCase):
    def test_auto_mode_contributes_without_asking(self):
        d = stop._contribution_directive(CAND, True, "/hooks")
        self.assertIsNotNone(d)
        self.assertIn("api/v1/traces", d)  # direct HTTP, no MCP dependency
        self.assertIn("banner mode=contributed", d)
        self.assertIn("without asking", d)
        self.assertNotIn("AskUserQuestion", d)
        self.assertIn("no narration", d)  # stays invisible
        # detection metadata rides along verbatim for somatic scoring
        self.assertIn("tokens_to_resolution", d)

    def test_manual_mode_suggests_then_asks(self):
        d = stop._contribution_directive(CAND, False, "/hooks")
        self.assertIsNotNone(d)
        self.assertIn("banner mode=suggest", d)
        self.assertIn("AskUserQuestion", d)
        self.assertIn("Always", d)  # the always-yes escalation
        self.assertIn("api/v1/traces", d)
        self.assertIn("no narration", d)

    def test_directive_demands_real_content_not_template(self):
        d = stop._contribution_directive(CAND, True, "/hooks")
        self.assertIn("ACTUALLY happened", d)
        # the husk template shape must never be handed back as content
        self.assertNotIn("When working with", d)

    def test_bad_candidate_returns_none_not_crash(self):
        self.assertIsNone(stop._contribution_directive(None, True, "/h"))

    def test_directive_never_claims_success_on_403(self):
        # Issue 7. Publishing is open to any registered account today, so a 403
        # should not happen — but if the server ever restricts writes again,
        # the failure mode that matters is the agent printing a receipt for a
        # contribution that never landed. The directive has to forbid that in
        # both auto and manual mode.
        for auto in (True, False):
            d = stop._contribution_directive(CAND, auto, "/hooks")
            self.assertIn("403", d)
            self.assertIn("nothing was", d.lower())
            self.assertIn("do NOT claim success", d)
            self.assertNotIn("invitations/redeem", d)


if __name__ == "__main__":
    unittest.main()
