"""Zero-decision onboarding: auto-provisioning, MCP wiring, first-run notices."""

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
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


class TestSaveConfig(OnboardingTestCase):
    def test_no_tmp_residue_after_save(self):
        session_start.save_config({"api_key": "k"})
        tmp_files = list(self.tmp_path.glob("tmp*"))
        self.assertEqual(tmp_files, [], "temp file left behind after save_config")

    def test_round_trip(self):
        data = {"api_key": "rt_key", "anonymous": True, "extra": 42}
        session_start.save_config(data)
        loaded = session_start.load_config()
        self.assertEqual(loaded, data)

    def test_file_permissions_0o600(self):
        session_start.save_config({"api_key": "perm_key"})
        mode = session_start.CONFIG_FILE.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


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

    def test_idempotent_remove_then_add_both_use_commontrace_server_name(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(session_start.subprocess, "run", fake_run):
            ok = session_start.configure_mcp("idempotent_key")

        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        # First call is remove
        self.assertIn("remove", calls[0])
        self.assertIn("commontrace", calls[0])
        # Second call is add
        self.assertIn("add", calls[1])
        self.assertIn("commontrace", calls[1])

    def test_remove_raises_but_add_still_succeeds(self):
        add_result = subprocess.CompletedProcess([], 0, stdout="", stderr="")

        def fake_run(argv, **kwargs):
            if "remove" in argv:
                raise FileNotFoundError("claude not found")
            return add_result

        with mock.patch.object(session_start.subprocess, "run", fake_run):
            ok = session_start.configure_mcp("key_after_remove_fail")

        self.assertTrue(ok)


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

    def test_provision_saves_mcp_configured_and_anonymous_flags(self):
        with mock.patch.object(session_start, "provision_api_key",
                               return_value="ct_live_xyz"), \
             mock.patch.object(session_start, "configure_mcp",
                               return_value=True), \
             mock.patch.object(session_start, "report_install"):
            key = session_start.ensure_setup()
        self.assertEqual(key, "ct_live_xyz")
        saved = json.loads(session_start.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertTrue(saved.get("anonymous"))
        self.assertTrue(saved.get("mcp_configured"))
        self.assertTrue(saved.get("pending_first_run_notice"))

    def test_provision_mcp_fail_sets_degraded_notice(self):
        with mock.patch.object(session_start, "provision_api_key",
                               return_value="ct_live_deg"), \
             mock.patch.object(session_start, "configure_mcp",
                               return_value=False), \
             mock.patch.object(session_start, "report_install"):
            key = session_start.ensure_setup()
        self.assertEqual(key, "ct_live_deg")
        saved = json.loads(session_start.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertFalse(saved.get("mcp_configured"))
        self.assertTrue(saved.get("pending_first_run_notice_degraded"))
        self.assertNotIn("pending_first_run_notice", saved)

    def test_stored_key_with_mcp_unconfigured_retries_mcp(self):
        session_start.save_config({"api_key": "stored_k", "mcp_configured": False})
        mcp_calls = []
        with mock.patch.object(session_start, "provision_api_key",
                               side_effect=_provision_forbidden), \
             mock.patch.object(session_start, "configure_mcp",
                               side_effect=lambda k: mcp_calls.append(k) or True):
            key = session_start.ensure_setup()
        self.assertEqual(key, "stored_k")
        self.assertEqual(len(mcp_calls), 1)
        saved = json.loads(session_start.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertTrue(saved.get("mcp_configured"))

    def test_env_var_with_anonymous_config_reconfigures_mcp_with_literal(self):
        os.environ["COMMONTRACE_API_KEY"] = "env_key_anon"
        self.addCleanup(os.environ.pop, "COMMONTRACE_API_KEY", None)
        session_start.save_config({"api_key": "old_key", "anonymous": True})
        mcp_calls = []
        with mock.patch.object(session_start, "provision_api_key",
                               side_effect=_provision_forbidden), \
             mock.patch.object(session_start, "configure_mcp",
                               side_effect=lambda k: mcp_calls.append(k) or True):
            key = session_start.ensure_setup()
        self.assertEqual(key, "env_key_anon")
        self.assertEqual(mcp_calls, ["${COMMONTRACE_API_KEY}"])
        saved = json.loads(session_start.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertTrue(saved.get("env_mcp_reconfigured"))

    def test_env_var_without_anonymous_config_skips_mcp(self):
        os.environ["COMMONTRACE_API_KEY"] = "env_key_manual"
        self.addCleanup(os.environ.pop, "COMMONTRACE_API_KEY", None)
        session_start.save_config({"api_key": "manual_key"})
        with mock.patch.object(session_start, "provision_api_key",
                               side_effect=_provision_forbidden), \
             mock.patch.object(session_start, "configure_mcp",
                               side_effect=AssertionError("configure_mcp must not be called")):
            key = session_start.ensure_setup()
        self.assertEqual(key, "env_key_manual")


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
