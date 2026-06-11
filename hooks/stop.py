#!/usr/bin/env python3
"""
CommonTrace Stop hook — Layer 2 knowledge scoring + contribution prompts.

Reads accumulated state from Layer 1 hooks (JSONL files) and knowledge
candidates detected in real-time by post_tool_use.py, then computes a
weighted importance score to decide whether to prompt for contribution.

Importance scoring (structural, no NLU):
  error_resolution:       3.0  — error→fix→verify cycle
  security_hardening:     2.5  — security file changes after errors
  user_correction:        2.5  — user redirected approach (file changed before/after turn)
  approach_reversal:      2.5  — rewrote after iteration (paradigm shift)
  research_then_implement: 2.0  — searched then coded (no errors)
  test_fix_cycle:         2.0  — test fails → fix code → test passes
  dependency_resolution:  2.0  — package manager errors → config fix → success
  config_discovery:       2.0  — config changes that fixed errors
  novelty_encounter:      2.0  — new language/domain in project
  infra_discovery:        2.0  — infrastructure file changes after errors
  migration_pattern:      2.0  — 5+ files across dirs + config changes
  iteration_depth:        1.5  — same file edited many times (scales to 2.0)
  cross_file_breadth:     1.5  — changes spanning 3+ directories
  generation_effect:      1.5  — solved without external knowledge
  workaround:             1.5  — research + errors + changes
  temporal_investment:    1.0  — long session with sustained activity

Temporal proximity compounding: patterns near high-signal events get a
0-30% boost (synaptic tagging). Threshold >= 4.0 triggers contribution.

Also handles: post-contribution refinement, session persistence,
anonymized trigger stats reporting.
"""

import json
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from session_state import get_state_dir, read_events, read_counter


RESOLUTION_DIR = Path.home() / ".commontrace" / "resolutions"
PENDING_DIR = Path.home() / ".commontrace" / "pending"
AUTO_LOG = Path.home() / ".commontrace" / "auto-log.jsonl"
CONFIG_FILE = Path.home() / ".commontrace" / "config.json"
IMPORTANCE_THRESHOLD = 4.0
MIN_TURNS = 2


def _read_config() -> dict:
    """Read ~/.commontrace/config.json. Returns {} on any failure.

    Recognized fields:
      auto_contribute (bool, default True) — submit silently to API when true,
                                              write pending file when false
    """
    try:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return {}


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
    except OSError:
        pass


def _auto_submit(payload: dict) -> str | None:
    """Submit candidate directly to API. Returns trace_id on success, else None.

    Used in auto mode (auto_contribute=true). Best-effort — failures fall back
    to writing a pending file so nothing is lost.
    """
    import urllib.request
    import urllib.error
    config = _read_config()
    api_key = config.get("api_key") or os.environ.get("COMMONTRACE_API_KEY", "")
    if not api_key:
        return None
    base_url = os.environ.get(
        "COMMONTRACE_API_BASE_URL",
        "https://api.commontrace.org").rstrip("/")

    metadata = dict(payload.get("metadata_json") or {})
    metadata["auto_contributed"] = True

    body = {
        "title": payload.get("title") or "auto-contributed trace",
        "context_text": payload.get("suggested_context_text") or "(no context captured)",
        "solution_text": payload.get("suggested_solution_text") or "(no solution captured)",
        "tags": payload.get("suggested_tags") or [],
        "metadata_json": metadata,
    }

    try:
        data_bytes = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/api/v1/traces",
            data=data_bytes, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("id")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
        return None


