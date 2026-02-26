#!/usr/bin/env python3
"""
CommonTrace Stop hook — Layer 2 knowledge scoring + contribution prompts.

Reads accumulated state from Layer 1 hooks (JSONL files) and knowledge
candidates detected in real-time by post_tool_use.py, then computes a
weighted importance score to decide whether to prompt for contribution.

Importance scoring (structural, no NLU):
  error_resolution:       3.0  — error→fix→verify cycle
  research_then_implement: 2.0  — searched then coded (no errors)
  approach_reversal:       2.5  — rewrote after iteration (paradigm shift)
  cross_file_breadth:      1.5  — changes spanning 3+ directories
  iteration_depth:         1.5  — same file edited many times
  temporal_investment:     1.0  — long session with sustained activity
  config_discovery:        2.0  — config changes that fixed errors
  novelty_encounter:       2.0  — new language/domain in project

Threshold >= 4.0 triggers contribution prompt.

Also handles: post-contribution refinement, session persistence,
anonymized trigger stats reporting.
"""

import hashlib
import json
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from session_state import get_state_dir, read_events, read_counter


RESOLUTION_DIR = Path("/tmp/commontrace-resolutions")
IMPORTANCE_THRESHOLD = 4.0
MIN_TURNS = 2


def get_session_key(data: dict) -> str:
    session_id = data.get("session_id")
    return str(session_id) if session_id else str(os.getppid())


def score_dedup_key(score: float, top_pattern: str, evidence: dict) -> str:
    """Stable hash based on score bucket + pattern + evidence."""
    bucket = int(score)  # Dedup by integer bucket
    key = f"{bucket}:{top_pattern}:" + json.dumps(
        evidence, sort_keys=True, default=str)
    return hashlib.md5(key.encode()).hexdigest()[:12]


def was_already_prompted(session_key: str, dedup_key: str) -> bool:
    return (RESOLUTION_DIR / f"dedup-{session_key}-{dedup_key}").exists()


def mark_prompted(session_key: str, dedup_key: str) -> None:
    RESOLUTION_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (RESOLUTION_DIR / f"dedup-{session_key}-{dedup_key}").write_text(
            "1", encoding="utf-8")
    except OSError:
        pass


