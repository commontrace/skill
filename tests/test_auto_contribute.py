"""Pure fire-condition for auto-contribute on transition (Task 1.1).

`should_fire_contribution` is a deterministic, structural gate — no LLM, no
NLU. It fires only when ALL of: the feature is enabled, the message
structurally matches a MOVE_ON pattern, a contribution-worthy fix candidate
exists this session, and nothing was contributed yet this session.

Written as stdlib unittest so it runs under both the repo's
`python -m unittest discover` harness and pytest.
"""

import unittest

from auto_contribute import should_fire_contribution, MOVE_ON_PATTERNS as P


class ShouldFireContributionTests(unittest.TestCase):
    def test_fires_when_all_conditions_met(self):
        self.assertIs(
            should_fire_contribution(
                enabled=True,
                message="Looks fixed — let's move on to the next task in the plan.",
                has_candidate=True,
                already_contributed=False,
                patterns=P,
            ),
            True,
        )

    def test_no_fire_when_disabled(self):
        self.assertIs(
            should_fire_contribution(
                enabled=False,
                message="move on to the next task",
                has_candidate=True,
                already_contributed=False,
                patterns=P,
            ),
            False,
        )

    def test_no_fire_without_candidate(self):
        self.assertIs(
            should_fire_contribution(
                enabled=True,
                message="next task please",
                has_candidate=False,
                already_contributed=False,
                patterns=P,
            ),
            False,
        )

    def test_no_fire_if_already_contributed(self):
        self.assertIs(
            should_fire_contribution(
                enabled=True,
                message="on to the next task",
                has_candidate=True,
                already_contributed=True,
                patterns=P,
            ),
            False,
        )

    def test_no_fire_on_unrelated_message(self):
        self.assertIs(
            should_fire_contribution(
                enabled=True,
                message="can you refactor this function?",
                has_candidate=True,
                already_contributed=False,
                patterns=P,
            ),
            False,
        )


if __name__ == "__main__":
    unittest.main()
