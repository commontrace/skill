"""What the session leaves behind: local stats, savings, telemetry.

Everything in this module is best-effort bookkeeping that runs on the way
out of a session. Each entry point is wrapped end to end — a failure here
must never crash the Stop hook or block a contribution.

  _persist_session      — session row + cache pruning in local.db
  _book_savings         — minutes/tokens the commons saved you (inbound only)
  _session_counters     — per-session aggregates for the north-star metric
  _report_trigger_stats — anonymized trigger effectiveness, opt-in only (M22)
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ct_config
from scoring import compute_importance
from session_state import read_events, log_hook_error


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
    except Exception as e:
        log_hook_error("persist_session", e)


def _book_savings(data: dict, state_dir: Path) -> None:
    """Book measured-inbound savings for trace-attributed recurrences.

    INBOUND ONLY (what the commons saved you). For each error signature in
    THIS project that resolved with an attributed trace_id since this
    session's window floor, credit:
      minutes = sum of (resolved_at - created_at), capped 120 min/event
      tokens  = measured message.usage over the session window
    Wrapped end-to-end so it can never crash the Stop hook. No LLM.
    """
    try:
        from savings import sum_usage
        import local_store

        project_id_path = state_dir / "project_id"
        if not project_id_path.exists():
            return
        project_id = int(project_id_path.read_text(encoding="utf-8").strip())

        times = [e["t"] for e in
                 read_events(state_dir, "resolutions.jsonl")
                 + read_events(state_dir, "errors.jsonl")
                 if "t" in e]
        if not times:
            return
        floor = min(times) - 5

        conn = local_store._get_conn()
        try:
            rows = conn.execute(
                "SELECT created_at, resolved_at FROM error_signatures "
                "WHERE project_id = ? AND trace_id IS NOT NULL "
                "AND resolved_at IS NOT NULL AND resolved_at >= ?",
                (project_id, floor),
            ).fetchall()
            if not rows:
                return
            minutes = sum(
                min(max(r["resolved_at"] - r["created_at"], 0) / 60.0, 120.0)
                for r in rows)
            tokens = sum_usage(
                data.get("transcript_path", ""), min(times) - 5, max(times) + 5)
            local_store.book_session_saving(
                conn, project_id, data.get("session_id", ""), minutes, tokens)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log_hook_error("book_savings", e)


def _session_counters(conn, state_dir: Path, project_id) -> dict:
    """Per-session aggregates for the assisted-resolution north-star (§4.3).

    Scoped to THIS session: trigger_feedback rows keyed by state_dir.name,
    resolution events from resolutions.jsonl, and assisted resolutions =
    error signatures resolved with an attributed trace (commons ID or
    local: marker — record_resolution COALESCEs both into trace_id) since
    this session's first resolution event minus 5s grace. Capped at
    resolutions_total so a recurring signature never overcounts.
    """
    counters = {"searches_fired": 0, "traces_consumed": 0,
                "resolutions_total": 0, "resolutions_assisted": 0}
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS fired, "
            "SUM(CASE WHEN trace_consumed_id IS NOT NULL THEN 1 ELSE 0 END) "
            "AS consumed "
            "FROM trigger_feedback WHERE session_id = ?",
            (state_dir.name,)).fetchone()
        if row:
            counters["searches_fired"] = int(row["fired"] or 0)
            counters["traces_consumed"] = int(row["consumed"] or 0)
    except Exception as e:
        log_hook_error("session_counters_triggers", e)
    try:
        resolutions = read_events(state_dir, "resolutions.jsonl")
        counters["resolutions_total"] = len(resolutions)
        if resolutions and project_id is not None:
            floor = min(e.get("t", 0) for e in resolutions) - 5.0
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM error_signatures "
                "WHERE project_id = ? AND trace_id IS NOT NULL "
                "AND resolved_at >= ?",
                (project_id, floor)).fetchone()
            if row:
                counters["resolutions_assisted"] = min(
                    int(row["n"] or 0), counters["resolutions_total"])
    except Exception as e:
        log_hook_error("session_counters_resolutions", e)
    return counters


def _report_trigger_stats(data: dict, state_dir: Path) -> None:
    """Send anonymized trigger effectiveness stats to the API.

    M22: Only sends if user has opted in via telemetry=true in config.
    """
    try:
        # M22: Check telemetry consent before sending. No config = no consent.
        if not ct_config.read_config().get("telemetry", False):
            return
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
        counters = _session_counters(conn, state_dir, project_id)
        conn.close()

        if not stats and not any(counters.values()):
            return

        api_key = ct_config.load_api_key()
        if not api_key:
            return
        base_url = ct_config.api_base_url()

        body = {"trigger_stats": stats, "session_id": session_id}
        body.update(counters)
        payload = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(
            f"{base_url}/api/v1/telemetry/triggers",
            data=payload, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
            },
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception as e:
        log_hook_error("report_trigger_stats", e)
