#!/usr/bin/env python3
"""
CommonTrace UserPromptSubmit hook — Layer 1 state writer + first-turn nudge.

Increments the user turn counter in session state. On the first user turn,
injects a brief reminder to search CommonTrace before solving problems.

Also detects structural emphasis markers in user prompts — ALL CAPS ratio,
exclamation density, and emphasis keywords — to capture "user trauma"
signals. When a user emphasizes something strongly, that knowledge is
remembered more intensely (like emotional memory in humans).
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from session_state import get_state_dir, increment_counter, append_event


# Keywords that signal the user considers this important (case-insensitive)
_EMPHASIS_KEYWORDS = {
    "important", "critical", "crucial", "essential", "must", "never",
    "always", "remember", "urgent", "dangerous", "careful", "warning",
    "beware", "priority", "vital", "mandatory",
}

# Minimum word count to compute meaningful ratios
_MIN_WORDS = 4


def detect_emphasis(prompt: str) -> dict | None:
    """Detect structural emphasis markers in user prompt text.

    Purely structural — no NLU, no sentiment analysis. Detects:
    1. ALL CAPS word ratio (excludes short words and acronyms <= 3 chars)
    2. Exclamation mark density (per sentence)
    3. Emphasis keyword presence

    Returns emphasis dict if any signal detected, None otherwise.
    """
    if not prompt or len(prompt.strip()) < 10:
        return None

    words = prompt.split()
    if len(words) < _MIN_WORDS:
        return None

    # 1. ALL CAPS ratio — words >= 4 chars that are fully uppercase
    #    Excludes short acronyms (API, URL, CSS) and common all-caps
    #    identifiers (README, TODO, FIXME, CHANGELOG) which are normal
    _NORMAL_CAPS = {"README", "TODO", "FIXME", "NOTE", "HACK", "CHANGELOG",
                    "LICENSE", "HTTPS", "HTTP", "JSON", "HTML", "NULL",
                    "TRUE", "FALSE", "NONE", "YAML", "TOML"}
    long_words = [w for w in words if len(w) >= 4 and w.isalpha()]
    caps_words = [w for w in long_words if w.isupper() and w not in _NORMAL_CAPS]
    caps_ratio = len(caps_words) / max(len(long_words), 1)

    # 2. Exclamation density — count of ! relative to sentence count
    exclamation_count = prompt.count("!")
    # Rough sentence count: split on .!? but minimum 1
    sentence_count = max(1, len(re.split(r'[.!?]+', prompt.strip())) - 1)
    exclamation_density = exclamation_count / sentence_count

    # 3. Emphasis keywords (case-insensitive, whole words)
    prompt_lower = prompt.lower()
    found_keywords = [
        kw for kw in _EMPHASIS_KEYWORDS
        if re.search(rf'\b{kw}\b', prompt_lower)
    ]

    # Compute emphasis score (0.0-1.0)
    score = 0.0

    # Caps: 10%+ of long words in ALL CAPS is unusual emphasis
    if caps_ratio >= 0.1:
        score += min(0.4, caps_ratio)

    # Exclamations: > 0.5 per sentence is emphatic
    if exclamation_density > 0.5:
        score += min(0.3, exclamation_density * 0.15)

    # Keywords: each keyword adds signal
    if found_keywords:
        score += min(0.4, len(found_keywords) * 0.1)

    if score < 0.1:
        return None

    return {
        "emphasis_score": round(min(1.0, score), 2),
        "caps_ratio": round(caps_ratio, 2),
        "exclamation_density": round(exclamation_density, 2),
        "keywords": found_keywords[:5],
    }


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return

    state_dir = get_state_dir(data)
    count = increment_counter(state_dir, "user_turn_count")

    # Record timestamp for user_correction detection in post_tool_use
    append_event(state_dir, "user_turns.jsonl", {"turn": count})

    # Detect structural emphasis in user prompt
    prompt = data.get("prompt", "")
    if prompt:
        emphasis = detect_emphasis(prompt)
        if emphasis:
            append_event(state_dir, "emphasis.jsonl", emphasis)

    # First turn: inject a brief nudge to search CommonTrace
    if count == 1:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    "Reminder: search CommonTrace (search_traces) before "
                    "solving coding problems. Contribute after solving."
                ),
            }
        }
        print(json.dumps(output))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
