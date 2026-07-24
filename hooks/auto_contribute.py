"""Deterministic, structural fire-condition for auto-contribute on transition.

The "auto-contribute on move-on" trigger contributes a just-learned fix to
CommonTrace the moment the user naturally signals they are done with the task
("let's move on to the next task"), so no manual /trace is needed.

Design guarantees (see the YC-demo spec):
  * NO LLM / NLU on user messages — this is a pure regex/substring match over
    a small, word-boundaried MOVE_ON pattern set. No classification, no model.
  * Gated — fires only when the feature is explicitly enabled AND a
    contribution-worthy fix candidate exists this session AND nothing was
    contributed yet this session. Config-off by default for real users.
  * Deterministic — same inputs always give the same boolean, so the demo can
    "just type the agreed lines and record."

This module holds only the pure decision function; the wiring (reading config,
reading candidates, emitting the directive) lives in user_prompt.py.
"""

import re

# Word-boundaried move-on phrases. Kept small and specific so ordinary
# messages ("move the file", "next line") never false-fire. Matched
# case-insensitively against the lowercased message.
MOVE_ON_PATTERNS = [
    r"\bnext task\b",
    r"\bmove on to the next\b",
    r"\bon to the next task\b",
]


def _matches(message: str, patterns) -> bool:
    """True if any pattern matches the (lowercased) message. Structural only."""
    m = (message or "").lower()
    return any(re.search(p, m) for p in patterns)


def should_fire_contribution(
    *,
    enabled: bool,
    message: str,
    has_candidate: bool,
    already_contributed: bool,
    patterns=MOVE_ON_PATTERNS,
) -> bool:
    """Decide whether to auto-contribute on this user message.

    Fires only when every gate is satisfied:
      enabled           — the auto_contribute_on_move_on feature is turned on.
      has_candidate     — a contribution-worthy fix candidate exists this
                          session (not yet contributed).
      not already_contributed — nothing has been auto-contributed this session
                          (one-shot; prevents re-fire on later "next task"s).
      message matches   — the message structurally matches a MOVE_ON pattern.

    Returns a plain bool. No side effects, no I/O, no LLM.
    """
    if not enabled or already_contributed or not has_candidate:
        return False
    return _matches(message, patterns)
