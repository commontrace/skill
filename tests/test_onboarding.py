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
