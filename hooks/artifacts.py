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
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

ARTIFACTS_DIR = Path.home() / ".commontrace" / "artifacts"
TRACE_URL = "https://commontrace.org/t/{}"

_TEMP_BOUNDS = [(7, "hot"), (30, "warm"), (90, "cool"), (180, "cold")]
TEMP_COLORS = {"hot": "#e25822", "warm": "#e8a33d", "cool": "#4f86c6",
               "cold": "#7a8b99", "frozen": "#b9c4cc"}
KNOWN_PATTERNS = frozenset({
    "error_resolution", "security_hardening", "user_correction",
    "approach_reversal", "test_fix_cycle", "dependency_resolution",
    "config_discovery", "novelty_encounter", "infra_discovery",
    "migration_pattern", "research_then_implement", "generation_effect",
    "cross_file_breadth", "iteration_depth", "workaround",
    "temporal_investment", "fail_then_succeed",
})


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


BAR_GLYPHS = "▏▎▍▌▋▊▉█"  # 8 monotone bar widths, thin → thick
RECEIPT_WIDTH = 34


def _barcode(word="commontrace"):
    """Encode a word as a scannable-looking bar strip.

    Each letter → two bars (its 0-25 alphabet index split across 8 glyph
    widths, hi // 8 then lo % 8), thin separators between letters, guard bars
    at both ends. Deterministic and decorative — the strip literally spells
    the word. Non-letters are skipped. Never raises.
    """
    pairs = []
    for ch in str(word).lower():
        n = ord(ch) - 97
        if 0 <= n < 26:
            pairs.append(BAR_GLYPHS[n // 8] + BAR_GLYPHS[n % 8])
    if not pairs:
        return ""
    return "║" + "│".join(pairs) + "║"


def contribution_banner(title, where, minutes, error_count, tokens,
                        trace_id="", ts=None, mode="contributed"):
    """The recognizable CommonTrace receipt.

    Three states, one figure (mode=):
      "suggest"     — a proposed trace awaiting approval. "WOULD SAVE",
                      header SUGGESTED CONTRIBUTION, footer asks to approve.
      "contributed" — just saved (default). Present tense "SAVES TIME /
                      SAVES MONEY": this trace *will* save others / future you.
      "retrieved"   — a retrieval that paid off. Past tense "TIME SAVED /
                      MONEY SAVED": the commons *already* saved you this.

    Structural only: title/where are caller-supplied labels; everything else
    is a number. The footer barcode spells 'commontrace'. Never raises.
    """
    from savings import money_usd, fmt_duration
    try:
        money = money_usd(int(tokens or 0))
    except Exception:
        money = 0.0
    dur = fmt_duration(minutes or 0)
    tm = time.strftime("%Y-%m-%d   %H:%M", time.localtime(ts or time.time()))
    ref = (str(trace_id) or "pending")[:6] or "pending"
    plural = "s" if int(error_count or 0) != 1 else ""
    inner = RECEIPT_WIDTH - 6
    pad = " " * 7

    def row(label, value):
        gap = max(1, inner - len(label) - len(value))
        return "        " + label + " " * gap + value

    mode = (mode or "contributed").lower()
    if mode == "suggest":
        header = "        SUGGESTED CONTRIBUTION"
        time_label, money_label = "WOULD SAVE TIME", "WOULD SAVE MONEY"
        footer = ["        others + your future self",
                  "        approve?   →   yes   ·   skip"]
    elif mode == "retrieved":
        header = f"        KNOWLEDGE COMMONS   #{ref}"
        time_label, money_label = "TIME SAVED", "MONEY SAVED"
        footer = ["        for others + your future self",
                  "          ✨✨✨ thank you ✨✨✨"]
    else:  # contributed
        header = f"        KNOWLEDGE COMMONS   #{ref}"
        time_label, money_label = "SAVES TIME", "SAVES MONEY"
        footer = ["        for others + your future self",
                  "          ✨✨✨ thank you ✨✨✨"]

    lines = [
        "        ⬡ C O M M O N T R A C E",
        pad + "═" * RECEIPT_WIDTH,
        header,
        f"        {tm}",
        pad + "-" * RECEIPT_WIDTH,
        f"        ITEM        {str(title)[:20].rstrip()}",
        f"        WHERE       {str(where)[:20].rstrip()}",
        f"        EFFORT      {int(round(minutes or 0))}m · "
        f"{int(error_count or 0)} error{plural}",
        pad + "-" * RECEIPT_WIDTH,
        row(time_label, dur),
        row(money_label, f"~${money:.2f}"),
        pad + "═" * RECEIPT_WIDTH,
        *footer,
        "",
        pad + _barcode("commontrace"),
    ]
    return "\n".join(lines)


def load_brain_data(conn):
    """Brain-graph dataset. No text leaves the rows: nodes carry only
    numbers; project hubs carry only language/framework labels (never
    paths). Caps: 12 most-recent projects × 60 most-recent signatures."""
    now = time.time()
    projects = []
    solved = 0
    open_count = 0
    rows = conn.execute(
        "SELECT p.id, p.language, p.framework FROM projects p "
        "ORDER BY p.last_seen_at DESC LIMIT 12").fetchall()
    for p in rows:
        label = "/".join(x for x in (p["language"], p["framework"]) if x) \
            or "project"
        sigs = conn.execute(
            "SELECT seen_count, created_at, last_seen_at, resolved_at "
            "FROM error_signatures WHERE project_id = ? "
            "ORDER BY last_seen_at DESC LIMIT 60", (p["id"],)).fetchall()
        nodes = []
        for s in sigs:
            resolved = s["resolved_at"] is not None
            age_days = max(0.0, (now - s["last_seen_at"]) / 86400)
            nodes.append({
                "intensity": intensity(s["seen_count"], s["created_at"],
                                       s["resolved_at"]),
                "temperature": temperature(s["last_seen_at"], now),
                "resolved": resolved,
                "age_days": round(age_days, 1),
                "opacity": round(1.0 - 0.6 * min(age_days / 365.0, 1.0), 2),
            })
            if resolved:
                solved += 1
            else:
                open_count += 1
        if nodes:
            projects.append({"label": label, "nodes": nodes})
    return {"projects": projects, "solved": solved, "open": open_count,
            "now": now}


GOLDEN_ANGLE = 2.399963229728653


def _node_positions(n, cx, cy, spread):
    """Golden-angle spiral: organic, deterministic, no collisions at small n."""
    positions = []
    for i in range(n):
        r = spread * math.sqrt(i + 1)
        a = i * GOLDEN_ANGLE
        positions.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return positions


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_brain_svg(data, width=760, height=520):
    """Brain graph: one hub per project, error-signature nodes on a
    golden-angle spiral around it. Node size = intensity, color =
    temperature, fade = decay; resolved nodes are filled, open nodes
    hollow. Only numbers and language/framework labels are rendered.
    Hub orbits can overlap at high project counts — accepted as organic."""
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" role="img">',
        f'<rect width="{width}" height="{height}" fill="#fcfcf9"/>',
    ]
    projects = data.get("projects", [])
    if not projects:
        parts.append(
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
            f'font-family="Georgia, serif" font-size="16" fill="#777">'
            f'No knowledge captured yet — keep coding.</text>')
        parts.append("</svg>")
        return "".join(parts)
    n = len(projects)
    orbit = min(width, height) * 0.30 if n > 1 else 0.0
    for j, project in enumerate(projects):
        angle = j * (2 * math.pi / n) - math.pi / 2
        hx = width / 2 + orbit * math.cos(angle)
        hy = height / 2 + orbit * math.sin(angle)
        nodes = project["nodes"]
        positions = _node_positions(len(nodes), hx, hy, 11.0)
        for node, (x, y) in zip(nodes, positions):
            color = TEMP_COLORS[node["temperature"]]
            radius = 3.0 + 8.0 * node["intensity"]
            if node["resolved"]:
                parts.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" '
                    f'fill="{color}" opacity="{node["opacity"]}"/>')
            else:
                parts.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" '
                    f'fill="none" stroke="{color}" stroke-width="1.5" '
                    f'opacity="{node["opacity"]}"/>')
        label_y = hy + 11.0 * math.sqrt(len(nodes) + 1) + 18
        parts.append(
            f'<text x="{hx:.1f}" y="{label_y:.1f}" text-anchor="middle" '
            f'font-family="Georgia, serif" font-size="13" fill="#444">'
            f'{_esc(project["label"])}</text>')
    stats = (f'{data["solved"]} solved · {data["open"]} open · '
             f'{n} project{"s" if n != 1 else ""}')
    parts.append(
        f'<text x="{width / 2}" y="{height - 16}" text-anchor="middle" '
        f'font-family="Georgia, serif" font-size="13" fill="#555">'
        f'{_esc(stats)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def render_brain_html(data):
    """Self-contained share page: inline SVG, inline styles, no JS, no
    external assets. Safe to screenshot — aggregate shapes only."""
    svg = render_brain_svg(data)
    date_str = time.strftime(
        "%B %Y", time.localtime(data.get("now") or time.time()))
    legend = "".join(
        f'<span style="white-space:nowrap"><span style="display:inline-block;'
        f'width:10px;height:10px;border-radius:50%;background:{color};'
        f'margin:0 4px 0 12px"></span>{label}</span>'
        for label, color in TEMP_COLORS.items())
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My agent's brain — CommonTrace</title>
<style>
  body {{ font-family: Georgia, 'Times New Roman', serif; background: #fcfcf9;
         color: #202122; max-width: 820px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-weight: normal; border-bottom: 1px solid #a2a9b1;
       padding-bottom: 0.3rem; }}
  figure {{ margin: 1.5rem 0; }}
  .legend {{ font-size: 0.85rem; color: #555; }}
  footer {{ margin-top: 2rem; font-size: 0.8rem; color: #72777d;
           border-top: 1px solid #eaecf0; padding-top: 0.7rem; }}
</style>
</head>
<body>
<h1>My agent's brain</h1>
<p>Every dot is an error signature my coding agent fought and remembered.
Size is how hard the fight was, color is how recently the knowledge was
used, fade is decay. Filled dots are solved; hollow dots are still open.</p>
<figure>{svg}</figure>
<p class="legend">Temperature:{legend}</p>
<footer>Generated locally by CommonTrace on {date_str} — your agent's
memory, on your machine. Aggregate shapes only: no code, no error text,
no file names ever leave local.db.</footer>
</body>
</html>
"""


def render_badge_svg(data, width=360, height=72):
    """README-embeddable badge: solved count + a dot-strip of the most
    recent nodes (≤20, truncated to fit)."""
    nodes = [n for p in data.get("projects", []) for n in p["nodes"]][:20]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" role="img">',
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" '
        f'rx="8" fill="#fcfcf9" stroke="#a2a9b1"/>',
        f'<text x="16" y="28" font-family="Georgia, serif" font-size="14" '
        f'fill="#222">CommonTrace brain</text>',
        f'<text x="{width - 16}" y="28" text-anchor="end" '
        f'font-family="Georgia, serif" font-size="14" font-weight="bold" '
        f'fill="#2e7d32">{data.get("solved", 0)} solved</text>',
    ]
    x = 16.0
    for node in nodes:
        radius = 3.0 + 4.0 * node["intensity"]
        if x + 2 * radius > width - 24:
            break
        color = TEMP_COLORS[node["temperature"]]
        if node["resolved"]:
            parts.append(
                f'<circle cx="{x + radius:.1f}" cy="50" r="{radius:.1f}" '
                f'fill="{color}" opacity="{node["opacity"]}"/>')
        else:
            parts.append(
                f'<circle cx="{x + radius:.1f}" cy="50" r="{radius:.1f}" '
                f'fill="none" stroke="{color}" stroke-width="1.5" '
                f'opacity="{node["opacity"]}"/>')
        x += 2 * radius + 5
    parts.append("</svg>")
    return "".join(parts)


def compiled_recap(conn, year, month):
    """Monthly Compiled — the user's own numbers, never an interpretation.

    Returns the recap text, or None when the month had no sessions.
    Counts only; no signature text, paths, or titles ever appear here.
    """
    start, end = month_range(year, month)
    sess = conn.execute(
        "SELECT COUNT(*) AS n, SUM(error_count) AS errs, "
        "SUM(resolution_count) AS fixes, "
        "SUM(contribution_count) AS contribs "
        "FROM sessions WHERE started_at BETWEEN ? AND ?",
        (start, end)).fetchone()
    if not sess or not sess["n"]:
        return None
    solved = conn.execute(
        "SELECT COUNT(*) AS n, MAX(seen_count) AS worst "
        "FROM error_signatures WHERE resolved_at BETWEEN ? AND ?",
        (start, end)).fetchone()
    assisted = conn.execute(
        "SELECT COUNT(*) AS n FROM trigger_feedback "
        "WHERE trigger_name = 'error_recurrence' "
        "AND trace_consumed_id IS NOT NULL "
        "AND consumed_at BETWEEN ? AND ?", (start, end)).fetchone()
    top = conn.execute(
        "SELECT top_pattern, COUNT(*) AS n FROM sessions "
        "WHERE started_at BETWEEN ? AND ? AND top_pattern IS NOT NULL "
        "GROUP BY top_pattern ORDER BY n DESC LIMIT 1",
        (start, end)).fetchone()
    label = calendar.month_name[month]
    lines = [
        f"CommonTrace Compiled — {label} {year}",
        "",
        f"  {sess['n']} session{'s' if sess['n'] != 1 else ''}",
        f"  {sess['errs'] or 0} errors hit · {sess['fixes'] or 0} resolutions",
        f"  {solved['n'] or 0} error signature"
        f"{'s' if (solved['n'] or 0) != 1 else ''} solved for good",
    ]
    if assisted and assisted["n"]:
        lines.append(
            f"  {assisted['n']} repeat error"
            f"{'s' if assisted['n'] != 1 else ''} killed by memory — "
            f"knowledge that bit back")
    if solved and solved["worst"] and solved["worst"] > 1:
        lines.append(
            f"  hardest fight: one error took {solved['worst']} hits "
            f"before it fell")
    if top and top["top_pattern"]:
        label = (top["top_pattern"] if top["top_pattern"] in KNOWN_PATTERNS
                 else "unknown")
        lines.append(f"  signature move: {label.replace('_', ' ')}")
    if sess["contribs"]:
        lines.append(
            f"  {sess['contribs']} trace{'s' if sess['contribs'] != 1 else ''} "
            f"contributed to the commons")
    try:
        sav = conn.execute(
            "SELECT COALESCE(SUM(minutes_saved), 0), "
            "COALESCE(SUM(tokens_saved), 0) FROM savings_events "
            "WHERE created_at BETWEEN ? AND ?", (start, end)).fetchone()
        if sav and (sav[0] > 0 or sav[1] > 0):
            from savings import money_usd, fmt_duration
            lines.append(
                f"  the commons saved you {fmt_duration(sav[0])} / ~${money_usd(sav[1])} "
                f"this month")
    except sqlite3.OperationalError:
        pass
    lines.append("")
    lines.append("  Your agent's own numbers, from your machine. "
                 "— commontrace.org")
    return "\n".join(lines)


def write_artifact(name, content):
    """Write an artifact under ARTIFACTS_DIR with H9 perms (0700/0600)."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    ARTIFACTS_DIR.chmod(0o700)
    path = ARTIFACTS_DIR / name
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "brain"
    if cmd == "banner":
        # key=value args, e.g.:
        #   banner title="config discovery" where=sitemap.xml \
        #          minutes=35 errors=1 tokens=420000 id=ab12cd
        kv = {}
        for arg in argv[2:]:
            if "=" in arg:
                key, val = arg.split("=", 1)
                kv[key] = val

        def _num(name, cast, default=0):
            try:
                return cast(kv.get(name) or default)
            except (TypeError, ValueError):
                return default

        print(contribution_banner(
            title=kv.get("title", "contribution"),
            where=kv.get("where", ""),
            minutes=_num("minutes", float),
            error_count=_num("errors", int),
            tokens=_num("tokens", int),
            trace_id=kv.get("id", ""),
            mode=kv.get("mode", "contributed"),
        ))
        return 0
    from local_store import _get_conn
    conn = _get_conn()
    try:
        if cmd == "brain":
            data = load_brain_data(conn)
            html = write_artifact("brain.html", render_brain_html(data))
            svg = write_artifact("brain.svg", render_brain_svg(data))
            badge = write_artifact("badge.svg", render_badge_svg(data))
            print(f"brain page  : {html}")
            print(f"brain svg   : {svg}")
            print(f"readme badge: {badge}")
            print(f"{data['solved']} solved · {data['open']} open · "
                  f"{len(data['projects'])} projects")
            return 0
        if cmd == "recap":
            if len(argv) > 2:
                try:
                    parts = argv[2].split("-")
                    if len(parts) != 2:
                        raise ValueError("wrong part count")
                    year, month = int(parts[0]), int(parts[1])
                    if not (1 <= month <= 12):
                        raise ValueError("month out of range")
                except ValueError:
                    print(f"Invalid month argument: {argv[2]!r}. "
                          f"Expected YYYY-MM (e.g. 2026-05).",
                          file=sys.stderr)
                    return 1
            else:
                t = time.localtime()
                year, month = ((t.tm_year, t.tm_mon - 1) if t.tm_mon > 1
                               else (t.tm_year - 1, 12))
            text = compiled_recap(conn, year, month)
            if text:
                print(text)
                return 0
            print(f"No activity recorded for {year}-{month:02d}.")
            return 0
        if cmd == "savings":
            from savings import money_usd, fmt_duration
            from local_store import savings_totals
            totals = savings_totals(conn)
            if totals["events"] == 0:
                print("No savings recorded yet — keep using CommonTrace.")
                return 0
            print("CommonTrace savings (lifetime, inbound)")
            print(f"  time saved   : {fmt_duration(totals['minutes'])}")
            print(f"  money saved  : ~${money_usd(totals['tokens'])}")
            print(f"  events       : {totals['events']}")
            print("  Measured from your own resolutions, on your machine. "
                  "— commontrace.org")
            return 0
        print(f"Unknown command: {cmd}. "
              f"Usage: artifacts.py [brain|recap [YYYY-MM]|savings]")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
