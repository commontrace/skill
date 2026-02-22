#!/usr/bin/env python3
"""
CommonTrace PostToolUse hook — auto-search on errors.

Fires after every tool call. When a Bash command fails (error patterns in
output), searches CommonTrace for relevant traces and injects them as context.

Cooldown: won't search more than once per 30 seconds to avoid spam.
Only fires for Bash tool (where errors are most actionable).
"""

import json
import os
import sys
import time
from pathlib import Path


CONFIG_FILE = Path.home() / ".commontrace" / "config.json"
COOLDOWN_FILE = Path("/tmp/commontrace-search-cooldown")
COOLDOWN_SECONDS = 30
API_BASE = "https://api.commontrace.org"

# Error patterns in Bash output that suggest a real problem
ERROR_PATTERNS = [
    "error:", "Error:", "ERROR:",
    "Traceback (most recent call last)",
    "SyntaxError:", "TypeError:", "ValueError:",
    "ImportError:", "ModuleNotFoundError:",
    "FileNotFoundError:", "PermissionError:",
    "ConnectionError:", "TimeoutError:",
    "FAILED", "FAIL:",
    "npm ERR!", "npm error",
    "cargo error", "compilation error",
    "fatal:", "panic:",
    "Exception:", "Unexpected",
    "command not found",
    "No such file or directory",
    "Permission denied",
    "segmentation fault",
    "killed", "OOMKilled",
]

# Ignore patterns — these are not real errors worth searching for
IGNORE_PATTERNS = [
    "exit code 1",  # too generic
    "grep",  # grep returns exit 1 on no match
    "warning:",  # warnings are not errors
]


def load_api_key() -> str:
    """Load API key from stored config."""
    try:
        if CONFIG_FILE.exists():
            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return config.get("api_key", "")
    except (json.JSONDecodeError, OSError):
        pass
    return os.environ.get("COMMONTRACE_API_KEY", "")


def is_on_cooldown() -> bool:
    """Don't search more than once per COOLDOWN_SECONDS."""
    try:
        if COOLDOWN_FILE.exists():
            last = float(COOLDOWN_FILE.read_text(encoding="utf-8"))
            if time.time() - last < COOLDOWN_SECONDS:
                return True
    except (ValueError, OSError):
        pass
    return False


def set_cooldown() -> None:
    try:
        COOLDOWN_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def extract_error_query(output: str) -> str | None:
    """Extract a searchable query from error output."""
    lines = output.strip().split("\n")

    # Find the most informative error line
    for line in lines:
        line_stripped = line.strip()
        for pattern in ERROR_PATTERNS:
            if pattern in line_stripped:
                # Skip if it matches an ignore pattern
                if any(ignore in line_stripped.lower() for ignore in IGNORE_PATTERNS):
                    continue
                # Truncate to reasonable query length
                query = line_stripped[:200]
                return query

    return None


def search_commontrace(query: str, api_key: str) -> list[dict]:
    """Search CommonTrace for traces matching the error."""
    import urllib.error
    import urllib.request

    base_url = os.environ.get("COMMONTRACE_API_BASE_URL", API_BASE).rstrip("/")
    payload = json.dumps({"q": query, "limit": 3}).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/api/v1/traces/search",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            data = json.loads(response.read())
            return data.get("results", [])
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError):
        return []


def format_results(results: list[dict]) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        solution = r.get("solution_text", "")[:200]
        trace_id = r.get("id", "")
        lines.append(f"{i}. [{title}] — {solution}... (ID: {trace_id})")
    return "\n".join(lines)


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        data = {}

    # Only fire for Bash tool
    if data.get("tool_name") != "Bash":
        return

    # Get tool output
    tool_response = data.get("tool_response", {})
    if isinstance(tool_response, str):
        output = tool_response
    elif isinstance(tool_response, dict):
        output = tool_response.get("output", tool_response.get("stderr", ""))
    else:
        return

    if not output:
        return

    # Check for error patterns
    query = extract_error_query(output)
    if not query:
        return

    # Cooldown
    if is_on_cooldown():
        return

    # Load API key
    api_key = load_api_key()
    if not api_key:
        return

    # Search CommonTrace
    set_cooldown()
    results = search_commontrace(query, api_key)
    if not results:
        return

    # Inject as additional context
    formatted = format_results(results)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                f"CommonTrace found relevant traces for this error:\n\n"
                f"{formatted}\n\n"
                f"Use get_trace with the ID to read the full solution."
            ),
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
