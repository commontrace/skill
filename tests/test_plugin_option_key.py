"""The API key supplied at install time via the plugin's userConfig.

`claude plugin install commontrace@commontrace --config api_key=...` (and the
enable-time prompt) store the value against the `api_key` entry declared in
.claude-plugin/plugin.json. Claude Code exports it to hook processes as
CLAUDE_PLUGIN_OPTION_API_KEY.

The point of these tests is the precedence rule: that key has to beat the
anonymous one session_start writes into config.json on first run. Someone who
pastes a contributor key at install time means to contribute, and the failure
mode without this is silent and expensive — every contribution keeps going out
under the anonymous account, which the invitation gate then rejects with a 403
at the very end of the flow.
"""

import json
import os
import unittest
from unittest import mock

from base import HookTestCase

import ct_config
import session_start


def _provision_forbidden(*_args, **_kwargs):
    raise AssertionError(
        "provision_api_key() must not run when an install-time key exists")


class TestLoadApiKeyPrecedence(HookTestCase):
    def test_plugin_option_wins_over_config_file(self):
        ct_config.write_config({"api_key": "anon_from_first_run"})
        os.environ["CLAUDE_PLUGIN_OPTION_API_KEY"] = "ct_contributor"
        self.assertEqual(ct_config.load_api_key(), "ct_contributor")

    def test_plugin_option_wins_over_environment(self):
        os.environ["COMMONTRACE_API_KEY"] = "from_env"
        os.environ["CLAUDE_PLUGIN_OPTION_API_KEY"] = "ct_contributor"
        self.assertEqual(ct_config.load_api_key(), "ct_contributor")

    def test_config_file_used_when_option_absent(self):
        ct_config.write_config({"api_key": "stored"})
        self.assertEqual(ct_config.load_api_key(), "stored")

    def test_environment_still_used_as_last_resort(self):
        os.environ["COMMONTRACE_API_KEY"] = "from_env"
        self.assertEqual(ct_config.load_api_key(), "from_env")

    def test_blank_option_falls_through(self):
        """An option left empty in the install dialog must not win.

        Claude Code exports declared-but-unset options as an empty string, so
        a bare presence check would blank out a perfectly good stored key.
        """
        ct_config.write_config({"api_key": "stored"})
        os.environ["CLAUDE_PLUGIN_OPTION_API_KEY"] = "   "
        self.assertEqual(ct_config.load_api_key(), "stored")

    def test_no_key_anywhere_returns_empty(self):
        self.assertEqual(ct_config.load_api_key(), "")


class TestEnsureSetupWithPluginOption(HookTestCase):
    def setUp(self):
        super().setUp()
        for attr, value in [
            ("CONFIG_DIR", self.tmp_path),
            ("CONFIG_FILE", self.tmp_path / "config.json"),
            ("PENDING_DIR", self.tmp_path / "pending"),
        ]:
            patcher = mock.patch.object(session_start, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_install_time_key_skips_anonymous_provisioning(self):
        os.environ["CLAUDE_PLUGIN_OPTION_API_KEY"] = "ct_contributor"
        mcp_calls = []
        with mock.patch.object(session_start, "provision_api_key",
                               side_effect=_provision_forbidden), \
             mock.patch.object(session_start, "configure_mcp",
                               side_effect=lambda k: mcp_calls.append(k) or True):
            key = session_start.ensure_setup()

        self.assertEqual(key, "ct_contributor")
        self.assertEqual(mcp_calls, ["ct_contributor"])
        saved = json.loads(
            session_start.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["api_key"], "ct_contributor")

    def test_install_time_key_replaces_a_previously_anonymous_key(self):
        """The upgrade path: anonymous first, contributor key added later."""
        session_start.save_config({"api_key": "anon_key", "anonymous": True,
                                   "mcp_configured": True})
        os.environ["CLAUDE_PLUGIN_OPTION_API_KEY"] = "ct_contributor"
        mcp_calls = []
        with mock.patch.object(session_start, "provision_api_key",
                               side_effect=_provision_forbidden), \
             mock.patch.object(session_start, "configure_mcp",
                               side_effect=lambda k: mcp_calls.append(k) or True):
            key = session_start.ensure_setup()

        self.assertEqual(key, "ct_contributor")
        saved = json.loads(
            session_start.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["api_key"], "ct_contributor")
        # MCP was registered against the anonymous key; it has to be re-pointed
        # or every MCP call keeps authenticating as the anonymous account.
        self.assertEqual(mcp_calls, ["ct_contributor"])
        self.assertNotIn("anonymous", saved)

    def test_unchanged_key_does_not_rewrite_config_every_session(self):
        session_start.save_config({"api_key": "ct_contributor",
                                   "mcp_configured": True})
        os.environ["CLAUDE_PLUGIN_OPTION_API_KEY"] = "ct_contributor"
        before = session_start.CONFIG_FILE.read_text(encoding="utf-8")
        with mock.patch.object(session_start, "provision_api_key",
                               side_effect=_provision_forbidden), \
             mock.patch.object(session_start, "configure_mcp",
                               side_effect=AssertionError):
            key = session_start.ensure_setup()

        self.assertEqual(key, "ct_contributor")
        self.assertEqual(
            session_start.CONFIG_FILE.read_text(encoding="utf-8"), before)

    def test_failed_mcp_registration_is_retried_next_session(self):
        session_start.save_config({"api_key": "ct_contributor",
                                   "mcp_configured": False})
        os.environ["CLAUDE_PLUGIN_OPTION_API_KEY"] = "ct_contributor"
        mcp_calls = []
        with mock.patch.object(session_start, "provision_api_key",
                               side_effect=_provision_forbidden), \
             mock.patch.object(session_start, "configure_mcp",
                               side_effect=lambda k: mcp_calls.append(k) or True):
            session_start.ensure_setup()

        self.assertEqual(mcp_calls, ["ct_contributor"])
        saved = json.loads(
            session_start.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertTrue(saved["mcp_configured"])


class TestManifestDeclaresTheOption(unittest.TestCase):
    """The env var only exists if the manifest declares the option."""

    def test_plugin_json_declares_a_sensitive_api_key_option(self):
        import pathlib
        manifest = (pathlib.Path(__file__).resolve().parent.parent
                    / ".claude-plugin" / "plugin.json")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        option = data["userConfig"]["api_key"]
        self.assertEqual(option["type"], "string")
        self.assertTrue(option["title"])
        self.assertTrue(option["description"])
        # Keeps the key out of settings.json and in secure storage.
        self.assertIs(option["sensitive"], True)
        # Required would break the zero-decision path: no key at all is a
        # supported state, session_start provisions an anonymous one.
        self.assertNotIn("required", option)


if __name__ == "__main__":
    unittest.main()
