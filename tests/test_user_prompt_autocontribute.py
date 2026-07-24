"""Auto-contribute wiring in user_prompt.py (Task 1.2).

Drives the UserPromptSubmit hook's `main()` with a fake stdin event and a
temp session store, mirroring the repo's other hook tests. Asserts the
`additionalContext` contribution directive is emitted only when the feature
is enabled AND a fix-candidate exists AND the message structurally matches a
MOVE_ON phrase — and never by default.

The directive carries a stable sentinel (`_DIRECTIVE_SENTINEL`) so presence
is unambiguous even though the first-turn /recall nudge shares the channel.
"""

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import session_state
import user_prompt
from session_state import append_event

SENTINEL = user_prompt._DIRECTIVE_SENTINEL
MOVE_ON_MSG = "Looks fixed — let's move on to the next task in the plan."


class AutoContributeWiringTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

        # Isolate the session store and config from the real ~/.commontrace.
        state_root = self.tmp_path / "sessions"
        state_root.mkdir()
        self._patch(session_state, "STATE_ROOT", state_root)
        self._patch(user_prompt, "CONFIG_FILE", self.tmp_path / "no-config.json")

        # Clean env: feature off unless a test opts in.
        env_patcher = mock.patch.dict(os.environ)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        os.environ.pop("CT_AUTO_CONTRIBUTE_ON_MOVE_ON", None)

        self.session_id = "autocontrib-test"
        self.state_dir = state_root / self.session_id
        self.state_dir.mkdir()

    def _patch(self, target, attr, value):
        p = mock.patch.object(target, attr, value)
        p.start()
        self.addCleanup(p.stop)

    def _add_fix_candidate(self):
        append_event(self.state_dir, "candidates.jsonl", {
            "pattern": "test_fix_cycle",
            "test_failures": 1,
            "fix_files": ["app/payments.py"],
        })

    def _run(self, message):
        event = json.dumps({"session_id": self.session_id, "prompt": message})
        buf = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(event)), redirect_stdout(buf):
            user_prompt.main()
        return buf.getvalue()

    def _additional_context(self, out):
        if not out.strip():
            return ""
        obj = json.loads(out.strip().splitlines()[-1])
        return obj.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_enabled_with_candidate_and_moveon_emits_directive(self):
        os.environ["CT_AUTO_CONTRIBUTE_ON_MOVE_ON"] = "1"
        self._add_fix_candidate()
        ctx = self._additional_context(self._run(MOVE_ON_MSG))
        self.assertIn(SENTINEL, ctx)
        # Reuses the /trace background handoff — direct HTTP POST, no fabrication.
        self.assertIn("api/v1/traces", ctx)
        self.assertIn("background", ctx.lower())

    def test_default_off_no_directive(self):
        # No env, no config → feature off even with a candidate + move-on line.
        self._add_fix_candidate()
        ctx = self._additional_context(self._run(MOVE_ON_MSG))
        self.assertNotIn(SENTINEL, ctx)

    def test_moveon_without_candidate_no_directive(self):
        os.environ["CT_AUTO_CONTRIBUTE_ON_MOVE_ON"] = "1"
        # No candidate written this session.
        ctx = self._additional_context(self._run(MOVE_ON_MSG))
        self.assertNotIn(SENTINEL, ctx)

    def test_fires_once_then_flag_prevents_refire(self):
        os.environ["CT_AUTO_CONTRIBUTE_ON_MOVE_ON"] = "1"
        self._add_fix_candidate()
        first = self._additional_context(self._run(MOVE_ON_MSG))
        self.assertIn(SENTINEL, first)
        self.assertTrue((self.state_dir / "auto_contributed").exists())
        second = self._additional_context(self._run("on to the next task"))
        self.assertNotIn(SENTINEL, second)


if __name__ == "__main__":
    unittest.main()