def _append_auto_log(entry: dict) -> None:
    """Append a record of an auto-contributed trace for user audit."""
    try:
        AUTO_LOG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        entry.setdefault("t", time.time())
        with open(AUTO_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        try:
            os.chmod(AUTO_LOG, 0o600)
        except OSError:
            pass
    except OSError:
        pass


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
        return None


def _build_title(top_pattern: str, evidence: dict, ctx_fp: dict | None) -> str:
    """Generate a short trace title from structural signals — no LLM."""
    lang = (ctx_fp or {}).get("language", "") if ctx_fp else ""
    framework = (ctx_fp or {}).get("framework", "") if ctx_fp else ""

    file_basename = ""
    for key in ("file", "files", "fix_files", "config_files",
                "security_files", "infra_files"):
        val = evidence.get(key)
        if isinstance(val, str) and val:
            file_basename = Path(val).name
            break
        if isinstance(val, list) and val:
            file_basename = Path(val[0]).name
            break

    pattern_label = top_pattern.replace("_", " ")
    parts = [pattern_label]
    if file_basename:
        parts.append(f"in {file_basename}")
    stack = "/".join(p for p in (lang, framework) if p)
    if stack:
        parts.append(f"({stack})")
    title = " ".join(parts)[:200]
    return title or "auto-contributed trace"


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

    # ── User Correction (2.5) — user redirected the approach ──
    correction_candidates = [
        c for c in candidates if c.get("pattern") == "user_correction"
    ]
    if correction_candidates:
        scores["user_correction"] = 2.5
        cc = correction_candidates[-1]
        evidence["user_correction"] = {
            "file": cc.get("file", ""),
            "pre_turn_edits": cc.get("pre_turn_edits", 0),
        }

    # ── Test Fix Cycle (2.0) — tests fail → fix code → tests pass ──
    test_candidates = [
        c for c in candidates if c.get("pattern") == "test_fix_cycle"
    ]
    if test_candidates:
        scores["test_fix_cycle"] = 2.0
        tc = test_candidates[-1]
        evidence["test_fix_cycle"] = {
            "test_failures": tc.get("test_failures", 0),
            "fix_files": tc.get("fix_files", [])[:3],
        }

    # ── Dependency Resolution (2.0) — version/package conflicts resolved ──
    dep_candidates = [
        c for c in candidates if c.get("pattern") == "dependency_resolution"
    ]
    if dep_candidates:
        scores["dependency_resolution"] = 2.0
        dc = dep_candidates[-1]
        evidence["dependency_resolution"] = {
            "error_count": dc.get("error_count", 0),
            "config_files": dc.get("config_files", [])[:3],
        }

    # ── Security Hardening (2.5) — security fix after errors ──
    sec_candidates = [
        c for c in candidates if c.get("pattern") == "security_hardening"
    ]
    if sec_candidates:
        scores["security_hardening"] = 2.5
        sc = sec_candidates[-1]
        evidence["security_hardening"] = {
            "security_files": sc.get("security_files", [])[:3],
            "error_count": sc.get("error_count", 0),
        }

    # ── Infra Discovery (2.0) — deployment/infrastructure fixes ──
    infra_candidates = [
        c for c in candidates if c.get("pattern") == "infra_discovery"
    ]
    if infra_candidates:
        scores["infra_discovery"] = 2.0
        ic = infra_candidates[-1]
        evidence["infra_discovery"] = {
            "infra_files": ic.get("infra_files", [])[:3],
            "error_count": ic.get("error_count", 0),
        }

    # ── Migration Pattern (2.0) — library/version migration ──
    mig_candidates = [
        c for c in candidates if c.get("pattern") == "migration_pattern"
    ]
    if mig_candidates:
        scores["migration_pattern"] = 2.0
        mc = mig_candidates[-1]
        evidence["migration_pattern"] = {
            "total_files": mc.get("total_files", 0),
            "config_files": mc.get("config_files", [])[:3],
        }

    # ── Generation Effect (1.5) — solved without external help ──
    consumed_traces = 0
    try:
        from local_store import _get_conn
        conn = _get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM trigger_feedback "
            "WHERE session_id = ? AND trace_consumed_id IS NOT NULL",
            (state_dir.name,),
        ).fetchone()
        consumed_traces = row[0] if row else 0
        conn.close()
    except Exception:
        pass

    if errors and resolutions and not research and consumed_traces == 0:
        scores["generation_effect"] = 1.5
        evidence["generation_effect"] = {
            "errors": len(errors), "external_help": False,
        }
    elif errors and resolutions and research and consumed_traces == 0:
        scores["generation_effect"] = 1.0
        evidence["generation_effect"] = {
            "errors": len(errors), "researched_but_no_trace": True,
        }

    # ── User Emphasis (1.5) — user structurally stressed importance ──
    emphasis_events = read_events(state_dir, "emphasis.jsonl")
    if emphasis_events:
        # Aggregate: take the peak emphasis score across all turns
        peak_emphasis = max(e.get("emphasis_score", 0) for e in emphasis_events)
        all_keywords = []
        for e in emphasis_events:
            all_keywords.extend(e.get("keywords", []))
        unique_keywords = list(dict.fromkeys(all_keywords))  # dedupe, preserve order

        if peak_emphasis >= 0.2:
            # Scale: 0.2 emphasis = 1.0 weight, 1.0 emphasis = 1.5 weight
            scores["user_emphasis"] = 1.0 + 0.5 * min(1.0, peak_emphasis)
            evidence["user_emphasis"] = {
                "peak_emphasis": peak_emphasis,
                "emphasis_turns": len(emphasis_events),
                "keywords": unique_keywords[:5],
            }

    # ── Temporal Proximity Compounding ──
    # Patterns near high-signal events get a boost (synaptic tagging)
    HIGH_SIGNAL = {
        "error_resolution", "approach_reversal", "security_hardening",
    }
    if candidates and len(candidates) >= 2:
        high_events = [
            c for c in candidates if c.get("pattern") in HIGH_SIGNAL
        ]
        if high_events:
            WINDOW_SECONDS = 300
            for candidate in candidates:
                pattern = candidate.get("pattern", "")
                if pattern in HIGH_SIGNAL or pattern not in scores:
                    continue
                for he in high_events:
                    dt = abs(candidate.get("t", 0) - he.get("t", 0))
                    if dt < WINDOW_SECONDS:
                        proximity = 1.0 - (dt / WINDOW_SECONDS)
                        scores[pattern] *= (1.0 + 0.3 * proximity)
                        break

    # Total score
    total = sum(scores.values())

    # Find top contributing pattern
    top_pattern = max(scores, key=scores.get) if scores else "none"
    top_evidence = evidence.get(top_pattern, {})

    return total, top_pattern, top_evidence


