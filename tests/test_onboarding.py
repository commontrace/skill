"""Zero-decision onboarding: auto-provisioning, MCP wiring, first-run notices."""

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from unittest import mock

from base import HookTestCase

import session_start
import session_state


class OnboardingTestCase(HookTestCase):
    """Adds session_start config isolation on top of HookTestCase."""

    def setUp(self):
        super().setUp()
        for target, attr, value in [
            (session_start, "CONFIG_DIR", self.tmp_path),
            (session_start, "CONFIG_FILE", self.tmp_path / "config.json"),
            (session_start, "PENDING_DIR", self.tmp_path / "pending"),
            (session_start, "PING_MARKER", self.tmp_path / "last_ping_date"),
            (session_state, "STATE_ROOT", self.tmp_path / "state"),
        ]:
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class TestConfigureMcp(OnboardingTestCase):
    def test_embeds_raw_key_in_header(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(session_start.subprocess, "run", fake_run):
            ok = session_start.configure_mcp("ct_raw_key_123")

        self.assertTrue(ok)
        self.assertIn("x-api-key: ct_raw_key_123", captured["argv"])
        joined = " ".join(captured["argv"])
        self.assertNotIn("${COMMONTRACE_API_KEY}", joined)

    def test_missing_claude_cli_returns_false(self):
        with mock.patch.object(
                session_start.subprocess, "run",
                side_effect=FileNotFoundError("claude not found")):
            self.assertFalse(session_start.configure_mcp("k"))


def _provision_forbidden():
    raise AssertionError("provision_api_key must not be called")


class TestEnsureSetup(OnboardingTestCase):
    def test_first_run_auto_provisions_anonymous_key(self):
        mcp_calls = []
        with mock.patch.object(session_start, "provision_api_key",
                               return_value="ct_live_abc"), \
             mock.patch.object(session_start, "configure_mcp",
                               side_effect=lambda k: mcp_calls.append(k) or True), \
             mock.patch.object(session_start, "report_install"):
            key = session_start.ensure_setup()

        self.assertEqual(key, "ct_live_abc")
        self.assertEqual(mcp_calls, ["ct_live_abc"])
        saved = json.loads(
            session_start.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["api_key"], "ct_live_abc")
        self.assertTrue(saved["pending_first_run_notice"])
        mode = session_start.CONFIG_FILE.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_env_var_short_circuits_provisioning(self):
        os.environ["COMMONTRACE_API_KEY"] = "env_key"
        self.addCleanup(os.environ.pop, "COMMONTRACE_API_KEY", None)
        with mock.patch.object(session_start, "provision_api_key",
                               side_effect=_provision_forbidden):
            key = session_start.ensure_setup()
        self.assertEqual(key, "env_key")
        saved = json.loads(
            session_start.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["api_key"], "env_key")
        self.assertNotIn("pending_first_run_notice", saved)

    def test_stored_key_short_circuits_provisioning(self):
        session_start.save_config({"api_key": "stored_key"})
        with mock.patch.object(session_start, "provision_api_key",
                               side_effect=_provision_forbidden):
            self.assertEqual(session_start.ensure_setup(), "stored_key")

    def test_provision_failure_returns_none_and_leaves_no_key(self):
        with mock.patch.object(session_start, "provision_api_key",
                               return_value=None):
            self.assertIsNone(session_start.ensure_setup())
        if session_start.CONFIG_FILE.exists():
            saved = json.loads(
                session_start.CONFIG_FILE.read_text(encoding="utf-8"))
            self.assertNotIn("api_key", saved)


class TestSetupFailedNotice(OnboardingTestCase):
    def _run_main(self):
        out = io.StringIO()
        with mock.patch.object(session_start, "provision_api_key",
                               return_value=None), \
             mock.patch.object(sys, "stdin", io.StringIO("{}")), \
             redirect_stdout(out):
            session_start.main()
        return out.getvalue()

    def test_failure_emits_notice_once_then_silent(self):
        first = self._run_main()
        payload = json.loads(first)
        self.assertEqual(
            payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("CommonTrace setup could not complete", ctx)

        second = self._run_main()
        self.assertEqual(second, "")


class TestFirstRunNotice(OnboardingTestCase):
    def _project_dir(self):
        proj = self.tmp_path / "proj"
        (proj / ".git").mkdir(parents=True, exist_ok=True)
        (proj / "app.py").write_text("x = 1\n", encoding="utf-8")
        return proj

    def _run_main(self, cwd):
        stdin_data = json.dumps(
            {"cwd": str(cwd), "session_id": "s-onboard"})
        out = io.StringIO()
        with mock.patch.object(session_start, "maybe_ping"), \
             mock.patch.object(session_start, "search_commontrace",
                               return_value=[]), \
             mock.patch.object(sys, "stdin", io.StringIO(stdin_data)), \
             redirect_stdout(out):
            session_start.main()
        return out.getvalue()

    def test_notice_prepended_once_then_cleared(self):
        session_start.save_config(
            {"api_key": "k", "pending_first_run_notice": True})
        output = self._run_main(self._project_dir())
        payload = json.loads(output)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.startswith("CommonTrace first-run notice"))
        saved = json.loads(
            session_start.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertNotIn("pending_first_run_notice", saved)

        # Second session: notice must not repeat
        output2 = self._run_main(self._project_dir())
        ctx2 = json.loads(output2)["hookSpecificOutput"]["additionalContext"]
        self.assertFalse(ctx2.startswith("CommonTrace first-run notice"))

    def test_notice_deferred_until_a_session_emits_context(self):
        session_start.save_config(
            {"api_key": "k", "pending_first_run_notice": True})
        bare = self.tmp_path / "bare"
        bare.mkdir()
        # no .git → main returns before emitting anything
        output = self._run_main(bare)
        self.assertEqual(output, "")
        saved = json.loads(
            session_start.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertTrue(saved["pending_first_run_notice"])
