#!/usr/bin/env python3
"""
CommonTrace Stop hook — Layer 2 pattern recognition.

Reads accumulated state from Layer 1 hooks and detects knowledge
creation patterns. No keyword matching on human messages. No NLU.

The state files are STRUCTURAL signals written by other hooks:
  errors.jsonl      — Bash errors, tool failures (timestamps, hashes)
  resolutions.jsonl — successful Bash runs after errors
  changes.jsonl     — files modified (paths, config flag)
  research.jsonl    — WebSearch/WebFetch activity
  contributions.jsonl — traces already contributed this session
  user_turn_count   — how many real user messages
  user_turns_at_contribution — turn count when last trace was contributed

Detected patterns (all structural):
  Error Resolution:    errors + changes + resolutions
  Workaround:          errors + research + changes
  Config Discovery:    config file changed + errors existed
  Deep Iteration:      same file changed multiple times
  Post-Contribution:   contribution + more user turns after

Guards: stop_hook_active, topic-hash dedup (no time cooldown — dedup
is sufficient, and a cooldown would block legitimate new patterns).
"""

import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from session_state import get_state_dir, read_events, read_counter


RESOLUTION_DIR = Path("/tmp/commontrace-resolutions")
MIN_TURNS = 3


def get_session_key(data: dict) -> str:
    session_id = data.get("session_id")
    return str(session_id) if session_id else str(os.getppid())


def pattern_dedup_key(pattern_name: str, evidence: dict) -> str:
    """Stable hash based on pattern + evidence, not message content.

    This ensures dedup works even when the assistant message changes
    between responses -- the pattern and its evidence are what matter.
    """
    key = pattern_name + ":" + json.dumps(evidence, sort_keys=True, default=str)
    return hashlib.md5(key.encode()).hexdigest()[:12]


def was_already_prompted_for(session_key: str, dedup_key: str) -> bool:
    return (RESOLUTION_DIR / f"dedup-{session_key}-{dedup_key}").exists()


def mark_prompted(session_key: str, dedup_key: str) -> None:
    RESOLUTION_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (RESOLUTION_DIR / f"dedup-{session_key}-{dedup_key}").write_text(
            "1", encoding="utf-8")
    except OSError:
        pass


