#!/usr/bin/env python3
"""
CommonTrace PostToolUse hook — Layer 1 state writer + error search.

Handles multiple tools via the tool_name field:

Bash:
  - Detect errors via exit code / stderr (structural, no keyword lists)
  - Record to errors.jsonl or resolutions.jsonl
  - On errors, search CommonTrace with the raw output tail

Write/Edit/NotebookEdit:
  - Record file path to changes.jsonl
  - Flag config files separately (for config discovery pattern)

WebSearch/WebFetch:
  - Record research activity to research.jsonl

MCP contribute_trace:
  - Record contribution to contributions.jsonl

Error detection is STRUCTURAL: non-zero exit code or presence of stderr.
No hardcoded error keyword lists. The search query is the raw output
tail — let the search engine handle relevance.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from session_state import (
    get_state_dir, append_event, read_events, is_config_file,
)


CONFIG_FILE = Path.home() / ".commontrace" / "config.json"
API_BASE = "https://api.commontrace.org"
COOLDOWN_DIR = Path("/tmp/commontrace-cooldowns")

EXTENSION_TO_LANGUAGE = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript",
    ".jsx": "javascript", ".js": "javascript", ".go": "go",
    ".rs": "rust", ".java": "java", ".rb": "ruby",
}


def load_api_key() -> str:
    try:
        if CONFIG_FILE.exists():
            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return config.get("api_key", "")
    except (json.JSONDecodeError, OSError):
        pass
    return os.environ.get("COMMONTRACE_API_KEY", "")


def is_on_cooldown(trigger_name: str, seconds: int) -> bool:
    """Per-trigger cooldown check."""
    path = COOLDOWN_DIR / f"{trigger_name}.ts"
    try:
        if path.exists():
            last = float(path.read_text(encoding="utf-8"))
            if time.time() - last < seconds:
                return True
    except (ValueError, OSError):
        pass
    return False


def set_cooldown(trigger_name: str) -> None:
    """Set cooldown timestamp for a trigger."""
    COOLDOWN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (COOLDOWN_DIR / f"{trigger_name}.ts").write_text(
            str(time.time()), encoding="utf-8")
    except OSError:
        pass


def search_commontrace(query: str, api_key: str,
                       context: dict | None = None) -> list[dict]:
    import urllib.error
    import urllib.request

    base_url = os.environ.get("COMMONTRACE_API_BASE_URL", API_BASE).rstrip("/")
    body: dict = {"q": query, "limit": 3}
    if context:
        body["context"] = context
    payload = json.dumps(body).encode("utf-8")

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
    except (urllib.error.URLError, urllib.error.HTTPError,
            json.JSONDecodeError, OSError):
        return []


def format_results(results: list[dict]) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        solution = r.get("solution_text", "")[:200]
        trace_id = r.get("id", "")
        lines.append(f"{i}. [{title}] — {solution}... (ID: {trace_id})")
    return "\n".join(lines)


def detect_bash_error(data: dict) -> tuple[bool, str, str]:
    """Detect if a Bash command failed using structural signals only.

    Checks (in order):
    1. Exit code field in tool_response (most reliable)
    2. Presence of stderr content (structural — stderr is for errors)
    3. If tool_response is a plain string, we cannot structurally
       determine error vs success — default to not-error.

    Returns: (is_error, output_text, error_text_for_search)
    """
    tool_response = data.get("tool_response", {})

    if isinstance(tool_response, dict):
        output = tool_response.get("output", "")
        stderr = tool_response.get("stderr", "")
        exit_code = tool_response.get("exitCode",
                    tool_response.get("exit_code"))

        # Non-zero exit code is the clearest structural signal
        if exit_code is not None and exit_code != 0:
            # Use stderr if available, otherwise tail of output
            error_text = stderr if stderr else output[-500:]
            return True, output, error_text

        # Stderr with content = error (by Unix convention)
        if stderr and stderr.strip():
            return True, output, stderr[-500:]

        return False, output, ""

    if isinstance(tool_response, str):
        # Plain string — can't structurally determine error.
        # But Claude Code often includes exit code info in the string.
        # Check for non-zero exit code at the end (this is structural
        # metadata appended by Claude Code, not error message parsing).
        output = tool_response
        # Claude Code appends "exit code: N" or similar
        exit_match = re.search(r'exit\s*code[:\s]+(\d+)', output[-100:],
                               re.IGNORECASE)
        if exit_match and int(exit_match.group(1)) != 0:
            return True, output, output[-500:]

        return False, output, ""

    return False, "", ""


# ── Tool handlers ────────────────────────────────────────────────────────

def handle_bash(data: dict, state_dir: Path) -> dict | None:
    """Handle Bash tool: record errors/resolutions, search on errors."""
    tool_input = data.get("tool_input", {})
    command = ""
    if isinstance(tool_input, dict):
        command = tool_input.get("command", "")

    is_error, output, error_text = detect_bash_error(data)

    if not output and not error_text:
        return None

    if is_error:
        # ── Error: record to state + search CommonTrace ──
        append_event(state_dir, "errors.jsonl", {
            "source": "bash",
            "command": command[:200],
            "output_tail": error_text[:500],
        })

        # Check for error recurrence in local store (takes priority)
        recurrence_output = _check_error_recurrence(error_text, state_dir)
        if recurrence_output:
            return recurrence_output

        # Search CommonTrace with raw error output (let search engine
        # handle relevance — no keyword extraction needed)
        if not is_on_cooldown("bash_error", 30):
            api_key = load_api_key()
            if api_key:
                set_cooldown("bash_error")
                # Use last 200 chars as search query
                query = error_text.strip()[-200:]
                if query:
                    results = search_commontrace(query, api_key)
                    if results:
                        formatted = format_results(results)
                        return {
                            "hookSpecificOutput": {
                                "hookEventName": "PostToolUse",
                                "additionalContext": (
                                    f"CommonTrace found relevant traces "
                                    f"for this error:\n\n{formatted}\n\n"
                                    f"Use get_trace with the ID to read "
                                    f"the full solution."
                                ),
                            }
                        }
    else:
        # ── Success: check if this resolves a previous error ──
        previous_errors = read_events(state_dir, "errors.jsonl")
        if previous_errors:
            append_event(state_dir, "resolutions.jsonl", {
                "source": "bash",
                "command": command[:200],
                "output_preview": output[:200] if output else "",
                "errors_before": len(previous_errors),
            })

    return None


def handle_code_change(data: dict, state_dir: Path) -> dict | None:
    """Handle Write/Edit/NotebookEdit: record file changes + smart triggers."""
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return None

    file_path = tool_input.get("file_path", "")
    if not file_path:
        return None

    tool_name = data.get("tool_name", "")

    # Check pre-code trigger BEFORE recording change (file may not exist yet)
    trigger_output = _check_pre_code(file_path, tool_name)

    append_event(state_dir, "changes.jsonl", {
        "tool": tool_name,
        "file": file_path,
        "is_config": is_config_file(file_path),
    })

    # Check domain entry trigger after recording
    if trigger_output is None:
        trigger_output = _check_domain_entry(file_path, state_dir)

    return trigger_output


def handle_research(data: dict, state_dir: Path) -> None:
    """Handle WebSearch/WebFetch: record research activity."""
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return

    tool_name = data.get("tool_name", "")
    query = tool_input.get("query", tool_input.get("url", ""))

    append_event(state_dir, "research.jsonl", {
        "tool": tool_name,
        "query": str(query)[:200],
    })


def _read_project_id(state_dir: Path) -> int | None:
    """Read project_id bridge file written by session_start."""
    try:
        return int((state_dir / "project_id").read_text(encoding="utf-8").strip())
    except (ValueError, OSError, FileNotFoundError):
        return None


def _read_context_fingerprint(state_dir: Path) -> dict | None:
    """Read context fingerprint bridge file written by session_start."""
    try:
        return json.loads(
            (state_dir / "context_fingerprint.json").read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return None


def _check_error_recurrence(error_text: str, state_dir: Path) -> dict | None:
    """Trigger enriched search when error matches previous session patterns."""
    if is_on_cooldown("error_recurrence", 60):
        return None

    project_id = _read_project_id(state_dir)
    if project_id is None:
        return None

    try:
        from local_store import _get_conn, get_error_history
        from session_state import error_hash
        ehash = error_hash(error_text)
        conn = _get_conn()
        history = get_error_history(conn, project_id, ehash)
        conn.close()

        if history:
            set_cooldown("error_recurrence")
            api_key = load_api_key()
            if api_key:
                context = _read_context_fingerprint(state_dir)
                query = error_text.strip()[-200:]
                results = search_commontrace(query, api_key, context)
                if results:
                    formatted = format_results(results)
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PostToolUse",
                            "additionalContext": (
                                f"This error has occurred in {len(history)} "
                                f"previous session(s). CommonTrace found "
                                f"relevant traces:\n\n{formatted}\n\n"
                                f"Use get_trace with the ID to read "
                                f"the full solution."
                            ),
                        }
                    }
    except Exception:
        pass
    return None


def _check_domain_entry(file_path: str, state_dir: Path) -> dict | None:
    """Trigger search when entering an unfamiliar language domain."""
    if is_on_cooldown("domain_entry", 120):
        return None

    ext = Path(file_path).suffix.lower()
    lang = EXTENSION_TO_LANGUAGE.get(ext)
    if not lang:
        return None

    project_id = _read_project_id(state_dir)
    if project_id is None:
        return None

    try:
        from local_store import _get_conn, get_known_languages
        conn = _get_conn()
        known = get_known_languages(conn, project_id)
        conn.close()

        if lang not in known:
            set_cooldown("domain_entry")
            api_key = load_api_key()
            if api_key:
                query = f"{lang} common patterns and gotchas"
                results = search_commontrace(query, api_key)
                if results:
                    formatted = format_results(results)
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PostToolUse",
                            "additionalContext": (
                                f"You're working in {lang} for the first "
                                f"time in this project. CommonTrace found "
                                f"relevant knowledge:\n\n{formatted}\n\n"
                                f"Use get_trace with the ID to read "
                                f"the full solution."
                            ),
                        }
                    }
    except Exception:
        pass
    return None


def _check_pre_code(file_path: str, tool_name: str) -> dict | None:
    """Trigger search before implementing a new file."""
    if tool_name != "Write":
        return None
    if is_on_cooldown("pre_code", 180):
        return None
    if Path(file_path).exists():
        return None

    ext = Path(file_path).suffix.lower()
    lang = EXTENSION_TO_LANGUAGE.get(ext)
    if not lang:
        return None

    set_cooldown("pre_code")
    api_key = load_api_key()
    if api_key:
        name = Path(file_path).stem.lower()
        query = f"{lang} {name} implementation patterns"
        results = search_commontrace(query, api_key)
        if results:
            formatted = format_results(results)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"Before implementing {Path(file_path).name}, "
                        f"CommonTrace found relevant patterns:\n\n"
                        f"{formatted}\n\n"
                        f"Use get_trace with the ID to read "
                        f"the full solution."
                    ),
                }
            }
    return None


def handle_contribution(data: dict, state_dir: Path) -> None:
    """Handle MCP contribute_trace: record contribution."""
    tool_response = data.get("tool_response", {})
    response_text = str(tool_response)

    match = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}", response_text)
    trace_id = match.group(0) if match else ""

    append_event(state_dir, "contributions.jsonl", {
        "trace_id": trace_id,
    })

    # Record turn count at contribution time so Stop hook can detect
    # how many user messages came AFTER the contribution
    try:
        path = state_dir / "user_turn_count"
        count = int(path.read_text(encoding="utf-8").strip()) if path.exists() else 0
        (state_dir / "user_turns_at_contribution").write_text(
            str(count), encoding="utf-8")
    except (ValueError, OSError):
        pass


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return

    tool_name = data.get("tool_name", "")
    if not tool_name:
        return

    state_dir = get_state_dir(data)
    output = None

    if tool_name == "Bash":
        output = handle_bash(data, state_dir)

    elif tool_name in ("Write", "Edit", "NotebookEdit"):
        output = handle_code_change(data, state_dir)

    elif tool_name in ("WebSearch", "WebFetch"):
        handle_research(data, state_dir)

    elif "contribute_trace" in tool_name:
        handle_contribution(data, state_dir)

    if output:
        print(json.dumps(output))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
