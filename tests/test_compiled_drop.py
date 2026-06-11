"""Monthly Compiled drop from session_start."""

import json
import time
from unittest import mock

from base import HookTestCase

import artifacts
import local_store
import session_start

DAY = 86400.0


def _prev_month():
    t = time.localtime()
    if t.tm_mon > 1:
        return t.tm_year, t.tm_mon - 1
    return t.tm_year - 1, 12


class TestCompiledDrop(HookTestCase):
    def setUp(self):
        super().setUp()
        for target, attr, value in [
            (session_start, "CONFIG_DIR", self.tmp_path),
            (session_start, "CONFIG_FILE", self.tmp_path / "config.json"),
        ]:
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_drops_recap_for_previous_month_and_saves_artifact(self):
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/test-project")
        year, month = _prev_month()
        start, _ = artifacts.month_range(year, month)
        conn.execute(
            "INSERT INTO sessions (id, project_id, started_at, error_count, "
            "resolution_count, contribution_count) VALUES (?, ?, ?, ?, ?, ?)",
            ("s1", pid, start + DAY, 5, 2, 0))
        conn.commit()
        config = {}
        note = session_start._compiled_drop(config)
        self.assertIsNotNone(note)
        self.assertIn("CommonTrace Compiled", note)
        self.assertIn("5 errors hit", note)
        files = list(artifacts.ARTIFACTS_DIR.glob("compiled-*.txt"))
        self.assertEqual(len(files), 1)
        t = time.localtime()
        self.assertEqual(config["last_compiled_month"],
                         f"{t.tm_year}-{t.tm_mon:02d}")

    def test_marker_blocks_repeat_even_on_empty_month(self):
        config = {}
        self.assertIsNone(session_start._compiled_drop(config))
        t = time.localtime()
        current = f"{t.tm_year}-{t.tm_mon:02d}"
        self.assertEqual(config["last_compiled_month"], current)
        saved = json.loads(
            session_start.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["last_compiled_month"], current)
        # second call: marker short-circuits before any db work
        self.assertIsNone(session_start._compiled_drop(config))