def _build_journey_context(state_dir: Path) -> dict:
    """Extract structured journey context from JSONL events for contribution templates."""
    errors = read_events(state_dir, "errors.jsonl")
    resolutions = read_events(state_dir, "resolutions.jsonl")
    changes = read_events(state_dir, "changes.jsonl")
    research = read_events(state_dir, "research.jsonl")
    candidates = read_events(state_dir, "candidates.jsonl")

    journey: dict = {}

    # Error messages — first 200 chars of each error tail (up to 5)
    if errors:
        journey["error_messages"] = [
            e.get("output_tail", "")[:200] for e in errors[:5]
        ]

    # Successful commands (up to 5)
    if resolutions:
        journey["resolution_commands"] = [
            r.get("command", "")[:200] for r in resolutions[:5]
        ]

    # Research queries (up to 5)
    if research:
        journey["research_queries"] = [
            r.get("query", "")[:200] for r in research[:5]
        ]

    # Unique file paths changed (up to 10)
    if changes:
        files = list(dict.fromkeys(c.get("file", "") for c in changes if c.get("file")))
        journey["files_changed"] = files[:10]

        # Config files changed (up to 5)
        config_files = [c.get("file", "") for c in changes if c.get("is_config")]
        if config_files:
            journey["config_files"] = list(dict.fromkeys(config_files))[:5]

    # Approaches tried — if reversal detected, capture original + final
    reversal_candidates = [c for c in candidates if c.get("pattern") == "approach_reversal"]
    if reversal_candidates:
        rc = reversal_candidates[-1]
        journey["approaches_tried"] = {
            "file": rc.get("file", ""),
            "previous_edits": rc.get("previous_edits", 0),
            "reversed": True,
        }

    return journey