def compute_importance(state_dir: Path) -> tuple[float, str, dict]:
    """Compute weighted importance score from all structural signals.

    Returns: (score, top_pattern_name, evidence_dict)
    """
    errors = read_events(state_dir, "errors.jsonl")
    resolutions = read_events(state_dir, "resolutions.jsonl")
    changes = read_events(state_dir, "changes.jsonl")
    research = read_events(state_dir, "research.jsonl")
    candidates = read_events(state_dir, "candidates.jsonl")
    user_turns = read_counter(state_dir, "user_turn_count")

    scores: dict[str, float] = {}
    evidence: dict[str, dict] = {}

    # ── Error Resolution (3.0) ──
    if errors and changes and resolutions:
        first_error_t = min(e.get("t", 0) for e in errors)
        last_change_t = max(c.get("t", 0) for c in changes)
        last_resolution_t = max(r.get("t", 0) for r in resolutions)
        if first_error_t < last_change_t <= last_resolution_t:
            scores["error_resolution"] = 3.0
            evidence["error_resolution"] = {
                "errors": len(errors),
                "changes": len(changes),
                "resolutions": len(resolutions),
            }

    # ── Research→Implement (2.0) — NEW: no errors required ──
    research_candidates = [
        c for c in candidates if c.get("pattern") == "research_then_implement"
    ]
    if research_candidates:
        scores["research_then_implement"] = 2.0
        rc = research_candidates[-1]
        evidence["research_then_implement"] = {
            "research_queries": rc.get("research_queries", []),
            "research_count": rc.get("research_count", 0),
            "file": rc.get("file", ""),
        }

    # ── Approach Reversal (2.5) — NEW: rewrite after iteration ──
    reversal_candidates = [
        c for c in candidates if c.get("pattern") == "approach_reversal"
    ]
    if reversal_candidates:
        scores["approach_reversal"] = 2.5
        rc = reversal_candidates[-1]
        evidence["approach_reversal"] = {
            "file": rc.get("file", ""),
            "previous_edits": rc.get("previous_edits", 0),
        }

    # ── Cross-file Breadth (1.5) — NEW: multi-directory work ──
    breadth_candidates = [
        c for c in candidates if c.get("pattern") == "cross_file_breadth"
    ]
    if breadth_candidates:
        bc = breadth_candidates[-1]
        scores["cross_file_breadth"] = 1.5
        evidence["cross_file_breadth"] = {
            "directories": bc.get("directories", [])[:5],
            "file_count": bc.get("file_count", 0),
        }

    # ── Config Discovery (2.0) ──
    config_changes = [c for c in changes if c.get("is_config")]
    if config_changes and errors:
        first_error_t = min(e.get("t", 0) for e in errors)
        config_after = [
            c for c in config_changes if c.get("t", 0) > first_error_t
        ]
        if config_after:
            scores["config_discovery"] = 2.0
            evidence["config_discovery"] = {
                "config_files": [c.get("file") for c in config_after[:3]],
            }

    # ── Iteration Depth (1.5) ──
    file_counts = Counter(c.get("file", "") for c in changes)
    iterated = {f: n for f, n in file_counts.items() if n >= 3}
    if iterated:
        max_edits = max(iterated.values())
        # Scale: 3 edits = 1.5, 6+ edits = 2.0
        scores["iteration_depth"] = min(1.5 * (max_edits / 3), 2.0)
        evidence["iteration_depth"] = {
            "files": list(iterated.keys())[:3],
            "max_edits": max_edits,
        }

    # ── Temporal Investment (1.0) ──
    all_events = errors + resolutions + changes + research
    if all_events and len(all_events) >= 5:
        timestamps = [e.get("t", 0) for e in all_events if e.get("t")]
        if timestamps:
            duration_min = (max(timestamps) - min(timestamps)) / 60
            if duration_min >= 5:
                # Scale: 5min = 0.5, 30min+ = 1.0
                scores["temporal_investment"] = min(
                    0.5 + 0.5 * math.log(duration_min / 5, 6), 1.0)
                evidence["temporal_investment"] = {
                    "duration_minutes": round(duration_min, 1),
                    "event_count": len(all_events),
                }

    # ── Novelty Encounter (2.0) ──
    # Detected via domain_entry trigger firing (bridge file)
    try:
        domain_entry_path = state_dir / "domain_entry_fired"
        if domain_entry_path.exists():
            scores["novelty_encounter"] = 2.0
            evidence["novelty_encounter"] = {
                "new_domain": domain_entry_path.read_text(
                    encoding="utf-8").strip()
            }
    except OSError:
        pass

    # ── Workaround: research + errors + changes (1.5) ──
    if research and errors and changes and "error_resolution" not in scores:
        scores["workaround"] = 1.5
        evidence["workaround"] = {
            "research_count": len(research),
            "error_count": len(errors),
        }

    # Total score
    total = sum(scores.values())

    # Find top contributing pattern
    top_pattern = max(scores, key=scores.get) if scores else "none"
    top_evidence = evidence.get(top_pattern, {})

    return total, top_pattern, top_evidence


def _build_prompt(score: float, top_pattern: str, evidence: dict,
                  state_dir: Path) -> str:
    """Build a context-rich contribution prompt based on detected knowledge."""
    candidates = read_events(state_dir, "candidates.jsonl")

    # Pattern-specific prompts
    prompts = {
        "error_resolution": (
            f"You resolved {evidence.get('errors', 0)} error(s) through "
            f"{evidence.get('changes', 0)} change(s) and verified the fix. "
            f"Error resolutions are high-value knowledge."
        ),
        "research_then_implement": (
            f"You researched "
            f"({', '.join(evidence.get('research_queries', [])[:2])}) "
            f"and then implemented a solution. Knowledge discovered "
            f"through research is especially valuable to other agents."
        ),
        "approach_reversal": (
            f"You rewrote {Path(evidence.get('file', '')).name} after "
            f"{evidence.get('previous_edits', 0)} previous edits — "
            f"a sign that the initial approach was wrong. "
            f"What you learned about WHY is valuable knowledge."
        ),
        "cross_file_breadth": (
            f"You made changes across {evidence.get('file_count', 0)} files "
            f"in {len(evidence.get('directories', []))} directories. "
            f"Integration knowledge (how systems connect) is consistently "
            f"the hardest to discover and most valuable to share."
        ),
        "config_discovery": (
            f"You modified configuration file(s) "
            f"({', '.join(Path(f).name for f in evidence.get('config_files', [])[:3])}) "
            f"to fix errors. Configuration discoveries are hard-won knowledge."
        ),
        "iteration_depth": (
            f"You iterated on "
            f"{', '.join(Path(f).name for f in evidence.get('files', [])[:3])} "
            f"({evidence.get('max_edits', 0)}+ edits). Solutions found "
            f"through iteration represent genuine effort."
        ),
        "workaround": (
            f"You researched a problem ({evidence.get('research_count', 0)} "
            f"searches) and worked around {evidence.get('error_count', 0)} "
            f"error(s). Workarounds are especially valuable."
        ),
    }

    base = prompts.get(top_pattern, (
        "This session involved substantial work that may contain "
        "knowledge worth sharing."
    ))

    # Add journey context from candidates if available
    journey = ""
    if candidates:
        patterns_found = list({c.get("pattern") for c in candidates})
        if len(patterns_found) > 1:
            journey = (
                f" (Session involved: "
                f"{', '.join(p.replace('_', ' ') for p in patterns_found)})"
            )

    return (
        f"{base}{journey} "
        f"Would you like to contribute to CommonTrace? "
        f"Use contribute_trace to submit, or say 'skip'."
    )


