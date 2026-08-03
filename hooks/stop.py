#!/usr/bin/env python3
"""
CommonTrace Stop hook — end-of-session decision point.

This file decides WHETHER to ask for a contribution and HOW to deliver the
ask. The work behind that decision lives in three modules:

  scoring.py        — how much was this session worth? (5 patterns)
  candidate.py      — the contribution draft + the agent-facing directive
  session_report.py — local stats, savings ledger, opt-in telemetry

Delivery is either a Stop `decision: block` (the agent authors the trace
with real content) or, if that can't be built, the durable pending queue
that `/trace` reads later. One ask per session, enforced by a marker file.
"""

import json
import os
import sys
import time
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

# Import guard — see post_tool_use.py. A partial update must degrade to a
# silent no-op, not a traceback at the end of every session.
try:
    import ct_config
    from candidate import _build_candidate, _contribution_directive
    from scoring import IMPORTANCE_THRESHOLD, compute_importance
    from session_report import _book_savings, _persist_session, _report_trigger_stats
    from session_state import get_state_dir, read_events, read_counter, log_hook_error
except ImportError:
    sys.exit(0)


RESOLUTION_DIR = Path.home() / ".commontrace" / "resolutions"
PENDING_DIR = Path.home() / ".commontrace" / "pending"


def _write_pending(session_key: str, payload: dict) -> None:
    """Append pending candidate for later user-driven review via /trace contribute.

    Used in manual mode (auto_contribute=false). The slash command reads these
    files and walks the user through approval via AskUserQuestion.
    """
    try:
        PENDING_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = PENDING_DIR / f"{session_key}.jsonl"
        payload.setdefault("t", time.time())
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as e:
        # Status-bearing: this is the durable fallback that keeps a scored
        # contribution candidate (and amend prompts) from being lost when the
        # directive path can't run. A silent write failure means the candidate
        # vanishes with no signal — log it locally (behavior unchanged).
        log_hook_error("write_pending", e)


def _struggle_artifact(candidate, state_dir, trace_id=""):
    """Write the Wordle-style struggle line for this session's knowledge.

    Aggregate shape only — built from event timestamps and counts, never
    from error text or file names. Never raises (artifacts must not be
    able to break the Stop hook).
    """
    try:
        from artifacts import struggle_grid, struggle_line, write_artifact
        errors = read_events(state_dir, "errors.jsonl")
        changes = read_events(state_dir, "changes.jsonl")
        meta = candidate.get("metadata_json") or {}
        grid = struggle_grid([e.get("t", 0) for e in errors],
                             [c.get("t", 0) for c in changes], resolved=True)
        line = struggle_line(grid, meta.get("time_to_resolution_minutes", 0),
                             meta.get("error_count", 0), trace_id=trace_id)
        write_artifact("last-struggle.txt", line + "\n")
        return line
    except Exception:
        # Intentionally silent: the struggle grid is a cosmetic artifact. If it
        # can't render there is nothing to diagnose and nothing is lost — the
        # contribution flow does not depend on it. Not a status-bearing swallow.
        return None


def get_session_key(data: dict) -> str:
    session_id = data.get("session_id")
    return str(session_id) if session_id else str(os.getppid())


def _marker_path(session_key: str, kind: str, sub: str = "") -> Path:
    name = f"prompted-{kind}-{session_key}"
    if sub:
        name += f"-{sub}"
    return RESOLUTION_DIR / name


def already_prompted(session_key: str, kind: str, sub: str = "") -> bool:
    """One prompt per (session, kind, sub). Prevents re-nagging across turns
    as score bumps or turns_since increment."""
    return _marker_path(session_key, kind, sub).exists()


def mark_prompted(session_key: str, kind: str, sub: str = "") -> None:
    RESOLUTION_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _marker_path(session_key, kind, sub).write_text("1", encoding="utf-8")
    except OSError:
        pass


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        data = {}

    if data.get("stop_hook_active", False):
        return

    session_key = get_session_key(data)
    state_dir = get_state_dir(data)

    # Persist session data to SQLite
    _persist_session(data, state_dir)

    # Book measured-inbound savings (best-effort; never crashes the hook)
    _book_savings(data, state_dir)

    # Report trigger stats (best-effort)
    _report_trigger_stats(data, state_dir)

    # Check for post-contribution refinement first
    contributions = read_events(state_dir, "contributions.jsonl")
    user_turns = read_counter(state_dir, "user_turn_count")
    turns_at_contribution = 0
    try:
        path = state_dir / "user_turns_at_contribution"
        if path.exists():
            turns_at_contribution = int(
                path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        pass

    # auto_contribute (default False): contribute silently vs. ask the user.
    auto_mode = ct_config.read_config().get("auto_contribute", False)

    if contributions and user_turns > turns_at_contribution:
        trace_id = contributions[-1].get("trace_id", "")
        if not already_prompted(session_key, "amend", trace_id or "any"):
            mark_prompted(session_key, "amend", trace_id or "any")
            # Amend suggestions never auto-submit — they always need human
            # judgment about what to add. Write to pending regardless of mode.
            _write_pending(session_key, {
                "kind": "amend",
                "session_id": data.get("session_id", ""),
                "cwd": data.get("cwd", ""),
                "trace_id": trace_id,
                "title": f"Amend trace {trace_id[:8]}" if trace_id else "Amend last trace",
                "human_prompt": (
                    "You contributed a trace earlier and the conversation "
                    "continued. The trace may benefit from additional context. "
                    f"Use amend_trace to update it"
                    f"{f' (ID: {trace_id})' if trace_id else ''}."
                ),
            })
            return

    # Compute importance score
    effectiveness = None
    try:
        from local_store import _get_conn, get_trigger_effectiveness
        pid_path = state_dir / "project_id"
        if pid_path.exists():
            project_id = int(pid_path.read_text(encoding="utf-8").strip())
            conn = _get_conn()
            try:
                effectiveness = get_trigger_effectiveness(conn, project_id)
            finally:
                conn.close()
    except Exception as e:
        log_hook_error("reinforcement_effectiveness", e)
        effectiveness = None
    score, top_pattern, top_evidence = compute_importance(state_dir, effectiveness)

    if score < IMPORTANCE_THRESHOLD:
        return

    if already_prompted(session_key, "score"):
        return

    mark_prompted(session_key, "score")
    candidate = _build_candidate(score, top_pattern, top_evidence, state_dir, transcript_path=data.get("transcript_path", ""))
    _struggle_artifact(candidate, state_dir)

    # Hand the candidate to the agent to author REAL content. The hook can only
    # synthesize the mechanical journey template (a husk) — no LLM in hooks — so
    # rather than silently POST that husk (the jam that stalled auto-contribute),
    # block the Stop and let the agent contribute (auto) or prompt (manual) with
    # real content. This is what makes a contribution visible even in full-auto.
    # `stop_hook_active` + the `already_prompted` marker keep it to one fire per
    # session.
    directive = _contribution_directive(
        candidate, auto_mode, str(Path(__file__).parent))
    if directive:
        print(json.dumps({"decision": "block", "reason": directive}))
        return

    # Fallback (directive build failed): keep the durable pending record so
    # nothing is lost and /trace can still surface it.
    line = _struggle_artifact(candidate, state_dir)
    _write_pending(session_key, {
        "kind": "score",
        "session_id": data.get("session_id", ""),
        "cwd": data.get("cwd", ""),
        **({"struggle_grid": line} if line else {}),
        **candidate,
    })


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
