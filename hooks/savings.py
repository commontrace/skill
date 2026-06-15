"""Pure-local savings instrument — no network, no LLM, never asks a model.

Every money number here is a MEASURED token count multiplied by one
published price constant. Tokens come from the Stop hook's transcript
(message.usage) summed over a time window; the price is DEFAULT_PRICE_PER_MTOK
(config-overridable). sum_usage must NEVER raise — it returns 0 on any
failure (no path, missing file, bad JSON, bad usage values).

Phases 1-3 = MEASURED INBOUND ONLY. No "your traces saved others" clause
is emitted here; that outbound view is the server plan's job.
"""

import json
from datetime import datetime, timezone

DEFAULT_PRICE_PER_MTOK = 3.0
TOKEN_CAP = 2_000_000
TOKENS_PER_TURN_EST = 1500

_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

def _epoch(ts: str) -> float:
    """ISO-8601 (trailing 'Z') -> UTC epoch float, comparable to event 't'."""
    return (datetime.fromisoformat(ts.replace("Z", "+00:00"))
            .astimezone(timezone.utc).timestamp())

def sum_usage(transcript_path: str, start_t: float, end_t: float) -> int:
    """Sum message.usage tokens for transcript lines inside [start_t, end_t].

    Returns min(total, TOKEN_CAP). Returns 0 on ANY failure and never raises.
    """
    if not transcript_path:
        return 0
    total = 0
    try:
        with open(transcript_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                ts = obj.get("timestamp")
                if not isinstance(ts, str):
                    continue
                try:
                    epoch = _epoch(ts)
                except (ValueError, TypeError):
                    continue
                if not (start_t <= epoch <= end_t):
                    continue
                usage = (obj.get("message") or {}).get("usage") or {}
                for key in _USAGE_KEYS:
                    val = usage.get(key)
                    if isinstance(val, int) and not isinstance(val, bool):
                        total += val
    except (OSError, ValueError):
        return 0
    return min(total, TOKEN_CAP)

def money_usd(tokens: int, price_per_mtok: float = None) -> float:
    """Money saved = tokens / 1M * price. Price is the only chosen constant."""
    price = price_per_mtok if price_per_mtok is not None else DEFAULT_PRICE_PER_MTOK
    return round(tokens / 1_000_000 * price, 2)

def _hm(minutes: float) -> str:
    """Compact duration: '~Xm' under an hour, '~Yh' (no trailing .0) above."""
    if minutes >= 60:
        hours = round(minutes / 60, 1)
        text = str(hours)
        if text.endswith(".0"):
            text = text[:-2]
        return "~" + text + "h"
    return "~" + str(int(round(minutes))) + "m"

def format_recap_line(life: dict, delta: dict = None,
                      price_per_mtok: float = None) -> str:
    """Build the one-line session-start recap. INBOUND ONLY.

    life  = {"minutes": float, "tokens": int, "events": int} lifetime totals.
    delta = {"minutes": float, "tokens": int} since last session, or None.
    Returns "" when there is nothing to say.
    """
    parts = []
    if delta and (delta.get("minutes", 0) > 0 or delta.get("tokens", 0) > 0):
        parts.append(
            "saved you " + _hm(delta.get("minutes", 0)) + " ~$"
            + str(money_usd(delta.get("tokens", 0), price_per_mtok))
            + " since last session")
    if life.get("minutes", 0) > 0 or life.get("tokens", 0) > 0:
        parts.append(
            "lifetime " + _hm(life.get("minutes", 0)) + "/~$"
            + str(money_usd(life.get("tokens", 0), price_per_mtok)))
    if not parts:
        return ""
    return "CommonTrace: " + " · ".join(parts)
