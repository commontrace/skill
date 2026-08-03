"""The domain_entry retrieval trigger must fire for files in subdirectories.

Regression: session_start registers the project under the session cwd
(e.g. /proj), but _check_domain_entry resolved the project by the edited
file's PARENT dir. For any file not in the repo root (src/, api/, lib/…)
the exact WHERE path=? lookup returned None → domain_entry never fired →
no CommonTrace search ran when the agent entered a new language.

domain_entry is a RETRIEVAL trigger (it searches the commons and injects
results), not a scored detection pattern — the novelty_encounter score it
used to feed was retired in v1.5. Its observable is the trigger_feedback
row, which is also what the adaptive cooldown reads back.
"""

import unittest

from tests.base import HookTestCase, local_store, post_tool_use


class TestDomainEntry(HookTestCase):
    def _register(self, path="/proj", language="python"):
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, path, language=language)
        (self.state_dir / "project_id").write_text(str(pid), encoding="utf-8")
        return pid

    def _fired(self):
        conn = self.get_conn()
        return conn.execute(
            "SELECT trigger_name FROM trigger_feedback "
            "WHERE session_id = ? AND trigger_name = 'domain_entry'",
            (self.state_dir.name,)).fetchone()

    def test_fires_for_file_in_subdirectory(self):
        """Editing a rust file under /proj/src must fire domain_entry."""
        self._register(path="/proj", language="python")
        out = post_tool_use._check_domain_entry("/proj/src/foo.rs", self.state_dir)
        self.assertIsNotNone(
            self._fired(), "domain_entry did not fire for a subdir file")
        # Offline: no API key → no additionalContext, but firing still recorded.
        self.assertIsNone(out)

    def test_fires_for_deeply_nested_file(self):
        self._register(path="/proj", language="python")
        post_tool_use._check_domain_entry(
            "/proj/api/app/routers/foo.go", self.state_dir)
        self.assertIsNotNone(self._fired())

    def test_same_language_does_not_fire(self):
        """A .py edit in a python project (any depth) must NOT fire."""
        self._register(path="/proj", language="python")
        post_tool_use._check_domain_entry("/proj/src/foo.py", self.state_dir)
        self.assertIsNone(self._fired())

    def test_no_project_bridge_is_silent(self):
        out = post_tool_use._check_domain_entry("/proj/src/foo.rs", self.state_dir)
        self.assertIsNone(out)
        self.assertIsNone(self._fired())


if __name__ == "__main__":
    unittest.main()
