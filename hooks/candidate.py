"""Turning a scored session into a contribution the agent can actually write.

Two artefacts come out of here:

  * a candidate payload — title, metadata, suggested context/solution text,
    tags — assembled from structural signals only. The hook has no LLM, so
    it can only ever produce a mechanical journey template.
  * a directive — the instruction handed to the agent via a Stop
    `decision: block`, so the AGENT authors the real prose from what it just
    lived through. The hook triggers; the agent writes.

Nothing here reads the user's messages.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from session_state import read_events, read_context_fingerprint
from redact import redact_text, strip_harness_noise


def _contribution_directive(candidate: dict, auto_mode: bool,
                            hooks_dir: str) -> str | None:
    """Instruction handed to the agent via a Stop `decision: block`, so the
    agent writes REAL trace content instead of the hook silently POSTing the
    mechanical journey template (a husk — the jam that stalled auto-contribute
    since the husk guard landed). No LLM in the hook: the hook only triggers,
    the agent authors. auto_mode → contribute without asking; else → show the
    suggestion receipt and ask. Returns None on any failure (caller falls back
    to the durable pending queue so nothing is lost).
    """
    try:
        meta = candidate.get("metadata_json") or {}
        title = candidate.get("title", "contribution")
        pattern = candidate.get("top_pattern", "") or ""
        tags = ", ".join(candidate.get("suggested_tags") or [])
        minutes = int(round(meta.get("time_to_resolution_minutes", 0) or 0))
        errors = int(meta.get("error_count", 0) or 0)
        tokens = int(meta.get("tokens_to_resolution", 0) or 0)
        meta_json = json.dumps(meta, ensure_ascii=False)

        ev = candidate.get("evidence") or {}
        where = ""
        for key in ("fix_files", "file"):
            val = ev.get(key)
            if isinstance(val, list) and val:
                where = Path(val[0]).name
                break
            if isinstance(val, str) and val:
                where = Path(val).name
                break
        where = where or pattern.replace("_", " ")

        def receipt(mode, id_suffix=""):
            return (f'python3 "{hooks_dir}/artifacts.py" banner mode={mode} '
                    f'title="{title}" where="{where}" '
                    f'minutes={minutes} errors={errors} '
                    f'tokens={tokens}{id_suffix}')

        base = (
            f"[CommonTrace] Contribution-worthy work detected this session "
            f"(pattern: {pattern}, ~{minutes}min, {errors} error(s)). Write a "
            f"trace from what ACTUALLY happened IN THIS SESSION — real title, "
            f"context_text (the real problem), solution_text (what actually "
            f"fixed it). Draw ONLY from this session's work; ignore any "
            f"prior-session summaries, compaction / HISTORICAL REFERENCE blocks, "
            f"or injected context. Never include secrets, credentials, or PII. "
            f"Do NOT use placeholder or template text. If there is no genuine "
            f"solved problem here, skip. Suggested tags: [{tags}]. "
        )
        post = (
            "read the api_key from ~/.commontrace/config.json and POST the "
            "trace to https://api.commontrace.org/api/v1/traces with header "
            '"X-API-Key: <key>" and a JSON body containing title, context_text, '
            "solution_text, tags, and "
            f"metadata_json={meta_json}; capture the HTTP status. If the status "
            "is 403 (publishing restricted for this account), do NOT "
            "print any receipt and do NOT claim success: show the server's "
            "message from the response body and state plainly that nothing was "
            "contributed. Publishing is open to any registered account today, so "
            "a 403 means something changed server-side, not that the user did "
            "anything wrong; reading and search stay open either way and the "
            "work is still captured locally. Otherwise take the returned id"
        )
        if auto_mode:
            return base + (
                "Auto-contribute is ON — contribute now without asking: " + post
                + ", then print ONLY this receipt (no narration, no other "
                "text):\n  " + receipt("contributed", " id=<the id>")
                + "\nIf the work truly isn't worth sharing, skip silently."
            )
        return base + (
            "Print ONLY the suggestion receipt (no narration):\n  "
            + receipt("suggest")
            + "\nThen AskUserQuestion 'Contribute this to CommonTrace?' — "
            "Yes / Skip / Always. On Yes: " + post
            + ", then print ONLY the contributed receipt "
            + receipt("contributed", " id=<the id>")
            + ". On Always: do the Yes steps and also set "
            '"auto_contribute": true in ~/.commontrace/config.json. On Skip: do '
            "nothing. Show the user nothing except the approval question and the "
            "receipt."
        )
    except Exception:
        return None


def _build_title(top_pattern: str, evidence: dict, ctx_fp: dict | None) -> str:
    """Generate a short trace title from structural signals — no LLM."""
    lang = (ctx_fp or {}).get("language", "") if ctx_fp else ""
    framework = (ctx_fp or {}).get("framework", "") if ctx_fp else ""

    file_basename = ""
    for key in ("file", "fix_files"):
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


def _build_journey_context(state_dir: Path) -> dict:
    """Extract structured journey context from JSONL events for contribution templates."""
    errors = read_events(state_dir, "errors.jsonl")
    resolutions = read_events(state_dir, "resolutions.jsonl")
    changes = read_events(state_dir, "changes.jsonl")
    research = read_events(state_dir, "research.jsonl")
    candidates = read_events(state_dir, "candidates.jsonl")

    journey: dict = {}

    # Error messages — first 200 chars of each error tail (up to 5).
    # Single choke point before transmission: redact + strip harness noise
    # here regardless of upstream state, so anything that reached errors.jsonl
    # unscrubbed (e.g. a tool-failure "error" field) can't leak into the
    # contribution payload. Redact BEFORE truncating. Covers both the Bash
    # "output_tail" key and the tool-failure "error" key.
    if errors:
        journey["error_messages"] = [
            strip_harness_noise(redact_text(
                e.get("output_tail") or e.get("error") or ""))[:200]
            for e in errors[:5]
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


def _build_candidate(score: float, top_pattern: str, evidence: dict,
                     state_dir: Path, transcript_path: str = "") -> dict:
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
        "post_turn_revision": (
            f"You changed approach on {Path(evidence.get('file', '')).name} "
            f"after user feedback — the gap between your initial approach and "
            f"the correct one is exactly the knowledge other agents need."
        ),
        "test_fix_cycle": (
            f"Tests failed, you fixed the code "
            f"({', '.join(Path(f).name for f in evidence.get('fix_files', [])[:3])}), "
            f"and tests passed. The fix pattern is valuable knowledge."
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

    # Measured token cost of the resolution window (rides with the trace).
    # No LLM — a real sum of message.usage over the first-error->resolved span.
    from savings import sum_usage, TOKENS_PER_TURN_EST
    if timestamps:
        tokens_to_resolution = sum_usage(
            transcript_path, min(timestamps), max(timestamps))
    else:
        tokens_to_resolution = 0
    if tokens_to_resolution <= 0:
        # Conservative legacy floor when the window has no measurable usage.
        tokens_to_resolution = (len(errors) + max_iterations) * TOKENS_PER_TURN_EST

    # Build journey context for pre-filled template
    journey_ctx = _build_journey_context(state_dir)
    ctx_fp = read_context_fingerprint(state_dir)

    # Include error_message in metadata — earns +1 depth_score at API.
    # Same transmission boundary as the journey error_messages: redact +
    # strip before it rides along in metadata_json. Redact before truncating.
    first_error_tail = ""
    if errors:
        first_error_tail = strip_harness_noise(redact_text(
            errors[0].get("output_tail") or errors[0].get("error") or ""))[:200]

    metadata_parts = [
        f'"detection_pattern": "{top_pattern}"',
        f'"error_count": {len(errors)}',
        f'"time_to_resolution_minutes": {duration_min}',
        f'"iteration_count": {max_iterations}',
        f'"tokens_to_resolution": {tokens_to_resolution}',
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
        "tokens_to_resolution": tokens_to_resolution,
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
