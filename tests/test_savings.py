"""Tests for hooks/savings.py — token-window sum, money, recap formatting.

Pure-function tests with a hand-built fixture transcript JSONL. No DB,
no network. sum_usage must NEVER raise — it returns 0 on any failure.
"""

import json
import unittest

from base import HookTestCase  # noqa: F401  (path bootstrap inserts hooks/)

import savings

def _line(ts, inp=0, out=0, cc=0, cr=0, typ="assistant"):
    """One transcript JSONL object: top-level ISO timestamp + message.usage."""
    return json.dumps({
        "timestamp": ts,
        "type": typ,
        "message": {"usage": {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_creation_input_tokens": cc,
            "cache_read_input_tokens": cr,
        }},
    })

class TestSumUsage(HookTestCase):
    def _write_transcript(self, lines):
        path = self.tmp_path / "transcript.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)

    def test_sums_usage_inside_window(self):
        path = self._write_transcript([
            _line("2026-06-14T12:00:00Z", inp=100, out=50),
            _line("2026-06-14T12:05:00Z", inp=200, out=80, cr=20),
        ])
        start = savings._epoch("2026-06-14T11:59:00Z")
        end = savings._epoch("2026-06-14T12:06:00Z")
        self.assertEqual(savings.sum_usage(path, start, end), 450)

    def test_excludes_messages_outside_window(self):
        path = self._write_transcript([
            _line("2026-06-14T11:00:00Z", inp=1000, out=1000),
            _line("2026-06-14T12:00:00Z", inp=100, out=50),
            _line("2026-06-14T13:00:00Z", inp=2000, out=2000),
        ])
        start = savings._epoch("2026-06-14T11:59:00Z")
        end = savings._epoch("2026-06-14T12:06:00Z")
        self.assertEqual(savings.sum_usage(path, start, end), 150)

    def test_caps_at_token_cap(self):
        path = self._write_transcript([
            _line("2026-06-14T12:00:00Z", inp=savings.TOKEN_CAP + 5_000_000),
        ])
        start = savings._epoch("2026-06-14T11:00:00Z")
        end = savings._epoch("2026-06-14T13:00:00Z")
        self.assertEqual(savings.sum_usage(path, start, end), savings.TOKEN_CAP)

    def test_missing_file_returns_zero(self):
        self.assertEqual(
            savings.sum_usage(str(self.tmp_path / "nope.jsonl"), 0.0, 9e9), 0)

    def test_empty_path_returns_zero(self):
        self.assertEqual(savings.sum_usage("", 0.0, 9e9), 0)

    def test_bad_json_lines_skipped_not_raised(self):
        path = self.tmp_path / "mixed.jsonl"
        path.write_text(
            _line("2026-06-14T12:00:00Z", inp=100) + "\n"
            "{ this is not json\n"
            + _line("2026-06-14T12:01:00Z", out=40) + "\n",
            encoding="utf-8")
        start = savings._epoch("2026-06-14T11:00:00Z")
        end = savings._epoch("2026-06-14T13:00:00Z")
        self.assertEqual(savings.sum_usage(str(path), start, end), 140)

    def test_non_int_usage_values_ignored(self):
        path = self.tmp_path / "weird.jsonl"
        bad = json.dumps({
            "timestamp": "2026-06-14T12:00:00Z",
            "message": {"usage": {"input_tokens": "lots", "output_tokens": 30}},
        })
        path.write_text(bad + "\n", encoding="utf-8")
        start = savings._epoch("2026-06-14T11:00:00Z")
        end = savings._epoch("2026-06-14T13:00:00Z")
        self.assertEqual(savings.sum_usage(str(path), start, end), 30)

class TestMoney(unittest.TestCase):
    def test_default_price_is_three(self):
        self.assertEqual(savings.DEFAULT_PRICE_PER_MTOK, 3.0)

    def test_one_million_tokens_at_default(self):
        self.assertEqual(savings.money_usd(1_000_000), 3.0)

    def test_half_million_tokens(self):
        self.assertEqual(savings.money_usd(500_000), 1.5)

    def test_zero_tokens(self):
        self.assertEqual(savings.money_usd(0), 0.0)

    def test_price_override(self):
        self.assertEqual(savings.money_usd(1_000_000, price_per_mtok=5.0), 5.0)

    def test_rounds_to_cents(self):
        self.assertEqual(savings.money_usd(333_333), 1.0)

class TestHm(unittest.TestCase):
    def test_minutes_under_an_hour(self):
        self.assertEqual(savings.fmt_duration(2), "~2m")
        self.assertEqual(savings.fmt_duration(2.4), "~2m")
        self.assertEqual(savings.fmt_duration(59), "~59m")

    def test_exactly_one_hour_drops_trailing_zero(self):
        self.assertEqual(savings.fmt_duration(60), "~1h")

    def test_ninety_minutes_is_one_point_five_hours(self):
        self.assertEqual(savings.fmt_duration(90), "~1.5h")

class TestRecapLine(unittest.TestCase):
    def test_delta_and_lifetime(self):
        life = {"minutes": 540.0, "tokens": 4_000_000, "events": 9}
        delta = {"minutes": 30.0, "tokens": 1_000_000}
        line = savings.format_recap_line(life, delta)
        self.assertTrue(line.startswith("CommonTrace: "))
        self.assertIn("saved you ~30m ~$3.0 since last session", line)
        self.assertIn("lifetime ~9h/~$12.0", line)
        self.assertIn(" · ", line)
        self.assertNotIn("saved others", line)

    def test_lifetime_only_when_no_delta(self):
        life = {"minutes": 120.0, "tokens": 2_000_000, "events": 3}
        line = savings.format_recap_line(life, None)
        self.assertEqual(line, "CommonTrace: lifetime ~2h/~$6.0")

    def test_empty_returns_empty_string(self):
        life = {"minutes": 0.0, "tokens": 0, "events": 0}
        self.assertEqual(savings.format_recap_line(life, None), "")

    def test_zero_delta_falls_back_to_lifetime_only(self):
        life = {"minutes": 120.0, "tokens": 2_000_000, "events": 3}
        delta = {"minutes": 0.0, "tokens": 0}
        line = savings.format_recap_line(life, delta)
        self.assertEqual(line, "CommonTrace: lifetime ~2h/~$6.0")

    def test_price_override_flows_into_money(self):
        life = {"minutes": 60.0, "tokens": 1_000_000, "events": 1}
        line = savings.format_recap_line(life, None, price_per_mtok=10.0)
        self.assertIn("~$10.0", line)

if __name__ == "__main__":
    unittest.main()