def _read_context_fingerprint(state_dir: Path) -> dict | None:
    """Read context fingerprint bridge file written by session_start."""
    try:
        return json.loads(
            (state_dir / "context_fingerprint.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return None


def _build_candidate(score: float, top_pattern: str, evidence: dict,
                     state_dir: Path) -> dict:
    """Build a structured candidate payload + human prompt from detection state.

    Returns dict with: score, top_pattern, evidence, metadata_json,
    suggested_context_text, suggested_solution_text, suggested_tags,
    title, human_prompt.
    """
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
        "user_correction": (
            f"You changed approach on {Path(evidence.get('file', '')).name} "
            f"after user feedback — the gap between your initial approach and "
            f"the correct one is exactly the knowledge other agents need."
        ),
        "test_fix_cycle": (
            f"Tests failed, you fixed the code "
            f"({', '.join(Path(f).name for f in evidence.get('fix_files', [])[:3])}), "
            f"and tests passed. The fix pattern is valuable knowledge."
        ),
        "dependency_resolution": (
            f"You resolved dependency/version conflicts involving "
            f"{', '.join(Path(f).name for f in evidence.get('config_files', [])[:3])}. "
            f"Package compatibility knowledge is extremely reusable."
        ),
        "security_hardening": (
            f"You fixed a security-related issue in "
            f"{', '.join(Path(f).name for f in evidence.get('security_files', [])[:3])}. "
            f"Security knowledge is critical to share — dangerous to miss."
        ),
        "infra_discovery": (
            f"You figured out infrastructure configuration "
            f"({', '.join(Path(f).name for f in evidence.get('infra_files', [])[:3])}). "
            f"Deployment knowledge is notoriously underdocumented."
        ),
        "migration_pattern": (
            f"You modified {evidence.get('total_files', 0)} files across "
            f"multiple directories — this looks like a migration. "
            f"Migration paths are poorly documented and highly reusable."
        ),
        "user_emphasis": (
            f"The user emphasized this work "
            f"({', '.join(evidence.get('keywords', [])[:3]) or 'strongly'}). "
            f"When users stress importance, the knowledge matters more — "
            f"like emotional memory in humans."
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

    # Build detection metadata for somatic intensity computation at API
    errors = read_events(state_dir, "errors.jsonl")
    changes = read_events(state_dir, "changes.jsonl")
    all_events = errors + changes + read_events(state_dir, "research.jsonl")
    timestamps = [e.get("t", 0) for e in all_events if e.get("t")]
    duration_min = round(
        (max(timestamps) - min(timestamps)) / 60, 1) if timestamps else 0

    file_counts = {}
    for c in changes:
        f = c.get("file", "")
        file_counts[f] = file_counts.get(f, 0) + 1
    max_iterations = max(file_counts.values()) if file_counts else 0

    # Include user emphasis score if detected
    emphasis_events = read_events(state_dir, "emphasis.jsonl")
    peak_emphasis = 0.0
    if emphasis_events:
        peak_emphasis = max(e.get("emphasis_score", 0) for e in emphasis_events)

    # Build journey context for pre-filled template
    journey_ctx = _build_journey_context(state_dir)
    ctx_fp = _read_context_fingerprint(state_dir)

    # Include error_message in metadata — earns +1 depth_score at API
    first_error_tail = ""
    if errors:
        first_error_tail = errors[0].get("output_tail", "")[:200]

    metadata_parts = [
        f'"detection_pattern": "{top_pattern}"',
        f'"error_count": {len(errors)}',
        f'"time_to_resolution_minutes": {duration_min}',
        f'"iteration_count": {max_iterations}',
        f'"user_emphasis": {peak_emphasis}',
    ]
    if first_error_tail:
        escaped_error = first_error_tail.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
        metadata_parts.append(f'"error_message": "{escaped_error}"')

    metadata_hint = (
        f'Include this in metadata_json: '
        f'{{{", ".join(metadata_parts)}}}'
    )

    # Build pre-filled contribution suggestions from journey context
    template_parts = []
    lang = ctx_fp.get("language", "") if ctx_fp else ""
    framework = ctx_fp.get("framework", "") if ctx_fp else ""

    suggested_context_text = ""
    suggested_solution_text = ""

    if journey_ctx.get("error_messages"):
        first_err = journey_ctx["error_messages"][0][:100]
        ctx_text = f"When working with {lang}"
        if framework:
            ctx_text += f" {framework}"
        ctx_text += f", encountered: {first_err}..."
        suggested_context_text = ctx_text
        template_parts.append(f"Suggested context_text: \"{ctx_text}\"")

    if journey_ctx.get("files_changed"):
        files_str = ", ".join(
            Path(f).name for f in journey_ctx["files_changed"][:3])
        sol_text = f"Resolution involved changing {files_str}."
        if journey_ctx.get("resolution_commands"):
            cmd = journey_ctx["resolution_commands"][0][:100]
            sol_text += f" Key command: {cmd}"
        suggested_solution_text = sol_text
        template_parts.append(f"Suggested solution_text: \"{sol_text}\"")

    tag_suggestions = []
    if lang:
        tag_suggestions.append(lang)
    if framework:
        tag_suggestions.append(framework)
    tag_suggestions.append(top_pattern.replace("_", "-"))
    template_parts.append(f"Suggested tags: [{', '.join(tag_suggestions)}]")

    template_hint = "\n".join(template_parts) if template_parts else ""

    human_prompt = (
        f"{base}{journey} "
        f"Would you like to contribute to CommonTrace? "
        f"Use contribute_trace to submit, or say 'skip'. "
        f"{metadata_hint}"
        f"{chr(10) + template_hint if template_hint else ''}"
    )

    metadata_json: dict = {
        "detection_pattern": top_pattern,
        "error_count": len(errors),
        "time_to_resolution_minutes": duration_min,
        "iteration_count": max_iterations,
        "user_emphasis": peak_emphasis,
    }
    if first_error_tail:
        metadata_json["error_message"] = first_error_tail

    return {
        "score": score,
        "top_pattern": top_pattern,
        "evidence": evidence,
        "metadata_json": metadata_json,
        "suggested_context_text": suggested_context_text,
        "suggested_solution_text": suggested_solution_text,
        "suggested_tags": tag_suggestions,
        "title": _build_title(top_pattern, evidence, ctx_fp),
        "human_prompt": human_prompt,
    }


def _persist_session(data: dict, state_dir: Path) -> None:
    """Persist session stats to SQLite working memory store."""
    try:
        from local_store import (
            _get_conn, end_session, prune_stale_cache,
        )
        conn = _get_conn()
        session_id = data.get("session_id") or str(os.getppid())

        errors = read_events(state_dir, "errors.jsonl")
        resolutions = read_events(state_dir, "resolutions.jsonl")
        contributions = read_events(state_dir, "contributions.jsonl")

        # Compute importance for session metadata
        score, top_pattern, _ = compute_importance(state_dir)

        end_session(conn, session_id, {
            "error_count": len(errors),
            "resolution_count": len(resolutions),
            "contribution_count": len(contributions),
        }, top_pattern=top_pattern, importance_score=score)

        # Prune stale cache entries
        prune_stale_cache(conn)

        conn.close()
    except Exception:
        pass


def _report_trigger_stats(data: dict, state_dir: Path) -> None:
    """Send anonymized trigger effectiveness stats to the API.

    M22: Only sends if user has opted in via telemetry=true in config.
    """
    try:
        # M22: Check telemetry consent before sending
        config_file = Path.home() / ".commontrace" / "config.json"
        if config_file.exists():
            config = json.loads(config_file.read_text(encoding="utf-8"))
            if not config.get("telemetry", False):
                return
        else:
            return  # No config = no consent
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

    config = _read_config()
    auto_mode = config.get("auto_contribute", True)

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
    score, top_pattern, top_evidence = compute_importance(state_dir)

    if score < IMPORTANCE_THRESHOLD:
        return

    if already_prompted(session_key, "score"):
        return

    mark_prompted(session_key, "score")
    candidate = _build_candidate(score, top_pattern, top_evidence, state_dir)

    if auto_mode:
        trace_id = _auto_submit(candidate)
        if trace_id:
            _append_auto_log({
                "trace_id": trace_id,
                "session_id": data.get("session_id", ""),
                "cwd": data.get("cwd", ""),
                "title": candidate.get("title", ""),
                "score": candidate.get("score", 0),
                "top_pattern": candidate.get("top_pattern", ""),
                "tags": candidate.get("suggested_tags", []),
            })
            line = _struggle_artifact(candidate, state_dir, trace_id)
            if line:
                print(json.dumps({"systemMessage": (
                    "CommonTrace captured this fight:\n" + line +
                    "\n(saved to ~/.commontrace/artifacts/"
                    "last-struggle.txt — paste it anywhere)")}))
            return
        # API failure: fall through to pending so nothing is lost

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
