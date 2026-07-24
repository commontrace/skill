#!/usr/bin/env python3
"""
CommonTrace UserPromptSubmit hook — Layer 1 state writer + first-turn nudge.

Increments the user turn counter in session state. On the first user turn,
injects a brief reminder to search CommonTrace before solving problems.

Also detects structural emphasis markers in user prompts — ALL CAPS ratio,
exclamation density, and emphasis keywords — to capture "user trauma"
signals. When a user emphasizes something strongly, that knowledge is
remembered more intensely (like emotional memory in humans).

Optionally (config-off by default) fires an auto-contribute directive when the
user structurally signals they are moving on and a fix-candidate exists this
session — see auto_contribute.py for the deterministic, no-LLM fire condition.
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from session_state import get_state_dir, increment_counter, append_event, read_events
from auto_contribute import should_fire_contribution, MOVE_ON_PATTERNS


# Keywords that signal the user considers this important (case-insensitive)
_EMPHASIS_KEYWORDS = {
    "important", "critical", "crucial", "essential", "must", "never",
    "always", "remember", "urgent", "dangerous", "careful", "warning",
    "beware", "priority", "vital", "mandatory",
}

# Minimum word count to compute meaningful ratios
_MIN_WORDS = 4


def detect_emphasis(prompt: str) -> dict | None:
    """Detect structural emphasis markers in user prompt text.

    Purely structural — no NLU, no sentiment analysis. Detects:
    1. ALL CAPS word ratio (excludes short words and acronyms <= 3 chars)
    2. Exclamation mark density (per sentence)
    3. Emphasis keyword presence

    Returns emphasis dict if any signal detected, None otherwise.
    """
    if not prompt or len(prompt.strip()) < 10:
        return None

    words = prompt.split()
    if len(words) < _MIN_WORDS:
        return None

    # 1. ALL CAPS ratio — words >= 4 chars that are fully uppercase
    #    Excludes short acronyms (API, URL, CSS) and common all-caps
    #    identifiers (README, TODO, FIXME, CHANGELOG) which are normal
    _NORMAL_CAPS = {"README", "TODO", "FIXME", "NOTE", "HACK", "CHANGELOG",
                    "LICENSE", "HTTPS", "HTTP", "JSON", "HTML", "NULL",
                    "TRUE", "FALSE", "NONE", "YAML", "TOML"}
    long_words = [w for w in words if len(w) >= 4 and w.isalpha()]
    caps_words = [w for w in long_words if w.isupper() and w not in _NORMAL_CAPS]
    caps_ratio = len(caps_words) / max(len(long_words), 1)

    # 2. Exclamation density — count of ! relative to sentence count
    exclamation_count = prompt.count("!")
    # Rough sentence count: split on .!? but minimum 1
    sentence_count = max(1, len(re.split(r'[.!?]+', prompt.strip())) - 1)
    exclamation_density = exclamation_count / sentence_count

    # 3. Emphasis keywords (case-insensitive, whole words)
    prompt_lower = prompt.lower()
    found_keywords = [
        kw for kw in _EMPHASIS_KEYWORDS
        if re.search(rf'\b{kw}\b', prompt_lower)
    ]

    # Compute emphasis score (0.0-1.0)
    score = 0.0

    # Caps: 10%+ of long words in ALL CAPS is unusual emphasis
    if caps_ratio >= 0.1:
        score += min(0.4, caps_ratio)

    # Exclamations: > 0.5 per sentence is emphatic
    if exclamation_density > 0.5:
        score += min(0.3, exclamation_density * 0.15)

    # Keywords: each keyword adds signal
    if found_keywords:
        score += min(0.4, len(found_keywords) * 0.1)

    if score < 0.1:
        return None

    return {
        "emphasis_score": round(min(1.0, score), 2),
        "caps_ratio": round(caps_ratio, 2),
        "exclamation_density": round(exclamation_density, 2),
        "keywords": found_keywords[:5],
    }


# ── Auto-contribute on transition (gated, config-off by default) ──────────
#
# When the user structurally signals "moving on" and a fix-candidate exists
# this session, inject a directive telling the agent to contribute the
# just-learned fix as a NON-BLOCKING background handoff — identical to the
# /trace command. All detection is structural (regex/substring); no LLM/NLU.
# Ships OFF by default: the existing Stop-prompt flow is unchanged for real
# users. The demo env turns it on via CT_AUTO_CONTRIBUTE_ON_MOVE_ON=1.

CONFIG_FILE = Path.home() / ".commontrace" / "config.json"

# Env values that count as "on" for the enable flag.
_ENV_TRUTHY = {"1", "true", "yes", "on"}

# candidates.jsonl patterns that represent a genuine, contribution-worthy fix
# (i.e. a real solved problem worth sharing). Weak breadth-only signals like
# cross_file_breadth are excluded so the trigger stays high-precision.
CONTRIBUTION_WORTHY_PATTERNS = {
    "error_resolution", "fail_then_succeed", "test_fix_cycle",
    "config_discovery", "dependency_resolution", "security_hardening",
    "infra_discovery", "approach_reversal", "user_correction",
    "migration_pattern", "research_then_implement",
}

# Per-session one-shot marker (a file in the session state dir).
_CONTRIBUTED_MARKER = "auto_contributed"

# Stable sentinel so the directive is unambiguously identifiable (tests +
# the agent) even though it shares the additionalContext channel with the
# first-turn /recall nudge.
_DIRECTIVE_SENTINEL = "[CommonTrace auto-contribute]"

# The contribution directive. Reuses the /trace instant-handoff instruction
# verbatim (spawn ONE background subagent that authors the trace from THIS
# session's real fix, POSTs it, and renders the ⬡ receipt), wrapped so it is
# non-blocking and the main thread proceeds straight to the next plan task.
_AUTO_CONTRIBUTE_DIRECTIVE = (
    _DIRECTIVE_SENTINEL + " CommonTrace detected a contribution-worthy fix in "
    "THIS session. Contribute it now as a NON-BLOCKING background handoff, "
    "exactly like the /trace command — do NOT do any of this in the main "
    "thread and do NOT let it delay the next task.\n"
    "Main thread: your ONLY action is to immediately spawn ONE subagent with "
    "the Task tool using run_in_background: true and model: sonnet, passing "
    "the task block below verbatim. Then print the single line "
    "`Contributing in the background…` and continue straight to the next task "
    "in the plan. Do NOT draft, read files, run Bash, or narrate in the main "
    "thread. When the subagent finishes, print its returned ⬡ receipt verbatim "
    "(nothing else); if it returns `Nothing to contribute.`, print nothing.\n"
    "\n"
    "--- Subagent task (pass verbatim) ---\n"
    "Contribute one trace to CommonTrace. Work silently; return only the "
    "receipt from the last step.\n"
    "1. Find THIS session's work: the most-recently-modified directory under "
    "~/.commontrace/sessions/ — read its errors.jsonl, changes.jsonl, "
    "resolutions.jsonl, candidates.jsonl. For real prose, tail the newest "
    "*.jsonl transcript under ~/.claude/projects/* (last ~200 lines only).\n"
    "2. Draft ONLY from work actually done in THIS session. Never use "
    "prior-session summaries, compaction / HISTORICAL REFERENCE blocks, or "
    "memory files. Never include secrets, credentials, or PII. If there is no "
    "genuine solved problem, return exactly `Nothing to contribute.` and stop. "
    "Produce title, where (key file/service), context_text (the real "
    "problem), solution_text (what actually fixed it), tags[], and rough "
    "minutes / errors / tokens.\n"
    "3. Read the api_key from ~/.commontrace/config.json and POST the trace to "
    "https://api.commontrace.org/api/v1/traces with header \"X-API-Key: "
    "<key>\" and a JSON body of title, context_text, solution_text, tags, and "
    "metadata_json={\"detection_pattern\":\"auto_move_on\","
    "\"time_to_resolution_minutes\":<m>,\"error_count\":<e>,"
    "\"tokens_to_resolution\":<t>}. Parse the returned id.\n"
    "4. Render the receipt (resolve $H as in /trace, "
    "\"${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/hooks}\"): "
    "python3 \"$H/artifacts.py\" banner mode=contributed title=\"<title>\" "
    "where=\"<where>\" minutes=<m> errors=<e> tokens=<t> id=\"$ID\".\n"
    "5. Return ONLY the rendered ⬡ receipt, then a final line "
    "`→ https://commontrace.org/t/<id>`. If the POST returns no id, return "
    "only `CommonTrace error: <response body>`."
)


def _read_config() -> dict:
    """Read ~/.commontrace/config.json. Returns {} on any failure."""
    try:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _auto_contribute_enabled(config: dict) -> bool:
    """True if the feature is enabled via env or config. Default: False.

    The demo env sets CT_AUTO_CONTRIBUTE_ON_MOVE_ON=1; config may set
    auto_contribute_on_move_on: true. Env wins.
    """
    env = os.environ.get("CT_AUTO_CONTRIBUTE_ON_MOVE_ON", "").strip().lower()
    if env in _ENV_TRUTHY:
        return True
    return config.get("auto_contribute_on_move_on") is True


def _resolve_patterns(config: dict):
    """Move-on patterns from config if a valid list of strings, else default."""
    patterns = config.get("move_on_patterns")
    if isinstance(patterns, list) and patterns and all(
        isinstance(p, str) for p in patterns
    ):
        return patterns
    return MOVE_ON_PATTERNS


def _has_fix_candidate(state_dir: Path) -> bool:
    """True if a contribution-worthy fix candidate exists this session."""
    for c in read_events(state_dir, "candidates.jsonl"):
        if c.get("pattern") in CONTRIBUTION_WORTHY_PATTERNS:
            return True
    return False


def _already_contributed(state_dir: Path) -> bool:
    """True once the auto-contribute directive has fired this session."""
    return (state_dir / _CONTRIBUTED_MARKER).exists()


def _mark_contributed(state_dir: Path) -> None:
    """Set the per-session one-shot flag so the directive can't re-fire."""
    try:
        (state_dir / _CONTRIBUTED_MARKER).write_text("1", encoding="utf-8")
    except OSError:
        pass


