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
