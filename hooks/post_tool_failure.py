#!/usr/bin/env python3
"""
CommonTrace PostToolUseFailure hook — Layer 1 state writer.

Records any tool failure to session state. This captures when Claude's
tool calls were rejected or errored at the system level (distinct from
Bash commands that return errors in their output).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from session_state import get_state_dir, append_event
from redact import redact_command, redact_text, strip_harness_noise


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return

    tool_name = data.get("tool_name", "")
    error = data.get("error", "")
    if not tool_name or not error:
        return

    state_dir = get_state_dir(data)

    tool_input = data.get("tool_input", {})
    # errors.jsonl feeds the contribution payload, so tool-failure text must
    # be scrubbed here just like Bash errors are in post_tool_use. REDACT
    # BEFORE TRUNCATING — truncation can slice a secret so a later redaction
    # pass no longer recognises it. strip_harness_noise drops harness lines
    # ("Shell cwd was reset to /home/<user>/…") that leak absolute paths.
    append_event(state_dir, "errors.jsonl", {
        "source": "tool_failure",
        "tool": tool_name,
        "error": strip_harness_noise(redact_text(str(error)))[:500],
        "input_summary": redact_command(str(tool_input))[:200],
    })


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
