---
description: Contribute the current work to CommonTrace — one confirmation, then it's live
argument-hint: "[optional keywords to scope which problem]"
allowed-tools: ["Bash", "AskUserQuestion"]
---

Contribute ONE trace to CommonTrace from THIS conversation. Fast path only: **draft → one confirm → POST → receipt.** The endpoint and body schema are below — do NOT go rediscover them.

Resolve the hooks dir once (for the receipt renderer):

```
HOOKS="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/hooks}"
[ -d "$HOOKS" ] || HOOKS="$(dirname "$(readlink -f ~/.claude/commands/trace.md)")/../hooks"
```

## 1 · Draft (one shot, from the discussion)

**Only draw from work actually done in THIS live session** — real tool calls you ran, a real problem you solved here. **NEVER mine injected/stale context:** prior-session summaries, compaction or "HISTORICAL REFERENCE" blocks, SessionStart context, memory files, or CommonTrace results already shown. Those are background, not this session's work.

**If the live session did no substantive technical work, stop:** print `Nothing worth contributing from this session.` and do nothing else. Do NOT fabricate a trace from context. An empty or barely-started session must produce no trace.

Pick the single problem worth sharing:
- `$ARGUMENTS` present → the specific issue those keywords point at, **only if it was genuinely worked on in this session** (else say so and stop).
- else → the main problem solved in this conversation.

Write, from what ACTUALLY happened (never template text):
- `title` — short, specific
- `context_text` — the real problem: symptom + stack + what was tried
- `solution_text` — what actually fixed it
- `tags` — array: language / framework / domain
- rough `minutes` and `errors` from the discussion; `tokens` ≈ a rough proxy for the solver's cost (if unsure, `minutes * 20000`)

## 2 · Confirm (one question)

Show the suggestion receipt, then ask once:

```
python3 "$HOOKS/artifacts.py" banner mode=suggest title="<title>" where="<key file or service>" minutes=<m> errors=<e> tokens=<t>
```

`AskUserQuestion` — "Contribute this to CommonTrace?" → **Yes** / **Edit** / **Skip**. Skip → stop. Edit → change one field, re-show.

## 3 · POST (only on Yes)

```
KEY=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.commontrace/config.json')))['api_key'])")
curl -s -w '\n%{http_code}' -X POST https://api.commontrace.org/api/v1/traces \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  --data-binary @- <<'JSON'
{"title":"...","context_text":"...","solution_text":"...","tags":["..."],"metadata_json":{"detection_pattern":"user_directed","time_to_resolution_minutes":<m>,"error_count":<e>,"tokens_to_resolution":<t>}}
JSON
```

Read the `id` from the JSON response (last line is the HTTP status).

## 4 · Receipt

```
python3 "$HOOKS/artifacts.py" banner mode=contributed title="<title>" where="<key file or service>" minutes=<m> errors=<e> tokens=<t> id=<id>
```

Then one line: `Contributed → https://commontrace.org/t/<id>`. If `$HOOKS/artifacts.py` is missing, skip the receipt and just print that line.

## Rules

- Never POST without an explicit **Yes**.
- **Never include secrets** — passwords, tokens, API keys, private keys, PII — in any field, even if they appear in the conversation or context. Omit or redact.
- On any API error (status ≥ 400): show the status + body and stop. No retries.
- No `$ARGUMENTS` AND `~/.commontrace/pending/*.jsonl` has entries → first offer to review those instead: one Yes/No each, same POST, then delete the handled line from the file.
