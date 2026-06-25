import unittest
import stop


class ReinforcementUnitTest(unittest.TestCase):
    def test_clamp(self):
        assert stop._clamp(0.7, 1.3, 0.5) == 0.7
        assert stop._clamp(0.7, 1.3, 1.5) == 1.3
        assert stop._clamp(0.7, 1.3, 1.0) == 1.0

    def test_is_protected(self):
        assert stop._is_protected("error_resolution") is True
        assert stop._is_protected("security_hardening") is True
        assert stop._is_protected("novelty_encounter") is False

    def test_pattern_effectiveness_aggregates(self):
        eff = {
            "error_recurrence": {"fired": 4, "consumed": 2, "rate": 0.5},
            "bash_error": {"fired": 2, "consumed": 2, "rate": 1.0},
        }
        out = stop._pattern_effectiveness("error_resolution", eff)
        assert out["fired"] == 6
        assert out["consumed"] == 4
        assert out["rate"] == round(4 / 6, 2)

    def test_pattern_effectiveness_unmapped_is_none(self):
        assert stop._pattern_effectiveness("cross_file_breadth", {}) is None

    def test_pattern_effectiveness_zero_fired_is_none(self):
        eff = {"domain_entry": {"fired": 0, "consumed": 0, "rate": 0.0}}
        assert stop._pattern_effectiveness("novelty_encounter", eff) is None

    def test_apply_reinforcement_none_is_noop(self):
        scores = {"error_resolution": 3.0}
        stop._apply_reinforcement(scores, None)
        assert scores == {"error_resolution": 3.0}

    def test_apply_reinforcement_protected_boost(self):
        # rate 1.0 -> mult 0.85 + 0.5 = 1.35 -> clamped to 1.3
        scores = {"error_resolution": 3.0}
        eff = {"error_recurrence": {"fired": 4, "consumed": 4, "rate": 1.0}}
        stop._apply_reinforcement(scores, eff)
        self.assertAlmostEqual(scores["error_resolution"], 3.9, places=5)

    def test_apply_reinforcement_unprotected_demote(self):
        # novelty, rate 0.0 -> mult 0.85 (>= lo 0.7) -> 2.0 * 0.85 = 1.7
        scores = {"novelty_encounter": 2.0}
        eff = {"domain_entry": {"fired": 5, "consumed": 0, "rate": 0.0}}
        stop._apply_reinforcement(scores, eff)
        self.assertAlmostEqual(scores["novelty_encounter"], 1.7, places=5)

    def test_apply_reinforcement_below_min_fired_skips(self):
        scores = {"error_resolution": 3.0}
        eff = {"error_recurrence": {"fired": 2, "consumed": 2, "rate": 1.0}}  # < MIN_FIRED
        stop._apply_reinforcement(scores, eff)
        assert scores["error_resolution"] == 3.0

    def test_apply_reinforcement_unmapped_unchanged(self):
        scores = {"cross_file_breadth": 1.5}
        eff = {"error_recurrence": {"fired": 9, "consumed": 9, "rate": 1.0}}
        stop._apply_reinforcement(scores, eff)
        assert scores["cross_file_breadth"] == 1.5

    def test_apply_reinforcement_zero_score_skips(self):
        scores = {"error_resolution": 0.0}
        eff = {"error_recurrence": {"fired": 9, "consumed": 0, "rate": 0.0}}
        stop._apply_reinforcement(scores, eff)
        assert scores["error_resolution"] == 0.0


if __name__ == "__main__":
    unittest.main()
