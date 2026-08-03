"""Who fixed it? Pairing a passing command with the error it resolves.

Retrieval and payoff are separated in time: a trace is surfaced at the
failure, and the command only starts passing much later. This module holds
the ledger that connects the two — which traces were put in front of the
agent for which error signature, and, when that signature finally resolves,
which one gets the credit.

The evidence standard is deliberately narrow: same session, same error
signature, same command head, correct ordering. Nothing here reads the
agent's reasoning or the user's messages.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ct_config
from session_state import (
    append_event, read_events, error_hash, log_hook_error, read_project_id,
)
from redact import redact_command

# Commons traces the hook itself put in front of the agent, keyed by the
# error signature they were retrieved for. Read back by pair_resolution.
SURFACED_FILE = "surfaced.jsonl"


def _command_head(command: str) -> str:
    """First meaningful token of a shell command, skipping VAR=val prefixes.

    Known limitation (accepted): compound commands ("cd x && pytest") yield
    the first command's head. Pairing is a heuristic, not a proof.
    """
    for tok in command.split():
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            continue
        return tok
    return ""


def record_surfaced(state_dir: Path, sig: str, results: list[dict]) -> None:
    """Remember which commons traces were surfaced for this error signature.

    The ids were formatted into the agent's context and then dropped, so a
    fix the commons actually supplied was indistinguishable from one the
    agent found alone — commons consumption reported flat zero while the
    local cache was observable. Persisting the ids at injection time is what
    lets pair_resolution attribute them when the same signature resolves.

    Structural only: signature, ids, timestamp. No content, no user text.
    Never raises — append_event already swallows OSError.
    """
    if not sig:
        return
    ids = []
    for r in results:
        if not isinstance(r, dict):
            continue
        # Same sanitization as the trailer: ids reach a JSON hook payload and
        # a URL, so strip anything that is not id-shaped and cap the length.
        safe = re.sub(r"[^A-Za-z0-9_-]", "", str(r.get("id", "")))[:64]
        if safe and safe not in ids:
            ids.append(safe)
    if not ids:
        return
    append_event(state_dir, SURFACED_FILE, {"sig": sig, "trace_ids": ids[:5]})


def _attribute_surfaced_commons(conn, state_dir: Path, sig: str,
                                err_t: float) -> None:
    """Credit a commons trace surfaced for `sig` when that signature resolves.

    Evidence standard, deliberately identical to the one already blessed for
    the ``local:`` marker: the trace was put in front of the agent earlier in
    THIS session, for THIS exact error signature, and the command that was
    failing now succeeds. Signature equality and ordering only.

    Two guards keep the claim honest rather than merely non-zero:

    * If a commons trace was already consumed in this window, the agent
      actually called get_trace and named the trace that helped. That exact
      evidence stands; the rank-1 heuristic below must never override it.
    * A trace already credited in this session is not credited twice. When
      the same signature recurs it is the locally cached fix paying off —
      crediting the commons again would double-count one act of help.

    Which id: rank 1 of the most recent surfacing for this signature. The
    agent saw the whole (max 3) list, so this is a heuristic — it is the
    search ranking's own best answer and the first line the agent read.
    """
    surfaced = [e for e in read_events(state_dir, SURFACED_FILE)
                if e.get("sig") == sig
                and isinstance(e.get("trace_ids"), list) and e["trace_ids"]]
    if not surfaced:
        return
    row = conn.execute(
        "SELECT 1 FROM trigger_feedback WHERE session_id = ? "
        "AND trace_consumed_id IS NOT NULL "
        "AND trace_consumed_id NOT LIKE 'local:%' AND consumed_at >= ? "
        "LIMIT 1",
        (state_dir.name, err_t),
    ).fetchone()
    if row:
        return
    trace_id = surfaced[-1]["trace_ids"][0]
    already = conn.execute(
        "SELECT 1 FROM trigger_feedback "
        "WHERE session_id = ? AND trace_consumed_id = ? LIMIT 1",
        (state_dir.name, trace_id),
    ).fetchone()
    if already:
        return
    from local_store import record_trace_consumed
    record_trace_consumed(conn, state_dir.name, trace_id)


def _pair_resolution(state_dir: Path, command: str,
                     previous_errors: list[dict]) -> dict | None:
    """Pair a succeeding command with a prior error of the same command head.

    Structural signal: the command that failed now succeeds. Stores the fix
    (verification command + basenames of files changed since the error +
    any commons trace consumed since the error) on the signature row —
    the payload retrieval._check_error_recurrence injects when the signature
    recurs. If this signature's fix was injected earlier this session, the
    resolution is recorded as a consumed trigger (assisted resolution),
    which feeds the error_recurrence rate in the existing M22-gated
    telemetry. A commons trace counts as having contributed either when
    the agent read it with get_trace or when the hook itself surfaced it
    for this exact signature (see _attribute_surfaced_commons). When one
    did, returns a Resolved-with disclosure suggestion for the agent.
    Never raises.
    """
    try:
        head = _command_head(command)
        if not head:
            return None
        match = None
        for entry in reversed(previous_errors):
            if entry.get("source") != "bash" or not entry.get("sig"):
                continue
            if _command_head(entry.get("command", "")) == head:
                match = entry
                break
        if match is None:
            return None
        project_id = read_project_id(state_dir)
        if project_id is None:
            return None
        err_t = match.get("t", 0)

        # Files changed between the error and this success = the fix.
        # Basenames only — full paths can contain usernames.
        fix_files = []
        for ch in read_events(state_dir, "changes.jsonl"):
            if ch.get("t", 0) >= err_t and ch.get("file"):
                name = Path(ch["file"]).name
                if name not in fix_files:
                    fix_files.append(name)

        from local_store import (
            _get_conn, record_resolution, record_trace_consumed,
        )
        conn = _get_conn()
        # Commons first, local second — order is load-bearing, not cosmetic.
        # record_trace_consumed attaches to ONE unconsumed trigger row, so when
        # both claims apply the commons trace has to take that row: it carries
        # the stronger claim ("the shared knowledge base helped" vs "our own
        # cache helped") and it is what the disclosure trailer cites.
        _attribute_surfaced_commons(conn, state_dir, match["sig"], err_t)

        # Assisted resolution: fix injected earlier this session → it landed.
        # Recorded BEFORE the attribution lookup below, not after: the marker
        # this very resolution just earned has to be visible to the query that
        # decides the signature's trace_id, or the signature keeps trace_id
        # NULL forever — and NULL is exactly what resolutions_assisted and the
        # savings ledger filter out.
        injected = {e.get("sig") for e in
                    read_events(state_dir, "recurrence_injected.jsonl")}
        if match["sig"] in injected:
            record_trace_consumed(conn, state_dir.name,
                                  "local:" + error_hash(match["sig"]))

        # Trace consumed since the error → attribute it to the fix. A commons
        # trace outranks the local: marker when both exist: it carries the
        # stronger claim and it is what the disclosure trailer cites.
        trace_id = None
        try:
            row = conn.execute(
                "SELECT trace_consumed_id FROM trigger_feedback "
                "WHERE session_id = ? AND trace_consumed_id IS NOT NULL "
                "AND consumed_at >= ? "
                "ORDER BY (trace_consumed_id LIKE 'local:%') ASC, "
                "consumed_at DESC LIMIT 1",
                (state_dir.name, err_t),
            ).fetchone()
            if row:
                trace_id = row["trace_consumed_id"]
            # Same precedence, applied to the stored row: a local: marker must
            # never overwrite a commons id this signature already carries.
            # record_resolution's COALESCE only protects NULL, and the
            # recurrence injection renders trace_id as a real, fetchable id
            # ("use get_trace with ID ...") — a local: marker there is noise.
            if trace_id and str(trace_id).startswith("local:"):
                prior = conn.execute(
                    "SELECT trace_id FROM error_signatures "
                    "WHERE project_id = ? AND signature = ?",
                    (project_id, match["sig"]),
                ).fetchone()
                if prior and prior["trace_id"] and not str(
                        prior["trace_id"]).startswith("local:"):
                    trace_id = None
        except Exception:
            trace_id = None

        record_resolution(conn, project_id, match["sig"],
                          fix_command=redact_command(command[:200]),
                          fix_files=fix_files[:10],
                          trace_id=trace_id)

        # Disclosure trailer: only for commons traces, never local markers
        trailer_output = None
        if trace_id and not str(trace_id).startswith("local:"):
            trailer_output = _suggest_trailer(state_dir, trace_id)
        conn.close()
        return trailer_output
    except Exception as e:
        log_hook_error("resolution_pairing", e)
        return None


def _suggest_trailer(state_dir: Path, trace_id: str) -> dict | None:
    """Resolved-with disclosure trailer — citation, not co-authorship.

    Fires once per (session, trace). Config gate: "resolved_with_trailer"
    (default on). The one-line opt-out is surfaced exactly once ever, on
    first use ("trailer_notice_shown" persisted to config).
    """
    # Sanitize trace_id at entry: only alphanumeric and hyphens, max 64 chars.
    # Prevents newlines/quotes/control chars from corrupting hook-protocol JSON.
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", str(trace_id))[:64]
    if not safe_id:
        return None

    config = ct_config.read_config()
    if not config.get("resolved_with_trailer", True):
        return None
    suggested = {e.get("trace_id") for e in
                 read_events(state_dir, "trailer_suggested.jsonl")}
    if safe_id in suggested:
        return None
    append_event(state_dir, "trailer_suggested.jsonl", {"trace_id": safe_id})
    parts = [
        f"CommonTrace: trace {safe_id} contributed to this fix. "
        f"If a commit comes out of it, the disclosure trailer is:\n"
        f"Resolved-with: CommonTrace https://commontrace.org/t/{safe_id}\n"
        f"(Citation, not co-authorship — add it at the end of the commit "
        f"message if the user is fine with it.)"]
    if not config.get("trailer_notice_shown"):
        parts.append('One-line opt-out: set "resolved_with_trailer": false '
                     "in ~/.commontrace/config.json.")
        # Fresh-config RMW: re-read the file so we don't clobber concurrent
        # writes from other hook processes that ran between our initial load
        # and this save. Also update the in-memory dict so callers stay
        # consistent.
        fresh_config = ct_config.read_config() or dict(config)
        fresh_config["trailer_notice_shown"] = True
        config["trailer_notice_shown"] = True
        ct_config.write_config(fresh_config)
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                   "additionalContext": " ".join(parts)}}
