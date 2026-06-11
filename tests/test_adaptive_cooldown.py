"""Adaptive cooldown: suppression must read real stats and never be permanent."""

import json
import unittest

from tests.base import HookTestCase, post_tool_use


class TestAdaptiveCooldown(HookTestCase):
    def _write_stats(self, name, fired, rate, key="fired"):
        (self.state_dir / "trigger_stats.json").write_text(json.dumps({
            name: {key: fired, "consumed": int(fired * rate), "rate": rate},
        }), encoding="utf-8")

    def test_ineffective_trigger_is_suppressed(self):
        self._write_stats("bash_error", fired=25, rate=0.0)
        self.assertEqual(
            post_tool_use._get_adaptive_cooldown(
                "bash_error", 30, self.state_dir), 90)

    def test_epsilon_floor_every_tenth_check_explores(self):
        self._write_stats("bash_error", fired=25, rate=0.0)
        values = [post_tool_use._get_adaptive_cooldown(
            "bash_error", 30, self.state_dir) for _ in range(10)]
        self.assertEqual(values[:9], [90] * 9)
        self.assertEqual(values[9], 30)  # exploration fires on the 10th

    def test_effective_trigger_halves_cooldown(self):
        self._write_stats("bash_error", fired=10, rate=0.5)
        self.assertEqual(
            post_tool_use._get_adaptive_cooldown(
                "bash_error", 30, self.state_dir), 15)

    def test_few_firings_no_suppression(self):
        self._write_stats("bash_error", fired=10, rate=0.0)
        self.assertEqual(
            post_tool_use._get_adaptive_cooldown(
                "bash_error", 30, self.state_dir), 30)

    def test_legacy_total_key_still_suppresses(self):
        self._write_stats("bash_error", fired=25, rate=0.0, key="total")
        self.assertEqual(
            post_tool_use._get_adaptive_cooldown(
                "bash_error", 30, self.state_dir), 90)

    def test_no_stats_file_returns_base(self):
        self.assertEqual(
            post_tool_use._get_adaptive_cooldown(
                "bash_error", 30, self.state_dir), 30)


if __name__ == "__main__":
    unittest.main()
