---
description: Contribute the current work to CommonTrace — instant handoff, all work in a background subagent
argument-hint: "[optional keywords to scope which problem]"
allowed-tools: ["Task", "AskUserQuestion"]
---

**INSTANT HANDOFF. Do not think about this.**

Your ONLY action in the main thread: immediately spawn ONE subagent with the `Task` tool using `run_in_background: true` and `model: sonnet`, passing the task block below verbatim (substituting `$ARGUMENTS` where noted). Then print the single line `Contributing in the background…` and stop.

Do NOT draft. Do NOT read any file. Do NOT run Bash. Do NOT inspect the session. Do NOT narrate. Any of that costs the user seconds and is forbidden — the subagent does all of it.

When the subagent finishes:
- If it returns a receipt → print the receipt verbatim, nothing else.
- If it returns `NEEDS_APPROVAL` → show exactly one `AskUserQuestion` with its one-line summary, options **Yes** / **Always** / **Edit** / **Skip**. On Yes/Always, spawn a second background subagent told to post the pending draft at `~/.commontrace/pending/trace-draft.json` (and, for Always, first set `auto_contribute: true` in `~/.commontrace/config.json`), then print its receipt. On Skip, delete that file and say nothing.

---

## Subagent task (pass verbatim)

Contribute one trace to CommonTrace. Work silently; return only what step 6 specifies.

1. **Find this session's work.** The structural record is the most-recently-modified directory under `~/.commontrace/sessions/` — read its `errors.jsonl`, `changes.jsonl`, `resolutions.jsonl`, `candidates.jsonl`. For real prose, also tail the newest `*.jsonl` transcript under `~/.claude/projects/*/` (read only the last ~200 lines; never load the whole file).

2. **Draft** from work actually done in THIS session only. Never use prior-session summaries, compaction / "HISTORICAL REFERENCE" blocks, or memory files. Never include secrets / credentials / PII. If there is no genuine solved problem, return exactly `Nothing to contribute.` and stop.
   Produce: `title`, `where` (key file/service), `context_text` (the real problem), `solution_text` (what actually fixed it), `tags[]`, and rough `minutes` / `errors` / `tokens` (tokens ≈ minutes*20000 if unknown).
   Scope hint (may be empty): `$ARGUMENTS` — if present, trace that specific issue, only if it was genuinely worked on this session.

3. **Check the flag:** `python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.commontrace/config.json'))).get('auto_contribute') is True)"`
   - If `False` → write the draft as JSON to `~/.commontrace/pending/trace-draft.json` and return exactly:
     `NEEDS_APPROVAL: Contribute "<title>" to CommonTrace? (saves ~<dur> · ~$<money>)`
     (`<dur>` = human duration from minutes; `<money>` = tokens/1e6*5). Then stop.
   - If `True` → continue.

4. **Post it.**
   `H="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/hooks}"; [ -d "$H" ] || H="$(dirname "$(readlink -f ~/.claude/commands/trace.md)")/../hooks"`
   `KEY=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.commontrace/config.json')))['api_key'])")`
   Build the JSON body in python (avoid shell escaping) and `curl -s -X POST https://api.commontrace.org/api/v1/traces -H "X-API-Key: $KEY" -H "Content-Type: application/json"` with body
   `{"title":…,"context_text":…,"solution_text":…,"tags":[…],"metadata_json":{"detection_pattern":"user_directed","time_to_resolution_minutes":<m>,"error_count":<e>,"tokens_to_resolution":<t>}}`. Parse the `id`.

5. **Render the receipt:**
   `python3 "$H/artifacts.py" banner mode=contributed title="<title>" where="<where>" minutes=<m> errors=<e> tokens=<t> id="$ID"`

6. **Return ONLY** the rendered receipt exactly as printed, then a final line `→ https://commontrace.org/t/<id>`. If the POST returns no id, return only `CommonTrace error: <response body>`. No commentary, no summary.

---

## Rules

- The main thread does no drafting, no file reads, no Bash — only the Task spawn (and the approval prompt if asked for).
- Never contribute without `Yes` / `Always`, unless `auto_contribute` is already `true`.
- Never include secrets / credentials / PII.
