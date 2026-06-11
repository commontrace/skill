"""Local-first viral artifacts: brain graph, struggle grid, monthly recap.

Everything here renders from local.db v3 — no network, no LLM calls, and
no text from the database ever reaches an artifact: only counts,
timestamps-derived numbers, language/framework labels, and trace IDs.
Share artifacts are aggregate shapes.

Server-side somatic_intensity / memory_temperature do not exist locally,
so this module derives local proxies from error_signatures:
- intensity: repeat-count + resolution latency (how hard the fight was)
- temperature: recency of last_seen_at (hot → frozen)
"""

import calendar
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

ARTIFACTS_DIR = Path.home() / ".commontrace" / "artifacts"
TRACE_URL = "https://commontrace.org/t/{}"

_TEMP_BOUNDS = [(7, "hot"), (30, "warm"), (90, "cool"), (180, "cold")]
TEMP_COLORS = {"hot": "#e25822", "warm": "#e8a33d", "cool": "#4f86c6",
               "cold": "#7a8b99", "frozen": "#b9c4cc"}


def temperature(last_seen_at, now=None):
    """Memory temperature from recency — local proxy for the server's
    activity-based temperature."""
    now = now if now is not None else time.time()
    age_days = max(0.0, (now - last_seen_at) / 86400)
    for bound, label in _TEMP_BOUNDS:
        if age_days < bound:
            return label
    return "frozen"


def intensity(seen_count, created_at, resolved_at):
    """Somatic-intensity proxy: how hard this knowledge was won.

    0.25 base + up to 0.6 for repeat encounters + up to 0.3 for a fight
    that took days to resolve. Capped at 1.0.
    """
    base = 0.25
    repeat = 0.15 * max(0, min(seen_count - 1, 4))
    latency = 0.0
    if resolved_at and resolved_at > created_at:
        latency_days = (resolved_at - created_at) / 86400
        latency = 0.3 * min(latency_days, 7.0) / 7.0
    return round(min(1.0, base + repeat + latency), 3)


def month_range(year, month):
    """(start, end) epoch seconds covering a local-time calendar month."""
    start = time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1))
    last_day = calendar.monthrange(year, month)[1]
    end = time.mktime((year, month, last_day, 23, 59, 59, 0, 0, -1))
    return start, end


GRID_CELLS = 10
CELL_ERROR, CELL_WORK, CELL_IDLE, CELL_SOLVED = "🟥", "🟨", "⬜", "🟩"


def struggle_grid(error_ts, change_ts, resolved=True):
    """Wordle-style struggle shape: the session timeline in 10 emoji cells.

    Spoiler-free by construction — built from event timestamps only, never
    from error text or file names. Red = errors, yellow = work, white =
    idle; the last cell turns green when the fight was won.
    """
    stamps = sorted(t for t in list(error_ts) + list(change_ts) if t)
    if not stamps:
        return CELL_SOLVED if resolved else CELL_IDLE
    start = stamps[0]
    span = max(stamps[-1] - start, 1.0)

    def bucket(t):
        return min(int((t - start) / span * GRID_CELLS), GRID_CELLS - 1)

    err_buckets = {bucket(t) for t in error_ts if t}
    chg_buckets = {bucket(t) for t in change_ts if t}
    cells = []
    for i in range(GRID_CELLS):
        if i in err_buckets:
            cells.append(CELL_ERROR)
        elif i in chg_buckets:
            cells.append(CELL_WORK)
        else:
            cells.append(CELL_IDLE)
    if resolved:
        cells[-1] = CELL_SOLVED
    return "".join(cells)


def struggle_line(grid, duration_min, error_count, trace_id=""):
    """The paste-anywhere share line under the grid."""
    duration = int(round(duration_min))
    plural = "s" if error_count != 1 else ""
    line = f"{grid} {duration}min · {error_count} error{plural} · solved"
    if trace_id:
        line += f" → {TRACE_URL.format(trace_id)}"
    return line
