"""Getting knowledge OUT of the commons and in front of the agent.

Three injection points, one shared rate-limiting policy:

  bash_error       — a command failed: search the commons with the error text
  error_recurrence — this project already solved this exact signature: replay
                     the local fix instead of searching
  pre_code         — about to Write a new source file: search for patterns
  domain_entry     — editing a language the project doesn't normally use

Each is gated by a per-trigger cooldown that adapts to the trigger's own
consumption record, with an epsilon-greedy floor so a suppressed trigger
never dies permanently.
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ct_config
from session_state import append_event, log_hook_error, read_project_id
from redact import redact_text

EXTENSION_TO_LANGUAGE = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript",
    ".jsx": "javascript", ".js": "javascript", ".go": "go",
    ".rs": "rust", ".java": "java", ".rb": "ruby",
}


# ── Cooldowns ────────────────────────────────────────────────────────────

def _cooldown_dir() -> Path:
    """Resolved at call time so tests can redirect ct_config.COOLDOWN_DIR."""
    return ct_config.COOLDOWN_DIR


def is_on_cooldown(trigger_name: str, seconds: int) -> bool:
    """Per-trigger cooldown check."""
    path = _cooldown_dir() / f"{trigger_name}.ts"
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
    _cooldown_dir().mkdir(parents=True, exist_ok=True)
    try:
        (_cooldown_dir() / f"{trigger_name}.ts").write_text(
            str(time.time()), encoding="utf-8")
    except OSError:
        pass


EXPLORATION_EVERY = 10  # every Nth suppressed check fires anyway (epsilon floor)


def _exploration_due(trigger_name: str) -> bool:
    """Deterministic epsilon-greedy floor for suppressed triggers.

    Counts suppressed-eligible checks per trigger; every
    EXPLORATION_EVERY-th check is allowed through at the base cooldown.
    Guarantees a suppressed trigger keeps sampling reality and can earn
    its way back when the corpus or the project changes — the search
    rate never decays to zero (spec §4.1).
    """
    _cooldown_dir().mkdir(parents=True, exist_ok=True)
    path = _cooldown_dir() / f"{trigger_name}.suppressed"
    try:
        count = int(path.read_text(encoding="utf-8")) if path.exists() else 0
    except (ValueError, OSError):
        count = 0
    count += 1
    try:
        path.write_text(str(count), encoding="utf-8")
    except OSError:
        return False
    return count % EXPLORATION_EVERY == 0


def _get_adaptive_cooldown(trigger_name: str, base_seconds: int,
                           state_dir: Path) -> int:
    """Scale cooldown by trigger conversion rate from trigger_feedback.

    >= 40% rate → 0.5x cooldown (more aggressive — trigger is effective)
    < 5% after 20+ firings → 3x cooldown, with an epsilon-greedy floor:
        every EXPLORATION_EVERY-th suppressed check goes through at the
        base cooldown, so suppression is never permanent.
    Default: no change

    Stats come from trigger_stats.json (written by session_start from
    get_trigger_effectiveness, key "fired"; "total" kept as a legacy
    fallback for old bridge files).
    """
    try:
        stats_path = state_dir / "trigger_stats.json"
        if not stats_path.exists():
            return base_seconds
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        trigger_data = stats.get(trigger_name)
        if not trigger_data:
            return base_seconds
        fired = trigger_data.get("fired", trigger_data.get("total", 0))
        rate = trigger_data.get("rate", 0)
        if fired >= 20 and rate < 0.05:
            if _exploration_due(trigger_name):
                return base_seconds
            return base_seconds * 3
        if rate >= 0.4:
            return max(base_seconds // 2, 5)
    except (json.JSONDecodeError, OSError, TypeError):
        pass
    return base_seconds


def _record_trigger_safe(state_dir: Path, trigger_name: str) -> None:
    """Record a trigger fire for reinforcement tracking. Never fails."""
    try:
        from local_store import _get_conn, record_trigger
        session_id = state_dir.name
        conn = _get_conn()
        record_trigger(conn, session_id, trigger_name)
        conn.close()
    except Exception as e:
        log_hook_error("record_trigger", e)


# ── Search ───────────────────────────────────────────────────────────────

def search_commontrace(query: str, api_key: str,
                       context: dict | None = None) -> list[dict]:
    body: dict = {"q": query, "limit": 3}
    if context:
        body["context"] = context
    payload = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        f"{ct_config.api_base_url()}/api/v1/traces/search",
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
            json.JSONDecodeError, OSError) as e:
        # Status-bearing network POST (domain-entry knowledge search). An empty
        # list is indistinguishable from "no matches" to the caller — the
        # silent-success trap — so a failed search silently drops the injection.
        # Log the real cause locally; still return [] so the hook proceeds.
        log_hook_error("search_commontrace", e)
        return []


def format_results(results: list[dict]) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        solution = r.get("solution_text", "")[:200]
        trace_id = r.get("id", "")
        # Contributor names are user-supplied — sanitize before display
        contributor = re.sub(
            r"[^\w\s.\-]", "", str(r.get("contributor_name") or ""))[:40].strip()
        by = f" by {contributor}" if contributor else ""
        lines.append(f"{i}. [{title}] — {solution}... (ID: {trace_id}{by})")
    return "\n".join(lines)


def _injection(text: str) -> dict:
    """Wrap injected knowledge in the PostToolUse hook output envelope."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": text,
        }
    }


