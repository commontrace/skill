#!/usr/bin/env python3
"""
CommonTrace Stop hook.

After a task completes, prompts the agent to consider contributing to CommonTrace.

Loop prevention:
  1. Checks stop_hook_active first — exits 0 immediately if true.
  2. Checks a session-scoped flag file — exits 0 if already prompted this session.

Fires at most once per session, only when completion signals are detected.
Exits 0 silently on any error — never prevents Claude from stopping.
"""

import json
import os
import sys
from pathlib import Path


COMPLETION_SIGNALS = [
    "completed", "done", "finished", "implemented", "fixed",
    "solved", "resolved", "deployed", "successfully",
]

COMPLETION_PATTERNS = [
    "task is complete", "changes are ready", "commit created",
    "all tests pass", "tests pass", "working now",
]

FLAG_DIR = Path("/tmp")


def get_session_key(data: dict) -> str:
    """
    Derive a stable session key from stdin data.

    Prefer session_id from the hook payload (stable across stop invocations
    in the same session). Fall back to the parent PID (os.getppid()) which
    is stable within a Claude session. Never use os.getpid() — each hook
    invocation spawns a fresh Python process with a unique PID.
    """
    session_id = data.get("session_id")
    if session_id:
        return str(session_id)
    return str(os.getppid())


def has_completion_signal(message: str) -> bool:
    """
    Return True if last_assistant_message contains a task completion signal.

    Signal words are matched at word boundaries (not substrings) via word list.
    Patterns are matched as substrings of the lowercased message.
    """
    lowered = message.lower()

    # Word boundary check: split into words, look for signal words
    words = set(lowered.split())
    for signal in COMPLETION_SIGNALS:
        if signal in words:
            return True

    # Substring check for multi-word patterns
    for pattern in COMPLETION_PATTERNS:
        if pattern in lowered:
            return True

    return False


def main() -> None:
    # Read stdin
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        data = {}

    # LOOP PREVENTION 1: stop_hook_active guard
    if data.get("stop_hook_active", False):
        return

    # LOOP PREVENTION 2: session flag file guard
    session_key = get_session_key(data)
    flag_file = FLAG_DIR / f"commontrace-prompted-{session_key}"
    if flag_file.exists():
        return

    # Check for task completion signals
    last_message = data.get("last_assistant_message", "")
    if not last_message or not has_completion_signal(last_message):
        return

    # Write flag file to prevent re-prompting this session
    try:
        import time
        flag_file.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        # If we can't write the flag, skip prompting to avoid repeated prompts
        return

    # Output contribution prompt
    output = {
        "decision": "block",
        "reason": (
            "Before we wrap up: if you just solved a problem that other agents might face, "
            "consider sharing it with the CommonTrace knowledge base. "
            "Use /trace:contribute to start the contribution flow, or just say 'skip' to finish."
        ),
    }
    print(json.dumps(output))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never prevent Claude from stopping — silently exit 0
        pass
