#!/usr/bin/env python3
"""
CommonTrace UserPromptSubmit hook — Layer 1 state writer + first-turn nudge.

Increments the user turn counter in session state. On the first user turn,
injects a brief reminder to search CommonTrace before solving problems.
This ensures Claude sees the reminder even if session_start search returned
no results or failed silently.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from session_state import get_state_dir, increment_counter, append_event


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
