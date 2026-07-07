---
description: Contribute the current work to CommonTrace — one confirmation, then it's live
argument-hint: "[optional keywords to scope which problem]"
allowed-tools: ["Bash", "AskUserQuestion"]
---

Contribute ONE trace to CommonTrace from THIS session. **Be invisible.** The ONLY things that may appear to the user are (1) the approval request and (2) the ticket receipt. Emit NO narration, NO plan, NO "drafting…", NO status lines, NO commentary — before, between, or after. Do the thinking silently; run only the single command below; say nothing of your own.

## 1 · Draft silently

**Only from work actually done in THIS live session** — real tool calls, a real problem solved here. NEVER mine prior-session summaries, compaction / "HISTORICAL REFERENCE" blocks, SessionStart context, memory, or already-shown results. Never include secrets / credentials / PII.

If the session has no genuine solved problem (empty or barely started), output the single line `Nothing to contribute.` and stop. Otherwise draft, silently:
`title`, `where` (key file/service), `context_text`, `solution_text`, `tags[]`, and rough `minutes` / `errors` / `tokens` (tokens ≈ `minutes*20000` if unknown).

Scope: `$ARGUMENTS` present → the specific issue those keywords point at (only if worked on this session); else → the main problem solved this session.

## 2 · Ask for approval (one line, nothing else)

No shell command here — no receipt, no preview. Emit a single `AskUserQuestion` whose question is a one-line text summary, exactly this shape (derive `<dur>` from the drafted minutes as a human duration, `<money>` from the drafted tokens as a rough dollar cost):

```
Contribute "<title>" to CommonTrace? (saves ~<dur> · ~$<money>)
```

Options: **Yes** / **Edit** / **Skip**.
- **Yes** → proceed to Command B.
- **Edit** → change one field, then re-ask this same one-line question (nothing else shown).
- **Skip** → stop silently, say nothing.

## 3 · Command B — on Yes only: post + receipt (nothing else)

Run exactly one Bash command that posts and prints the final receipt:

```
H="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/hooks}"; [ -d "$H" ] || H="$(dirname "$(readlink -f ~/.claude/commands/trace.md)")/../hooks"
KEY=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.commontrace/config.json')))['api_key'])")
RESP=$(curl -s -X POST https://api.commontrace.org/api/v1/traces -H "X-API-Key: $KEY" -H "Content-Type: application/json" --data-binary @- <<'JSON'
{"title":"<title>","context_text":"<context>","solution_text":"<solution>","tags":[<tags>],"metadata_json":{"detection_pattern":"user_directed","time_to_resolution_minutes":<m>,"error_count":<e>,"tokens_to_resolution":<t>}}
JSON
)
ID=$(python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))" <<<"$RESP")
[ -n "$ID" ] && python3 "$H/artifacts.py" banner mode=contributed title="<title>" where="<where>" minutes=<m> errors=<e> tokens=<t> id="$ID" && echo "→ https://commontrace.org/t/$ID" || echo "CommonTrace error: $RESP"
```

That receipt is the last thing shown. Do not add a summary line.

## Rules

- Never POST without an explicit **Yes**.
- Never include secrets / credentials / PII in any field.
- Exactly one shell command total (the POST in Command B). No exploratory, receipt, or status commands.
- On API error, print only the error line from Command B. No retries.
