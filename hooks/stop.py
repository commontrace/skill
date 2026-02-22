#!/usr/bin/env python3
"""
CommonTrace Stop hook — contribution prompt on resolution.

Fires every time Claude finishes a response. Detects when a problem was
resolved (user confirmation or Claude self-proof) and prompts to contribute.

Allows multiple contributions per session, but:
  - Never fires twice in a row (stop_hook_active guard)
  - Cooldown: won't re-prompt within 60 seconds of the last prompt
  - Dedup: won't prompt for the same resolution twice (topic hash)

Reads the last few transcript messages to detect user satisfaction signals.
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path


RESOLUTION_DIR = Path("/tmp/commontrace-resolutions")

# Claude's own signals that a problem was solved
ASSISTANT_SIGNALS = [
    "fixed", "solved", "resolved", "working now", "works now",
    "that fixes", "that resolves", "issue is resolved",
    "tests pass", "all tests pass", "build succeeds",
    "successfully", "the fix was", "root cause was",
    "the problem was", "the issue was",
]

# User satisfaction signals (checked from transcript)
USER_CONFIRMATION = [
    "that works", "it works", "works now", "nice", "perfect",
    "thanks", "thank you", "great", "awesome", "good job",
    "exactly", "yes", "yep", "correct", "that's it",
    "fixed", "solved", "finally",
]

# Skip signals — user is still unhappy
USER_REJECTION = [
    "no", "wrong", "not working", "still broken", "nope",
    "that's not", "doesn't work", "didn't work", "try again",
    "still failing", "same error", "not right",
]

COOLDOWN_SECONDS = 60


def get_session_key(data: dict) -> str:
    session_id = data.get("session_id")
    return str(session_id) if session_id else str(os.getppid())


def read_last_user_messages(transcript_path: str, count: int = 3) -> list[str]:
    """Read the last N user messages from the transcript JSONL file."""
    messages = []
    try:
        path = Path(transcript_path)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("role") == "user":
                    content = entry.get("content", "")
                    if isinstance(content, list):
                        # Extract text from content blocks
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                messages.append(block["text"])
                    elif isinstance(content, str):
                        messages.append(content)
                    if len(messages) >= count:
                        break
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return messages


def has_resolution_signal(assistant_msg: str, user_messages: list[str]) -> bool:
    """Detect if a problem was just resolved."""
    lowered_assistant = assistant_msg.lower()

    # Check if user rejected (takes priority)
    for msg in user_messages[:1]:  # Only check most recent user message
        lowered = msg.lower()
        for pattern in USER_REJECTION:
            if pattern in lowered:
                return False

    # Check user confirmation
    for msg in user_messages[:2]:  # Check last 2 user messages
        lowered = msg.lower()
        for pattern in USER_CONFIRMATION:
            if pattern in lowered:
                return True

    # Check Claude's own completion signals
    words = set(lowered_assistant.split())
    for signal in ASSISTANT_SIGNALS:
        if " " in signal:
            if signal in lowered_assistant:
                return True
        elif signal in words:
            return True

    return False


def topic_hash(message: str) -> str:
    """Hash the first 200 chars of Claude's message for dedup."""
    return hashlib.md5(message[:200].encode()).hexdigest()[:12]


def was_recently_prompted(session_key: str) -> bool:
    """Check cooldown — don't prompt within COOLDOWN_SECONDS of last prompt."""
    cooldown_file = RESOLUTION_DIR / f"cooldown-{session_key}"
    if cooldown_file.exists():
        try:
            last_time = float(cooldown_file.read_text(encoding="utf-8"))
            if time.time() - last_time < COOLDOWN_SECONDS:
                return True
        except (ValueError, OSError):
            pass
    return False


def was_already_prompted_for(session_key: str, msg_hash: str) -> bool:
    """Check if we already prompted for this exact resolution."""
    dedup_file = RESOLUTION_DIR / f"dedup-{session_key}-{msg_hash}"
    return dedup_file.exists()


def mark_prompted(session_key: str, msg_hash: str) -> None:
    """Record that we prompted, for cooldown and dedup."""
    RESOLUTION_DIR.mkdir(parents=True, exist_ok=True)
    cooldown_file = RESOLUTION_DIR / f"cooldown-{session_key}"
    dedup_file = RESOLUTION_DIR / f"dedup-{session_key}-{msg_hash}"
    try:
        cooldown_file.write_text(str(time.time()), encoding="utf-8")
        dedup_file.write_text("1", encoding="utf-8")
    except OSError:
        pass


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        data = {}

    # Loop guard — never fire twice in a row
    if data.get("stop_hook_active", False):
        return

    session_key = get_session_key(data)
    assistant_msg = data.get("last_assistant_message", "")
    if not assistant_msg:
        return

    # Cooldown — don't spam
    if was_recently_prompted(session_key):
        return

    # Dedup — don't prompt for the same fix twice
    msg_hash = topic_hash(assistant_msg)
    if was_already_prompted_for(session_key, msg_hash):
        return

    # Read recent user messages from transcript
    transcript_path = data.get("transcript_path", "")
    user_messages = read_last_user_messages(transcript_path) if transcript_path else []

    # Detect resolution
    if not has_resolution_signal(assistant_msg, user_messages):
        return

    # Mark as prompted (cooldown + dedup)
    mark_prompted(session_key, msg_hash)

    output = {
        "decision": "block",
        "reason": (
            "It looks like you just solved a problem. "
            "Would you like to contribute this solution to the CommonTrace knowledge base "
            "so other AI agents can learn from it? "
            "Use the contribute_trace tool to submit it, or say 'skip' to continue."
        ),
    }
    print(json.dumps(output))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
