"""Tests for hooks/artifacts.py — local-first viral artifacts.

Pure-function tests use fixed timestamps (no wall-clock dependence).
DB-backed tests use HookTestCase isolation. Privacy tests seed worst-case
PII and assert none of it reaches any artifact.
"""

import contextlib
import io
import json
import time
import unittest
import xml.etree.ElementTree

from base import HookTestCase

import artifacts
import local_store

DAY = 86400.0


def seed_sensitive_project(conn):
    """Worst-case PII rows: tests assert none of this text reaches artifacts."""
    pid = local_store.ensure_project(
        conn, "/home/secretuser/topsecret-repo",
        language="python", framework="fastapi")
    now = time.time()
    conn.execute(
        "INSERT INTO error_signatures (project_id, signature, created_at, "
        "last_seen_at, seen_count, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
        (pid, "ModuleNotFoundError: secret_module in /home/secretuser/app.py",
         now - 3 * DAY, now - DAY, 3, now - DAY))
    conn.execute(
        "INSERT INTO error_signatures (project_id, signature, created_at, "
        "last_seen_at, seen_count, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
        (pid, "TypeError: secret_func() broke in /home/secretuser/lib.py",
         now - 100 * DAY, now - 95 * DAY, 1, None))
    conn.commit()
    return pid


class TestTemperature(unittest.TestCase):
    def test_hot_warm_cool_cold_frozen_bounds(self):
        now = 1_750_000_000.0
        self.assertEqual(artifacts.temperature(now - 1 * DAY, now), "hot")
        self.assertEqual(artifacts.temperature(now - 8 * DAY, now), "warm")
        self.assertEqual(artifacts.temperature(now - 31 * DAY, now), "cool")
        self.assertEqual(artifacts.temperature(now - 91 * DAY, now), "cold")
        self.assertEqual(artifacts.temperature(now - 181 * DAY, now), "frozen")

    def test_future_timestamp_clamps_to_hot(self):
        now = 1_750_000_000.0
        self.assertEqual(artifacts.temperature(now + DAY, now), "hot")


class TestIntensity(unittest.TestCase):
    def test_single_hit_instant_fix_is_base(self):
        t = 1_750_000_000.0
        self.assertEqual(artifacts.intensity(1, t, t), 0.25)

    def test_repeats_raise_intensity(self):
        t = 1_750_000_000.0
        self.assertEqual(artifacts.intensity(3, t, t), 0.55)

    def test_repeat_contribution_caps_at_four(self):
        t = 1_750_000_000.0
        self.assertEqual(artifacts.intensity(99, t, t), 0.85)

    def test_long_fight_caps_at_one(self):
        t = 1_750_000_000.0
        self.assertEqual(artifacts.intensity(99, t, t + 30 * DAY), 1.0)

    def test_unresolved_has_no_latency_term(self):
        t = 1_750_000_000.0
        self.assertEqual(artifacts.intensity(1, t, None), 0.25)


class TestMonthRange(unittest.TestCase):
    def test_range_covers_whole_month(self):
        start, end = artifacts.month_range(2026, 5)
        self.assertEqual(time.localtime(start)[:5], (2026, 5, 1, 0, 0))
        self.assertEqual(time.localtime(end)[:4], (2026, 5, 31, 23))

    def test_february_leap_year(self):
        start, end = artifacts.month_range(2024, 2)
        self.assertEqual(time.localtime(end)[:3], (2024, 2, 29))


class TestStruggleGrid(unittest.TestCase):
    def test_empty_session_resolved_is_single_green(self):
        self.assertEqual(artifacts.struggle_grid([], [], resolved=True), "🟩")

    def test_empty_session_unresolved_is_single_idle(self):
        self.assertEqual(artifacts.struggle_grid([], [], resolved=False), "⬜")

    def test_grid_is_ten_cells_and_ends_green_when_resolved(self):
        t0 = 1_750_000_000.0
        errors = [t0, t0 + 60, t0 + 120]
        changes = [t0 + 300, t0 + 600]
        grid = artifacts.struggle_grid(errors, changes, resolved=True)
        cells = list(grid)
        self.assertEqual(len(cells), 10)
        self.assertEqual(cells[-1], "🟩")
        self.assertEqual(cells[0], "🟥")

    def test_error_wins_over_change_in_same_bucket(self):
        t0 = 1_750_000_000.0
        grid = artifacts.struggle_grid([t0, t0 + 1000], [t0 + 1],
                                       resolved=False)
        self.assertEqual(grid[0], "🟥")

    def test_zero_timestamps_filtered(self):
        self.assertEqual(artifacts.struggle_grid([0, 0], [0], resolved=True),
                         "🟩")


class TestStruggleLine(unittest.TestCase):
    def test_line_format_with_trace(self):
        line = artifacts.struggle_line("🟥🟩", 47.4, 8, trace_id="a3f9")
        self.assertEqual(
            line,
            "🟥🟩 47min · 8 errors · solved → https://commontrace.org/t/a3f9")

    def test_singular_error_no_trace(self):
        self.assertEqual(artifacts.struggle_line("🟩", 2, 1),
                         "🟩 2min · 1 error · solved")


