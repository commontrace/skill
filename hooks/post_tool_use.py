#!/usr/bin/env python3
"""
CommonTrace PostToolUse hook — Layer 1 state writer + knowledge detection.

This file is the dispatcher: it parses the hook payload, routes it to a
handler per tool, and prints at most one injection back to the agent. The
thinking lives in four focused modules:

  bash_result.py — did that command fail?
  detection.py   — did the agent just learn something? (4 patterns)
  retrieval.py   — cooldowns, commons search, the injection points
  resolution.py  — pairing a passing command with the error it fixed

Handlers record structural signals (errors, changes, research,
contributions) as they go; stop.py scores them at the end of the session.
"""

import json
import re
import sys
from pathlib import Path

# Defensive (Issue 9): hook payloads arrive as UTF-8 JSON on stdin, but some
# Windows consoles default stdin to cp1252 and mangle non-ASCII into mojibake
# before we parse it. Force UTF-8 with errors="replace" (root cause is likely
# the upstream harness console). Guarded — no-op on POSIX / redirected streams.
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).parent))
from bash_result import detect_bash_error
from detection import _detect_knowledge_candidates
from redact import redact_text, redact_command, is_sensitive_file
from resolution import _pair_resolution, record_surfaced
from retrieval import (
    _check_domain_entry, _check_error_recurrence, _check_pre_code,
    format_error_hits, search_on_bash_error,
)
from session_state import (
    append_event, error_signature, get_state_dir, is_config_file, log_hook_error,
    read_events, read_project_id,
)


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
        # M19/M20: Redact secrets before storing or sending
        safe_command = redact_command(command[:200])
        safe_error = redact_text(error_text[:500])
        # M19: signature computed from REDACTED text — it is stored in local.db
        sig = error_signature(redact_text(error_text))

        append_event(state_dir, "errors.jsonl", {
            "source": "bash",
            "command": safe_command,
            "output_tail": safe_error,
            "sig": sig,
        })

        # Error recurrence: record this occurrence and, if this project has
        # already resolved the same signature, inject the known fix now.
        recurrence_output = _check_error_recurrence(sig, state_dir)
        if recurrence_output:
            return recurrence_output

        # Search CommonTrace with the raw error output (let the search engine
        # handle relevance — no keyword extraction needed).
        results = search_on_bash_error(state_dir, error_text)
        if results:
            record_surfaced(state_dir, sig, results)
            return format_error_hits(results)
        return None

    # ── Success: check if this resolves a previous error ──
    previous_errors = read_events(state_dir, "errors.jsonl")
    if previous_errors:
        append_event(state_dir, "resolutions.jsonl", {
            "source": "bash",
            "command": redact_command(command[:200]),
            "output_preview": redact_text(output[:200]) if output else "",
            "errors_before": len(previous_errors),
        })
        return _pair_resolution(state_dir, command, previous_errors)

    return None


def handle_code_change(data: dict, state_dir: Path) -> dict | None:
    """Handle Write/Edit/NotebookEdit: record file changes + smart triggers."""
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return None

    file_path = tool_input.get("file_path", "")
    if not file_path:
        return None

    # M23: Skip recording changes to sensitive files entirely
    if is_sensitive_file(file_path):
        return None

    tool_name = data.get("tool_name", "")

    # Check pre-code trigger BEFORE recording change (file may not exist yet)
    trigger_output = _check_pre_code(file_path, tool_name, state_dir)

    append_event(state_dir, "changes.jsonl", {
        "tool": tool_name,
        "file": file_path,
        "is_config": is_config_file(file_path),
    })

    # Check domain entry trigger after recording
    if trigger_output is None:
        trigger_output = _check_domain_entry(file_path, state_dir)

    return trigger_output


def handle_research(data: dict, state_dir: Path) -> dict | None:
    """Handle WebSearch/WebFetch: record research event."""
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return None

    append_event(state_dir, "research.jsonl", {
        "tool": data.get("tool_name", ""),
        "query": str(tool_input.get("query", tool_input.get("url", "")))[:200],
    })

    return None


def _parse_tool_response(data: dict) -> dict | None:
    """Parse tool_response handling both dict and JSON string formats.

    MCP tool responses may arrive as dicts or as JSON-serialized strings
    depending on the Claude Code version and transport layer.
    """
    tool_response = data.get("tool_response")
    if isinstance(tool_response, dict):
        return tool_response
    if isinstance(tool_response, str):
        try:
            parsed = json.loads(tool_response)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def handle_trace_consumption(data: dict, state_dir: Path) -> None:
    """Handle get_trace: record consumption + cache trace pointer."""
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return

    trace_id = tool_input.get("trace_id", "")
    if not trace_id:
        return

    try:
        from local_store import (
            _get_conn, record_trace_consumed, mark_trace_used_v2,
            cache_trace_pointer,
        )
        session_id = state_dir.name
        project_id = read_project_id(state_dir)
        conn = _get_conn()
        record_trace_consumed(conn, session_id, trace_id)
        mark_trace_used_v2(conn, trace_id, project_id)

        # Cache trace pointer (title only — no content stored locally)
        resp = _parse_tool_response(data)
        if resp:
            title = resp.get("title", "")
            if title:
                cache_trace_pointer(conn, trace_id, project_id, title,
                                    source="search")
        conn.close()
    except Exception as e:
        log_hook_error("trace_consumption_cache", e)


def handle_contribution(data: dict, state_dir: Path) -> None:
    """Handle MCP contribute_trace: record contribution + store locally."""
    response_text = str(data.get("tool_response", {}))

    match = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}", response_text)
    trace_id = match.group(0) if match else ""

    append_event(state_dir, "contributions.jsonl", {"trace_id": trace_id})

    # Record turn count at contribution time so the Stop hook can detect
    # how many user messages came AFTER the contribution
    try:
        path = state_dir / "user_turn_count"
        count = int(path.read_text(encoding="utf-8").strip()) if path.exists() else 0
        (state_dir / "user_turns_at_contribution").write_text(
            str(count), encoding="utf-8")
    except (ValueError, OSError):
        pass

    # Cache a pointer to the contributed trace (title only — the API is the
    # source of truth). trace_id comes from the contribution RESPONSE, not
    # from tool_input.
    if not trace_id:
        return
    try:
        tool_input = data.get("tool_input", {})
        if isinstance(tool_input, dict):
            title = tool_input.get("title", "")
            if title:
                from local_store import _get_conn, cache_trace_pointer
                conn = _get_conn()
                cache_trace_pointer(conn, trace_id, read_project_id(state_dir),
                                    title, source="contributed")
                conn.close()
    except Exception as e:
        log_hook_error("contribution_cache", e)


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

    # Detect knowledge crystallization on every tool use
    _detect_knowledge_candidates(tool_name, data, state_dir)

    if tool_name == "Bash":
        output = handle_bash(data, state_dir)

    elif tool_name in ("Write", "Edit", "NotebookEdit"):
        output = handle_code_change(data, state_dir)

    elif tool_name in ("WebSearch", "WebFetch"):
        output = handle_research(data, state_dir)

    elif "get_trace" in tool_name:
        handle_trace_consumption(data, state_dir)

    elif "contribute_trace" in tool_name:
        handle_contribution(data, state_dir)

    if output:
        print(json.dumps(output))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
