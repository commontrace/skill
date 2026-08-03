"""How much was this session worth? One weighted total, five patterns.

Structural scoring only — no LLM, no NLU. Four of the five patterns arrive
as candidates written by detection.py; error_resolution is derived here
from the raw error / change / resolution streams.

  error_resolution:        3.0  — error → fix → verified
  post_turn_revision:         2.5  — the user redirected the approach
  approach_reversal:       2.5  — rewrote after iterating (paradigm shift)
  test_fix_cycle:          2.0  — test fails → fix code → test passes
  research_then_implement: 2.0  — searched, then coded, no errors

On top of the base weights:

  * temporal proximity compounding — a pattern that fires within 5 minutes
    of a high-signal one gets up to +30% (synaptic tagging)
  * reinforcement — patterns whose retrieval triggers actually get consumed
    are boosted by their own track record

Total >= IMPORTANCE_THRESHOLD prompts for a contribution.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from session_state import read_events

IMPORTANCE_THRESHOLD = 4.0

# Base weight per pattern. Every entry here is produced by detection.py or
# derived below — nothing is scored that isn't detected, and nothing is
# detected that isn't scored.
PATTERN_WEIGHTS = {
    "error_resolution": 3.0,
    "post_turn_revision": 2.5,
    "approach_reversal": 2.5,
    "test_fix_cycle": 2.0,
    "research_then_implement": 2.0,
}

# Patterns whose presence tags nearby work as valuable (synaptic tagging).
HIGH_SIGNAL = {"error_resolution", "approach_reversal"}
PROXIMITY_WINDOW_SECONDS = 300
PROXIMITY_MAX_BOOST = 0.3

# Which candidate fields become the evidence dict handed to the contribution
# draft, with the default that stands in when a candidate lacks the field.
# The defaults are type-correct on purpose: downstream builds file names with
# Path(evidence["file"]), so a None here would crash the draft.
_EVIDENCE_FIELDS = {
    "research_then_implement": {
        "research_queries": [], "research_count": 0, "file": ""},
    "approach_reversal": {"file": "", "previous_edits": 0},
    "post_turn_revision": {"file": "", "pre_turn_edits": 0},
    "test_fix_cycle": {"test_failures": 0, "fix_files": []},
}
# Evidence lists are only ever read for display; keep them short.
_EVIDENCE_LIST_CAP = 3


# --- Reinforcement loop ----------------------------------------------------
# Nudge pattern weights by each trigger's real consumption track record.
# Pure-structural: reads SQLite trigger counters, no LLM. A pattern is only
# adjusted once its mapped triggers have fired at least MIN_FIRED times, so
# cold-start sessions leave scoring untouched.
MIN_FIRED = 3

# Map a scored detection pattern to the trigger name(s) whose track record
# should reinforce it. Patterns absent from this map are never adjusted.
PATTERN_TO_TRIGGERS = {
    "error_resolution": ("error_recurrence", "bash_error"),
}


def _clamp(lo, hi, val):
    return max(lo, min(hi, val))


def _pattern_effectiveness(pattern, effectiveness):
    """Aggregate fired/consumed across all triggers mapped to `pattern`.

    Returns {"fired", "consumed", "rate"} or None when the pattern is
    unmapped or its triggers never fired.
    """
    triggers = PATTERN_TO_TRIGGERS.get(pattern)
    if not triggers:
        return None
    fired = consumed = 0
    for t in triggers:
        e = effectiveness.get(t)
        if e:
            fired += e["fired"]
            consumed += e["consumed"]
    if fired <= 0:
        return None
    return {"fired": fired, "consumed": consumed, "rate": round(consumed / fired, 2)}


def _apply_reinforcement(scores, effectiveness):
    """Scale mapped pattern scores in place by their trigger consumption rate.

    multiplier = clamp(1.0, 1.3, 0.85 + 0.5 * rate) — boost-only. Every
    pattern in PATTERN_TO_TRIGGERS is a high-signal one, so a cold or unlucky
    trigger record may lift a score but must never push it below its base
    weight. No-op on empty effectiveness or when a pattern's triggers fired
    fewer than MIN_FIRED times.
    """
    if not effectiveness:
        return
    for pattern in list(scores):
        if scores[pattern] <= 0:
            continue
        e = _pattern_effectiveness(pattern, effectiveness)
        if not e or e["fired"] < MIN_FIRED:
            continue
        scores[pattern] *= _clamp(1.0, 1.3, 0.85 + 0.5 * e["rate"])


def _score_error_resolution(state_dir, scores, evidence):
    """error → change → verified success, read straight off the event streams.

    The only pattern with no candidate row: it is a property of the whole
    session's ordering, not of any single tool call.
    """
    errors = read_events(state_dir, "errors.jsonl")
    changes = read_events(state_dir, "changes.jsonl")
    resolutions = read_events(state_dir, "resolutions.jsonl")
    if not (errors and changes and resolutions):
        return
    first_error_t = min(e.get("t", 0) for e in errors)
    last_change_t = max(c.get("t", 0) for c in changes)
    last_resolution_t = max(r.get("t", 0) for r in resolutions)
    if not (first_error_t < last_change_t <= last_resolution_t):
        return
    scores["error_resolution"] = PATTERN_WEIGHTS["error_resolution"]
    evidence["error_resolution"] = {
        "errors": len(errors),
        "changes": len(changes),
        "resolutions": len(resolutions),
    }


def _score_candidates(candidates, scores, evidence):
    """Score the candidate-backed patterns. Last candidate of a kind wins."""
    for pattern, fields in _EVIDENCE_FIELDS.items():
        matches = [c for c in candidates if c.get("pattern") == pattern]
        if not matches:
            continue
        scores[pattern] = PATTERN_WEIGHTS[pattern]
        last = matches[-1]
        found = {}
        for field, default in fields.items():
            value = last.get(field, default)
            if isinstance(default, list):
                value = (list(value)[:_EVIDENCE_LIST_CAP]
                         if isinstance(value, list) else list(default))
            found[field] = value
        evidence[pattern] = found


def _apply_temporal_proximity(candidates, scores):
    """Boost patterns that fired close in time to a high-signal one.

    Synaptic tagging: work done in the shadow of a hard-won fix is more
    likely to be part of the same lesson.
    """
    if len(candidates) < 2:
        return
    high_events = [c for c in candidates if c.get("pattern") in HIGH_SIGNAL]
    if not high_events:
        return
    for candidate in candidates:
        pattern = candidate.get("pattern", "")
        if pattern in HIGH_SIGNAL or pattern not in scores:
            continue
        for he in high_events:
            dt = abs(candidate.get("t", 0) - he.get("t", 0))
            if dt < PROXIMITY_WINDOW_SECONDS:
                proximity = 1.0 - (dt / PROXIMITY_WINDOW_SECONDS)
                scores[pattern] *= (1.0 + PROXIMITY_MAX_BOOST * proximity)
                break


def compute_importance(state_dir: Path,
                       effectiveness: dict | None = None
                       ) -> tuple[float, str, dict]:
    """Compute the weighted importance score from all structural signals.

    Returns: (total_score, top_pattern_name, evidence_for_top_pattern)
    """
    candidates = read_events(state_dir, "candidates.jsonl")

    scores: dict[str, float] = {}
    evidence: dict[str, dict] = {}

    _score_error_resolution(state_dir, scores, evidence)
    _score_candidates(candidates, scores, evidence)
    _apply_temporal_proximity(candidates, scores)
    _apply_reinforcement(scores, effectiveness)

    total = sum(scores.values())
    top_pattern = max(scores, key=scores.get) if scores else "none"
    return total, top_pattern, evidence.get(top_pattern, {})