class TestLoadBrainData(HookTestCase):
    def test_counts_and_label(self):
        conn = self.get_conn()
        seed_sensitive_project(conn)
        data = artifacts.load_brain_data(conn)
        self.assertEqual(data["solved"], 1)
        self.assertEqual(data["open"], 1)
        self.assertEqual(len(data["projects"]), 1)
        self.assertEqual(data["projects"][0]["label"], "python/fastapi")
        self.assertEqual(len(data["projects"][0]["nodes"]), 2)

    def test_nodes_carry_no_text_from_db(self):
        conn = self.get_conn()
        seed_sensitive_project(conn)
        blob = json.dumps(artifacts.load_brain_data(conn))
        for leak in ("secretuser", "topsecret", "secret_module",
                     "secret_func", "app.py", "lib.py", "/home"):
            self.assertNotIn(leak, blob)

    def test_node_shape(self):
        conn = self.get_conn()
        seed_sensitive_project(conn)
        node = artifacts.load_brain_data(conn)["projects"][0]["nodes"][0]
        self.assertEqual(set(node), {"intensity", "temperature", "resolved",
                                     "age_days", "opacity"})

    def test_empty_db(self):
        conn = self.get_conn()
        data = artifacts.load_brain_data(conn)
        self.assertEqual(data["projects"], [])
        self.assertEqual(data["solved"], 0)
        self.assertEqual(data["open"], 0)


class TestRenderers(HookTestCase):
    def test_svgs_parse_and_have_no_leaks(self):
        conn = self.get_conn()
        seed_sensitive_project(conn)
        data = artifacts.load_brain_data(conn)
        for render in (artifacts.render_brain_svg, artifacts.render_badge_svg):
            out = render(data)
            xml.etree.ElementTree.fromstring(out)  # must be well-formed
            for leak in ("secretuser", "topsecret", "secret_module",
                         "secret_func", "app.py", "lib.py", "/home"):
                self.assertNotIn(leak, out)

    def test_html_is_self_contained_and_clean(self):
        conn = self.get_conn()
        seed_sensitive_project(conn)
        data = artifacts.load_brain_data(conn)
        html = artifacts.render_brain_html(data)
        self.assertIn("<svg", html)
        self.assertIn("My agent's brain", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("src=", html)
        for leak in ("secretuser", "topsecret", "secret_module",
                     "secret_func", "app.py", "lib.py", "/home"):
            self.assertNotIn(leak, html)

    def test_empty_state_svg(self):
        out = artifacts.render_brain_svg(
            {"projects": [], "solved": 0, "open": 0, "now": 0.0})
        xml.etree.ElementTree.fromstring(out)
        self.assertIn("No knowledge captured yet", out)

    def test_badge_shows_solved_count(self):
        conn = self.get_conn()
        seed_sensitive_project(conn)
        data = artifacts.load_brain_data(conn)
        self.assertIn("1 solved", artifacts.render_badge_svg(data))


class TestCompiledRecap(HookTestCase):
    def _seed_month(self, conn, year=2026, month=5):
        pid = local_store.ensure_project(conn, "/test-project")
        start, _ = artifacts.month_range(year, month)
        mid = start + 10 * DAY
        conn.execute(
            "INSERT INTO sessions (id, project_id, started_at, error_count, "
            "resolution_count, contribution_count, top_pattern) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("s1", pid, mid, 12, 5, 1, "error_resolution"))
        conn.execute(
            "INSERT INTO sessions (id, project_id, started_at, error_count, "
            "resolution_count, contribution_count, top_pattern) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("s2", pid, mid + DAY, 3, 2, 0, "error_resolution"))
        conn.execute(
            "INSERT INTO error_signatures (project_id, signature, created_at, "
            "last_seen_at, seen_count, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
            (pid, "sig-a", mid - DAY, mid, 4, mid))
        conn.commit()

    def test_recap_contains_own_numbers(self):
        conn = self.get_conn()
        self._seed_month(conn)
        text = artifacts.compiled_recap(conn, 2026, 5)
        self.assertIn("CommonTrace Compiled — May 2026", text)
        self.assertIn("2 sessions", text)
        self.assertIn("15 errors hit · 7 resolutions", text)
        self.assertIn("1 error signature solved for good", text)
        self.assertIn("hardest fight: one error took 4 hits", text)
        self.assertIn("signature move: error resolution", text)
        self.assertIn("1 trace contributed to the commons", text)

    def test_empty_month_returns_none(self):
        conn = self.get_conn()
        self.assertIsNone(artifacts.compiled_recap(conn, 2026, 4))

    def test_no_text_from_db_in_recap(self):
        conn = self.get_conn()
        self._seed_month(conn)
        text = artifacts.compiled_recap(conn, 2026, 5)
        self.assertNotIn("sig-a", text)
        self.assertNotIn("/test-project", text)


class TestWriteArtifactAndCLI(HookTestCase):
    def test_write_artifact_perms(self):
        path = artifacts.write_artifact("probe.txt", "hello\n")
        self.assertEqual(path.read_text(encoding="utf-8"), "hello\n")
        self.assertEqual(path.parent, artifacts.ARTIFACTS_DIR)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_cli_brain_writes_three_files(self):
        conn = self.get_conn()
        seed_sensitive_project(conn)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = artifacts.main(["artifacts.py", "brain"])
        self.assertEqual(rc, 0)
        for name in ("brain.html", "brain.svg", "badge.svg"):
            self.assertTrue((artifacts.ARTIFACTS_DIR / name).exists())
        self.assertIn("1 solved", buf.getvalue())

    def test_cli_recap_empty_month(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = artifacts.main(["artifacts.py", "recap", "2026-04"])
        self.assertEqual(rc, 0)
        self.assertIn("No activity recorded for 2026-04", buf.getvalue())

    def test_cli_unknown_command(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = artifacts.main(["artifacts.py", "bogus"])
        self.assertEqual(rc, 1)
