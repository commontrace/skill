"""How the hooks decide which API key to use, and why order matters.

COMMONTRACE_API_KEY is the explicit override; ``config.json`` normally holds
the anonymous key session_start provisions on its own. Reading the file first
meant the documented override silently did nothing once that file existed —
every contribution kept going out under the per-machine anonymous account with
no sign anything was wrong.

There was briefly a `userConfig` option here too, read from
CLAUDE_PLUGIN_OPTION_API_KEY and settable with `--config api_key=...`. It
worked, and was verified end to end against a live session. It was removed
because declaring a userConfig option makes Claude Code prompt every installer
for it, and being asked for an API key you do not need is precisely the
decision this plugin exists to avoid.
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
        "provision_api_key() must not run when an explicit key exists")


class TestLoadApiKeyPrecedence(HookTestCase):
    def test_environment_wins_over_config_file(self):
        ct_config.write_config({"api_key": "anon_from_first_run"})
        os.environ["COMMONTRACE_API_KEY"] = "ct_mine"
        self.assertEqual(ct_config.load_api_key(), "ct_mine")

    def test_config_file_used_when_environment_absent(self):
        ct_config.write_config({"api_key": "stored"})
        self.assertEqual(ct_config.load_api_key(), "stored")

    def test_blank_environment_falls_through(self):
        """An exported-but-empty variable must not blank out a good key."""
        ct_config.write_config({"api_key": "stored"})
        os.environ["COMMONTRACE_API_KEY"] = "   "
        self.assertEqual(ct_config.load_api_key(), "stored")

    def test_no_key_anywhere_returns_empty(self):
        self.assertEqual(ct_config.load_api_key(), "")


class TestEnsureSetupWithExplicitKey(HookTestCase):
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

    def test_explicit_key_skips_anonymous_provisioning(self):
        os.environ["COMMONTRACE_API_KEY"] = "ct_mine"
        with mock.patch.object(session_start, "provision_api_key",
                               side_effect=_provision_forbidden), \
             mock.patch.object(session_start, "configure_mcp", return_value=True):
            self.assertEqual(session_start.ensure_setup(), "ct_mine")

        saved = json.loads(
            session_start.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["api_key"], "ct_mine")

    def test_explicit_key_replaces_a_previously_anonymous_one(self):
        """The upgrade path: anonymous first, own key exported later."""
        session_start.save_config({"api_key": "anon_key", "anonymous": True,
                                   "mcp_configured": True})
        os.environ["COMMONTRACE_API_KEY"] = "ct_mine"
        mcp_calls = []
        with mock.patch.object(session_start, "provision_api_key",
                               side_effect=_provision_forbidden), \
             mock.patch.object(session_start, "configure_mcp",
                               side_effect=lambda k: mcp_calls.append(k) or True):
            self.assertEqual(session_start.ensure_setup(), "ct_mine")

        saved = json.loads(
            session_start.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["api_key"], "ct_mine")
        self.assertNotIn("anonymous", saved)
        # MCP pointed at the anonymous key; it has to be re-pointed, and by
        # indirection so the raw key never lands in the MCP config.
        self.assertEqual(mcp_calls, ["${COMMONTRACE_API_KEY}"])

    def test_unchanged_key_does_not_rewrite_config_every_session(self):
        session_start.save_config({"api_key": "ct_mine", "mcp_configured": True})
        os.environ["COMMONTRACE_API_KEY"] = "ct_mine"
        before = session_start.CONFIG_FILE.read_text(encoding="utf-8")
        with mock.patch.object(session_start, "provision_api_key",
                               side_effect=_provision_forbidden), \
             mock.patch.object(session_start, "configure_mcp",
                               side_effect=AssertionError):
            self.assertEqual(session_start.ensure_setup(), "ct_mine")

        self.assertEqual(
            session_start.CONFIG_FILE.read_text(encoding="utf-8"), before)


class TestManifestAsksForNothing(unittest.TestCase):
    def test_plugin_json_declares_no_userConfig(self):
        """Installing must not prompt for anything.

        A declared userConfig option is prompted for at enable time, and the
        README's promise is "no account, no email, no environment variables,
        no decisions". Re-adding one re-introduces a question every installer
        has to answer about a key that publishing does not require.
        """
        import pathlib
        manifest = (pathlib.Path(__file__).resolve().parent.parent
                    / ".claude-plugin" / "plugin.json")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertNotIn("userConfig", data)


class TestConcurrentProvisioningRace(HookTestCase):
    """The explicit-key branch must take the provisioning lock, not skip it.

    Found by running the real plugin in a live session on a machine that also
    had a copy of these hooks wired straight into settings.json: two
    SessionStart processes fired at once, one carrying an explicit key and one
    not. The one without it read an empty config, provisioned an anonymous key,
    and overwrote the explicit one — visible in the MCP registration as two
    `claude mcp add` calls, the anonymous key second and winning.

    Asserting on the outcome of two sequential calls proves nothing: by then
    the key is already on disk and the second call reads it. So this asserts
    the property that actually fixes the race — that the branch BLOCKS while
    another process holds the lock.
    """

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

    @unittest.skipIf(os.name == "nt", "fcntl locking is POSIX-only")
    def test_explicit_key_branch_waits_for_the_provisioning_lock(self):
        import fcntl
        import multiprocessing

        held = open(self.tmp_path / ".provision_lock", "w")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX)

        ctx = multiprocessing.get_context("fork")
        q = ctx.Queue()
        child = ctx.Process(target=_child_ensure_setup, args=(q,))
        child.start()
        self.addCleanup(lambda: child.kill() if child.is_alive() else None)

        child.join(2.0)
        self.assertTrue(
            child.is_alive(),
            "ensure_setup() returned while another process held the "
            "provisioning lock, so the explicit key can still be clobbered "
            "by a concurrent anonymous provisioning")

        fcntl.flock(held, fcntl.LOCK_UN)
        child.join(20)
        self.assertFalse(child.is_alive(), "child never finished after unlock")
        self.assertEqual(q.get(timeout=5), "ct_mine")


def _child_ensure_setup(q):
    """Run the explicit-key branch in a forked child and report the key.

    Forked, so it inherits the parent's patched CONFIG_DIR/CONFIG_FILE and
    sys.path without re-importing anything.
    """
    os.environ["COMMONTRACE_API_KEY"] = "ct_mine"
    with mock.patch.object(session_start, "configure_mcp", return_value=True), \
         mock.patch.object(session_start, "provision_api_key",
                           side_effect=_provision_forbidden):
        q.put(session_start.ensure_setup())


if __name__ == "__main__":
    unittest.main()
