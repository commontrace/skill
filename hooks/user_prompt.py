#!/usr/bin/env python3
"""
CommonTrace UserPromptSubmit hook — Layer 1 state writer.

Increments the user turn counter in session state. This is a pure
structural signal — it counts how many times the user sent a message,
without analyzing what they said.

The Stop hook uses this to determine conversation depth.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from session_state import get_state_dir, increment_counter


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return

    state_dir = get_state_dir(data)
    increment_counter(state_dir, "user_turn_count")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