def _persist_session(data: dict, state_dir: Path) -> None:
    """Migrate session data to persistent SQLite store."""
    try:
        from local_store import (
            _get_conn, migrate_jsonl_events, end_session, record_entity,
        )
        conn = _get_conn()
        session_id = data.get("session_id") or str(os.getppid())

        migrate_jsonl_events(conn, session_id, state_dir)

        errors = read_events(state_dir, "errors.jsonl")
        resolutions = read_events(state_dir, "resolutions.jsonl")
        contributions = read_events(state_dir, "contributions.jsonl")
        end_session(conn, session_id, {
            "error_count": len(errors),
            "resolution_count": len(resolutions),
            "contribution_count": len(contributions),
        })

        project_id_path = state_dir / "project_id"
        if project_id_path.exists():
            project_id = int(
                project_id_path.read_text(encoding="utf-8").strip())
            changes = read_events(state_dir, "changes.jsonl")
            seen_langs: set[str] = set()
            lang_map = {
                ".py": "python", ".ts": "typescript", ".tsx": "typescript",
                ".jsx": "javascript", ".js": "javascript", ".go": "go",
                ".rs": "rust", ".java": "java", ".rb": "ruby",
            }
            for change in changes:
                ext = Path(change.get("file", "")).suffix.lower()
                lang = lang_map.get(ext)
                if lang and lang not in seen_langs:
                    record_entity(conn, project_id, "language", lang)
                    seen_langs.add(lang)

        conn.close()
    except Exception:
        pass


def _report_trigger_stats(data: dict, state_dir: Path) -> None:
    """Send anonymized trigger effectiveness stats to the API."""
    try:
        from local_store import _get_conn, get_trigger_effectiveness
        import urllib.request

        session_id = data.get("session_id") or str(os.getppid())
        project_id_path = state_dir / "project_id"
        project_id = None
        if project_id_path.exists():
            project_id = int(
                project_id_path.read_text(encoding="utf-8").strip())

        conn = _get_conn()
        stats = get_trigger_effectiveness(conn, project_id)
        conn.close()

        if not stats:
            return

        # Load API key
        config_file = Path.home() / ".commontrace" / "config.json"
        api_key = ""
        if config_file.exists():
            config = json.loads(config_file.read_text(encoding="utf-8"))
            api_key = config.get("api_key", "")
        if not api_key:
            api_key = os.environ.get("COMMONTRACE_API_KEY", "")
        if not api_key:
            return

        base_url = os.environ.get(
            "COMMONTRACE_API_BASE_URL",
            "https://api.commontrace.org").rstrip("/")

        payload = json.dumps({
            "trigger_stats": stats,
            "session_id": session_id,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{base_url}/api/v1/telemetry/triggers",
            data=payload, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
            },
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # Best-effort, never block


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

    if contributions and user_turns > turns_at_contribution:
        trace_id = contributions[-1].get("trace_id", "")
        ev = {"trace_id": trace_id, "turns_since": user_turns - turns_at_contribution}
        dedup_key = score_dedup_key(0, "post_contribution", ev)
        if not was_already_prompted(session_key, dedup_key):
            mark_prompted(session_key, dedup_key)
            print(json.dumps({
                "decision": "block",
                "reason": (
                    "You contributed a trace earlier and the conversation "
                    "continued. The trace may benefit from additional context. "
                    f"Use amend_trace to update it"
                    f"{f' (ID: {trace_id})' if trace_id else ''}, "
                    "or say 'skip'."
                ),
            }))
            return

    # Compute importance score
    score, top_pattern, top_evidence = compute_importance(state_dir)

    if score < IMPORTANCE_THRESHOLD:
        return

    dedup_key = score_dedup_key(score, top_pattern, top_evidence)
    if was_already_prompted(session_key, dedup_key):
        return

    mark_prompted(session_key, dedup_key)
    prompt = _build_prompt(score, top_pattern, top_evidence, state_dir)

    print(json.dumps({
        "decision": "block",
        "reason": prompt,
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
