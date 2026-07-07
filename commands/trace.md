---
description: Contribute the current work to CommonTrace — approval + receipt only; everything else runs in a hidden background subagent
argument-hint: "[optional keywords to scope which problem]"
allowed-tools: ["Read", "AskUserQuestion", "Task"]
---

Contribute ONE trace from THIS session with MINIMAL footprint. The user must see ONLY: (1) the approval prompt (skipped when auto-contribute is already on) and (2) the final receipt. Everything else — the API call, the render, any shell — runs INSIDE a background subagent and must stay hidden. Do NOT run Bash yourself. Do NOT narrate.

## 1 · Draft silently (main thread)

From work actually done in THIS live session only — real tool calls, a real problem solved here. NEVER mine prior-session summaries, compaction / "HISTORICAL REFERENCE" blocks, SessionStart context, memory, or already-shown results. Never include secrets / credentials / PII.

If the session has no genuine solved problem (empty or barely started), output the single line `Nothing to contribute.` and stop. Otherwise prepare silently, holding these in your head (do not print them):
`title`, `where` (key file/service), `context_text`, `solution_text`, `tags[]`, and rough `minutes` / `errors` / `tokens` (tokens ≈ `minutes*20000` if unknown). Also a `<dur>` (human duration from minutes) and `<money>` (rough $ from tokens, tokens/1e6*5).

Scope: `$ARGUMENTS` present → the specific issue those keywords point at (only if worked on this session); else → the main problem solved this session.

## 2 · Approval (main thread) — unless already on "Always"

`Read` the file `~/.commontrace/config.json`. If its `auto_contribute` is `true` → skip this step entirely and go to 3 (no prompt).

Otherwise emit exactly one `AskUserQuestion`, question = the one-line summary:

```
Contribute "<title>" to CommonTrace? (saves ~<dur> · ~$<money>)
```

Options: **Yes** / **Always** / **Edit** / **Skip**.
- **Yes** → go to 3.
- **Always** → go to 3, and include the "set the always flag" instruction in the subagent task.
- **Edit** → change one field, re-ask this same one-line question (nothing else shown).
- **Skip** → stop, say nothing.

## 3 · Hand off to a background subagent (hidden, non-blocking)

Spawn ONE subagent with the `Task` tool, **run_in_background: true**, so the user is not blocked and the shell stays hidden. Give it this task (fill in the drafted values; keep it self-contained):

> Contribute a trace to CommonTrace, then render its receipt. Steps, silently:
> 1. `H="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/hooks}"; [ -d "$H" ] || H="$(dirname "$(readlink -f ~/.claude/commands/trace.md)")/../hooks"`
> 2. `KEY=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.commontrace/config.json')))['api_key'])")`
> 3. POST to `https://api.commontrace.org/api/v1/traces` with header `X-API-Key: $KEY`, JSON body `{"title":"<title>","context_text":"<context>","solution_text":"<solution>","tags":[<tags>],"metadata_json":{"detection_pattern":"user_directed","time_to_resolution_minutes":<m>,"error_count":<e>,"tokens_to_resolution":<t>}}`. Parse the `id`.
> 4. If it was an "Always" contribution, also set the flag: `python3 -c "import json,os;p=os.path.expanduser('~/.commontrace/config.json');d=json.load(open(p));d['auto_contribute']=True;json.dump(d,open(p,'w'),indent=2)"`
> 5. Render the receipt: `python3 "$H/artifacts.py" banner mode=contributed title="<title>" where="<where>" minutes=<m> errors=<e> tokens=<t> id="$ID"`
> 6. Return ONLY the rendered receipt, then a final line `→ https://commontrace.org/t/<id>`. On any API error (no id), return only `CommonTrace error: <response body>`.

## 4 · Show the receipt (main thread)

When the subagent finishes, print its returned receipt verbatim — nothing else. No summary line, no narration.

## Rules

- Never contribute without an explicit **Yes** / **Always** — unless `auto_contribute` is already `true`.
- **Always** persists `auto_contribute: true` (done inside the subagent), so future `/trace` and the Stop-hook auto path skip the prompt. Undo by setting it back to `false`.
- Never include secrets / credentials / PII in any field.
- The main thread runs NO Bash — only Read, AskUserQuestion, and the background Task. All shell lives in the subagent.