def _search_injection(query: str, preamble: str) -> dict | None:
    """Search the commons and format the hit list, or None if nothing lands."""
    api_key = ct_config.load_api_key()
    if not api_key:
        return None
    results = search_commontrace(query, api_key)
    if not results:
        return None
    return _injection(
        f"{preamble}\n\n{format_results(results)}\n\n"
        f"Use get_trace with the ID to read the full solution.")


# ── Injection points ─────────────────────────────────────────────────────

def search_on_bash_error(state_dir: Path, error_text: str) -> list[dict]:
    """Cooldown-gated commons search for a failing command. Returns the hits.

    Returns the raw results rather than an injection envelope because the
    caller also has to remember which traces were surfaced for this error
    signature — see resolution.record_surfaced.
    """
    if is_on_cooldown("bash_error",
                      _get_adaptive_cooldown("bash_error", 30, state_dir)):
        return []
    api_key = ct_config.load_api_key()
    if not api_key:
        return []
    set_cooldown("bash_error")
    _record_trigger_safe(state_dir, "bash_error")
    # M19: redact before the error text leaves the machine as a search query.
    query = redact_text(error_text.strip()[-200:])
    if not query:
        return []
    return search_commontrace(query, api_key)


def format_error_hits(results: list[dict]) -> dict:
    """Envelope for commons hits found for a failing command."""
    return _injection(
        f"CommonTrace found relevant traces for this error:\n\n"
        f"{format_results(results)}\n\n"
        f"Use get_trace with the ID to read the full solution.")


def _check_error_recurrence(sig: str, state_dir: Path) -> dict | None:
    """Record this error occurrence; on resolved recurrence, inject the fix.

    Recording is exempt from the cooldown so seen_count stays accurate —
    the cooldown gates only the injection. Injection fires when this
    project has already resolved the same signature: the moment a past
    lesson pays off. The injection is informational (never an instruction
    to execute), names its provenance, and is remembered in
    recurrence_injected.jsonl so a subsequent fix counts as an assisted
    resolution (closes the trigger_feedback loop).
    """
    project_id = read_project_id(state_dir)
    if project_id is None:
        return None

    info = None
    try:
        from local_store import _get_conn, record_error_signature
        conn = _get_conn()
        info = record_error_signature(conn, project_id, sig)
        conn.close()
    except Exception as e:
        log_hook_error("error_recurrence", e)
        return None

    if not info or not info.get("recurrence") or not info.get("resolved"):
        return None

    if is_on_cooldown("error_recurrence",
                      _get_adaptive_cooldown("error_recurrence", 60, state_dir)):
        return None
    set_cooldown("error_recurrence")
    _record_trigger_safe(state_dir, "error_recurrence")
    append_event(state_dir, "recurrence_injected.jsonl", {"sig": sig})

    when = time.strftime("%Y-%m-%d",
                         time.localtime(info.get("last_seen_at", 0)))
    parts = [
        f"CommonTrace: this error has hit this project before "
        f"(seen {info['seen_count']} times, last {when}) and was solved."
    ]
    if info.get("fix_command"):
        parts.append(f"The fix was verified with: `{info['fix_command']}`.")
    files = info.get("fix_files") or []
    if files:
        parts.append("Files changed for the fix: "
                     + ", ".join(files[:5]) + ".")
    if info.get("trace_id"):
        parts.append(f"Full solution: use get_trace with ID "
                     f"{info['trace_id']}.")
    parts.append("(Source: this project's local CommonTrace history.)")
    return _injection(" ".join(parts))


def _check_pre_code(file_path: str, tool_name: str,
                    state_dir: Path = None) -> dict | None:
    """Trigger search before implementing a new file."""
    if tool_name != "Write":
        return None
    cd = _get_adaptive_cooldown("pre_code", 180, state_dir) if state_dir else 180
    if is_on_cooldown("pre_code", cd):
        return None
    if Path(file_path).exists():
        return None

    lang = EXTENSION_TO_LANGUAGE.get(Path(file_path).suffix.lower())
    if not lang:
        return None

    set_cooldown("pre_code")
    if state_dir:
        _record_trigger_safe(state_dir, "pre_code")
    name = Path(file_path).stem.lower()
    return _search_injection(
        f"{lang} {name} implementation patterns",
        f"Before implementing {Path(file_path).name}, "
        f"CommonTrace found relevant patterns:")


def _check_domain_entry(file_path: str, state_dir: Path) -> dict | None:
    """Trigger search when entering a language different from the project's."""
    if is_on_cooldown("domain_entry",
                      _get_adaptive_cooldown("domain_entry", 120, state_dir)):
        return None

    lang = EXTENSION_TO_LANGUAGE.get(Path(file_path).suffix.lower())
    if not lang:
        return None

    project_id = read_project_id(state_dir)
    if project_id is None:
        return None

    try:
        from local_store import _get_conn, get_project_context_by_id
        conn = _get_conn()
        # Resolve by the registered project_id (session cwd), NOT the edited
        # file's parent dir — files under src/, api/, lib/… would otherwise
        # miss the exact WHERE path=? lookup and never fire this pattern.
        ctx = get_project_context_by_id(conn, project_id)
        conn.close()

        # Fire when editing in a language different from the primary language
        if ctx and ctx.get("language") != lang:
            set_cooldown("domain_entry")
            _record_trigger_safe(state_dir, "domain_entry")
            return _search_injection(
                f"{lang} common patterns and gotchas",
                f"You're working in {lang} "
                f"(project primary: {ctx.get('language', 'unknown')}). "
                f"CommonTrace found relevant knowledge:")
    except Exception as e:
        log_hook_error("domain_entry", e)
    return None