def _maybe_auto_contribute_directive(state_dir: Path, prompt: str):
    """Return the contribution directive if the fire condition is met, else None.

    Pure structural gate (see auto_contribute.should_fire_contribution): no
    LLM, no NLU. Sets the one-shot flag as a side effect when it fires.
    """
    config = _read_config()
    if not should_fire_contribution(
        enabled=_auto_contribute_enabled(config),
        message=prompt,
        has_candidate=_has_fix_candidate(state_dir),
        already_contributed=_already_contributed(state_dir),
        patterns=_resolve_patterns(config),
    ):
        return None
    _mark_contributed(state_dir)
    return _AUTO_CONTRIBUTE_DIRECTIVE


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

    # Detect structural emphasis in user prompt
    prompt = data.get("prompt", "")
    if prompt:
        emphasis = detect_emphasis(prompt)
        if emphasis:
            append_event(state_dir, "emphasis.jsonl", emphasis)

    context_parts = []

    # First turn: inject a brief nudge to search CommonTrace
    if count == 1:
        context_parts.append(
            "Reminder: search CommonTrace with /recall before "
            "solving coding problems. Contribute with /trace after solving."
        )

    # Auto-contribute on transition (gated, config-off by default)
    directive = _maybe_auto_contribute_directive(state_dir, prompt)
    if directive:
        context_parts.append(directive)

    if context_parts:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "\n\n".join(context_parts),
            }
        }
        print(json.dumps(output))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