def detect_patterns(state_dir: Path) -> dict:
    """Read all Layer 1 state and detect structural patterns.

    Returns dict with pattern names and their evidence.
    """
    errors = read_events(state_dir, "errors.jsonl")
    resolutions = read_events(state_dir, "resolutions.jsonl")
    changes = read_events(state_dir, "changes.jsonl")
    research = read_events(state_dir, "research.jsonl")
    contributions = read_events(state_dir, "contributions.jsonl")
    user_turns = read_counter(state_dir, "user_turn_count")

    # Turns at last contribution (to detect post-contribution messages)
    turns_at_contribution = 0
    try:
        path = state_dir / "user_turns_at_contribution"
        if path.exists():
            turns_at_contribution = int(
                path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        pass

    # Config changes
    config_changes = [c for c in changes if c.get("is_config")]

    # File edit frequency (same file changed multiple times = iteration)
    file_counts = Counter(c.get("file", "") for c in changes)
    iterated_files = {f for f, count in file_counts.items() if count >= 2}

    patterns = {}

    # ── Error Resolution: errors + code changes + verification ──
    if errors and changes and resolutions:
        # Check temporal order: error before change before resolution
        first_error_t = min(e.get("t", 0) for e in errors)
        last_change_t = max(c.get("t", 0) for c in changes)
        last_resolution_t = max(r.get("t", 0) for r in resolutions)

        if first_error_t < last_change_t <= last_resolution_t:
            patterns["error_resolution"] = {
                "errors": len(errors),
                "changes": len(changes),
                "resolutions": len(resolutions),
            }

    # ── Workaround: errors + research + changes + VERIFICATION ──
    # Research alone doesn't mean the fix works. Require either a
    # resolution (Bash succeeded) or that the conversation progressed
    # without new errors after the last change.
    if errors and research and changes:
        last_change_t = max(c.get("t", 0) for c in changes)
        verified = False

        # Explicit verification: resolution exists after changes
        if resolutions:
            last_resolution_t = max(r.get("t", 0) for r in resolutions)
            if last_resolution_t >= last_change_t:
                verified = True

        # Implicit verification: no new errors after last change
        # AND conversation continued (user didn't just disappear)
        if not verified:
            errors_after_change = [
                e for e in errors if e.get("t", 0) > last_change_t
            ]
            if not errors_after_change and user_turns >= MIN_TURNS:
                verified = True

        if verified:
            patterns["workaround"] = {
                "errors": len(errors),
                "research_queries": len(research),
                "changes": len(changes),
                "verified_by": "resolution" if resolutions else "no_new_errors",
            }

    # ── Config Discovery: config change + errors + VERIFICATION ──
    # Same principle: changing a config doesn't mean it fixed anything.
    if config_changes and errors:
        first_error_t = min(e.get("t", 0) for e in errors)
        config_after_error = [
            c for c in config_changes if c.get("t", 0) > first_error_t
        ]
        if config_after_error:
            last_config_t = max(c.get("t", 0) for c in config_after_error)
            verified = False

            if resolutions:
                last_resolution_t = max(r.get("t", 0) for r in resolutions)
                if last_resolution_t >= last_config_t:
                    verified = True

            if not verified:
                errors_after_config = [
                    e for e in errors if e.get("t", 0) > last_config_t
                ]
                if not errors_after_config and user_turns >= MIN_TURNS:
                    verified = True

            if verified:
                patterns["config_discovery"] = {
                    "config_files": [c.get("file") for c in config_after_error],
                    "errors": len(errors),
                }

    # ── Deep Iteration: same file edited multiple times ──
    if iterated_files and user_turns >= 2:
        patterns["iteration"] = {
            "iterated_files": list(iterated_files),
            "max_edits": max(file_counts.values()),
        }

    # ── Multi-turn with changes (catch-all for substantial work) ──
    if user_turns >= MIN_TURNS and changes:
        patterns["multi_turn_work"] = {
            "user_turns": user_turns,
            "changes": len(changes),
        }

    # ── Post-contribution refinement ──
    if contributions and user_turns > turns_at_contribution:
        latest_contribution = contributions[-1]
        patterns["post_contribution"] = {
            "trace_id": latest_contribution.get("trace_id", ""),
            "turns_since": user_turns - turns_at_contribution,
        }

    return patterns


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
    patterns = detect_patterns(state_dir)

    if not patterns:
        return

    # Pick highest-priority pattern and build dedup key from it
    priority = [
        "post_contribution", "error_resolution", "workaround",
        "config_discovery", "iteration", "multi_turn_work",
    ]
    for name in priority:
        if name in patterns:
            dedup_key = pattern_dedup_key(name, patterns[name])
            if was_already_prompted_for(session_key, dedup_key):
                return
            break
    else:
        return

    # ── Post-contribution refinement takes priority ──
    if "post_contribution" in patterns:
        trace_id = patterns["post_contribution"].get("trace_id", "")
        mark_prompted(session_key, dedup_key)
        print(json.dumps({
            "decision": "block",
            "reason": (
                "You contributed a trace earlier in this session and the "
                "conversation has continued since then. The trace may "
                "benefit from the additional context. Use amend_trace "
                f"to update it{f' (ID: {trace_id})' if trace_id else ''}, "
                "or say 'skip' to continue."
            ),
        }))
        return

    # ── Error resolution is highest-confidence pattern ──
    if "error_resolution" in patterns:
        p = patterns["error_resolution"]
        mark_prompted(session_key, dedup_key)
        print(json.dumps({
            "decision": "block",
            "reason": (
                f"You went through an error→fix→verify cycle "
                f"({p['errors']} error(s), {p['changes']} change(s), "
                f"{p['resolutions']} verification(s)). This looks like "
                f"a solved problem. Would you like to contribute this "
                f"solution to CommonTrace? Use contribute_trace to "
                f"submit, or say 'skip'."
            ),
        }))
        return

    # ── Workaround (error + research + fix) ──
    if "workaround" in patterns:
        p = patterns["workaround"]
        mark_prompted(session_key, dedup_key)
        print(json.dumps({
            "decision": "block",
            "reason": (
                f"You researched a problem ({p['research_queries']} "
                f"search(es)) and made changes to fix it. Workarounds "
                f"found through research are especially valuable. "
                f"Would you like to contribute to CommonTrace? Use "
                f"contribute_trace to submit, or say 'skip'."
            ),
        }))
        return

    # ── Config discovery ──
    if "config_discovery" in patterns:
        files = patterns["config_discovery"]["config_files"]
        mark_prompted(session_key, dedup_key)
        print(json.dumps({
            "decision": "block",
            "reason": (
                f"You modified configuration file(s) "
                f"({', '.join(Path(f).name for f in files[:3])}) "
                f"while debugging errors. Configuration discoveries "
                f"are hard-won knowledge. Would you like to contribute "
                f"to CommonTrace? Use contribute_trace, or say 'skip'."
            ),
        }))
        return

    # ── Iteration (same file edited repeatedly) ──
    if "iteration" in patterns:
        p = patterns["iteration"]
        mark_prompted(session_key, dedup_key)
        print(json.dumps({
            "decision": "block",
            "reason": (
                f"You iterated on "
                f"{', '.join(Path(f).name for f in p['iterated_files'][:3])} "
                f"(edited {p['max_edits']}+ times). If you solved "
                f"something through iteration, consider contributing "
                f"to CommonTrace. Use contribute_trace, or say 'skip'."
            ),
        }))
        return

    # ── Multi-turn catch-all ──
    if "multi_turn_work" in patterns:
        mark_prompted(session_key, dedup_key)
        print(json.dumps({
            "decision": "block",
            "reason": (
                "This has been a multi-turn session with code changes. "
                "If you solved a problem, found a workaround, or learned "
                "something useful, consider contributing to CommonTrace. "
                "Use contribute_trace to submit, or say 'skip'."
            ),
        }))
        return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
