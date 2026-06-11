"""Shared test base: isolates every test from the real ~/.commontrace.

Patches the module-level path constants in artifacts, local_store, and
post_tool_use so tests never touch the developer's real local.db, cooldowns, or config,
and never make network calls (no API key resolvable).
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import artifacts  # noqa: E402
import local_store  # noqa: E402
import post_tool_use  # noqa: E402
from session_state import append_event, read_events  # noqa: E402,F401


class HookTestCase(unittest.TestCase):
    """Temp-dir isolation + offline guarantee for hook tests."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

        for target, attr, value in [
            (artifacts, "ARTIFACTS_DIR", self.tmp_path / "artifacts"),
            (local_store, "DB_PATH", self.tmp_path / "local.db"),
            (post_tool_use, "COOLDOWN_DIR", self.tmp_path / "cooldowns"),
            (post_tool_use, "CONFIG_FILE", self.tmp_path / "no-config.json"),
        ]:
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Offline guarantee: no API key from the environment either
        env_patcher = mock.patch.dict(os.environ)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        os.environ.pop("COMMONTRACE_API_KEY", None)

        self.state_dir = self.tmp_path / "session-test"
        self.state_dir.mkdir()

    def get_conn(self):
        conn = local_store._get_conn()
        self.addCleanup(conn.close)
        return conn

    def write_project_bridge(self, conn, state_dir=None):
        """Register a project and write the project_id bridge file."""
        pid = local_store.ensure_project(conn, "/test-project")
        ((state_dir or self.state_dir) / "project_id").write_text(
            str(pid), encoding="utf-8")
        return pid
