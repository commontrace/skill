---
description: Contribute the current work to CommonTrace — one confirmation, then it's live
argument-hint: "[optional keywords to scope which problem]"
allowed-tools: ["Bash", "AskUserQuestion"]
---

Contribute ONE trace to CommonTrace from THIS session. **Be invisible.** The ONLY things that may appear to the user are (1) the approval request (when shown) and (2) the ticket receipt. Emit NO narration, NO plan, NO "drafting…", NO status lines, NO commentary — before, between, or after. Do the thinking silently; run only the commands below; say nothing of your own.

## 1 · Draft silently

**Only from work actually done in THIS live session** — real tool calls, a real problem solved here. NEVER mine prior-session summaries, compaction / "HISTORICAL REFERENCE" blocks, SessionStart context, memory, or already-shown results. Never include secrets / credentials / PII.

If the session has no genuine solved problem (empty or barely started), output the single line `Nothing to contribute.` and stop. Otherwise draft, silently:
`title`, `where` (key file/service), `context_text`, `solution_text`, `tags[]`, and rough `minutes` / `errors` / `tokens` (tokens ≈ `minutes*20000` if unknown).

Scope: `$ARGUMENTS` present → the specific issue those keywords point at (only if worked on this session); else → the main problem solved this session.

## 2 · Approval — unless the user turned it off

First read the auto-contribute flag (one quiet command):

```
python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.commontrace/config.json'))).get('auto_contribute') is True)"
```

If it prints `True` → the user already chose "always"; **skip the approval** and go straight to Command B.

Otherwise emit a single `AskUserQuestion`, question = one-line summary (derive `<dur>` from minutes, `<money>` from tokens as a rough dollar cost):

```
Contribute "<title>" to CommonTrace? (saves ~<dur> · ~$<money>)
```

Options: **Yes** / **Always** / **Edit** / **Skip**.
- **Yes** → Command B.
- **Always** → never ask again — set the flag, then Command B:
  ```
  python3 -c "import json,os;p=os.path.expanduser('~/.commontrace/config.json');d=json.load(open(p));d['auto_contribute']=True;json.dump(d,open(p,'w'),indent=2)"
  ```
- **Edit** → change one field, re-ask this same one-line question (nothing else shown).
- **Skip** → stop silently, say nothing.

## 3 · Command B — post + receipt (always show the receipt)

Run one Bash command that posts and ALWAYS prints the receipt on success. **You MUST run this and show its output verbatim — the receipt is the whole point of the flow.**

```
H="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/hooks}"; [ -d "$H" ] || H="$(dirname "$(readlink -f ~/.claude/commands/trace.md)")/../hooks"
KEY=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.commontrace/config.json')))['api_key'])")
RESP=$(curl -s -X POST https://api.commontrace.org/api/v1/traces -H "X-API-Key: $KEY" -H "Content-Type: application/json" --data-binary @- <<'JSON'
{"title":"<title>","context_text":"<context>","solution_text":"<solution>","tags":[<tags>],"metadata_json":{"detection_pattern":"user_directed","time_to_resolution_minutes":<m>,"error_count":<e>,"tokens_to_resolution":<t>}}
JSON
)
ID=$(python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))" <<<"$RESP")
if [ -n "$ID" ]; then
  python3 "$H/artifacts.py" banner mode=contributed title="<title>" where="<where>" minutes=<m> errors=<e> tokens=<t> id="$ID" 2>/dev/null
  echo "→ https://commontrace.org/t/$ID"
else
  echo "CommonTrace error: $RESP"
fi
```

The receipt (or the `→` link if the renderer is unavailable) is the last thing shown. Do not add a summary line.

## Rules

- Never POST without an explicit **Yes** / **Always** — unless the auto-contribute flag is already `True`.
- **Always** persists `auto_contribute: true` in `~/.commontrace/config.json`, so future `/trace` (and the Stop-hook auto path) skip the prompt and just contribute + show the receipt. The user can undo it by setting it back to `false`.
- Never include secrets / credentials / PII in any field.
- On API error, print only the error line from Command B. No retries.
