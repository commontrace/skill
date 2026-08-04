"""Shared test base: isolates every test from the real ~/.commontrace.

Patches the module-level path constants in artifacts, local_store, and
ct_config so tests never touch the developer's real local.db, cooldowns, or
config, and never make network calls (no API key resolvable).

ct_config owns CONFIG_FILE and COOLDOWN_DIR for the whole hook suite, so
one patch here covers every module that reads them.
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
import ct_config  # noqa: E402
import detection  # noqa: E402,F401
import local_store  # noqa: E402
import post_tool_use  # noqa: E402
import resolution  # noqa: E402,F401
import retrieval  # noqa: E402,F401
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
            (ct_config, "COOLDOWN_DIR", self.tmp_path / "cooldowns"),
            (ct_config, "CONFIG_FILE", self.tmp_path / "no-config.json"),
        ]:
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Offline guarantee: no API key from the environment either. That now
        # includes CLAUDE_PLUGIN_OPTION_API_KEY — Claude Code exports the
        # plugin's userConfig options into every hook process, so a developer
        # running this suite with the plugin installed and configured would
        # otherwise hand their real contributor key to the tests.
        env_patcher = mock.patch.dict(os.environ)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        os.environ.pop("COMMONTRACE_API_KEY", None)
        os.environ.pop("CLAUDE_PLUGIN_OPTION_API_KEY", None)

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
