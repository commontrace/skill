---
description: Contribute to CommonTrace — targeted from the discussion (/trace <keywords>) or review pending
argument-hint: "[keywords about the issue to trace, e.g. gandi http]"
allowed-tools: ["mcp__commontrace__contribute_trace", "mcp__commontrace__amend_trace", "mcp__commontrace__search_traces", "mcp__commontrace__list_tags", "Read", "Bash", "AskUserQuestion"]
---

`$ARGUMENTS` decides the mode:

- **Arguments present** (e.g. `/trace gandi http`) → **Targeted suggestion** flow below. Treat the arguments as keywords pointing at ONE specific problem discussed in THIS conversation.
- **No arguments** (`/trace`) → skip to **Step 1** and review pending candidates.

## Targeted suggestion (when `$ARGUMENTS` is non-empty)

1. **Locate the issue.** Using the keywords `$ARGUMENTS` as a hint, scan the current conversation for the specific problem the user is pointing at — the exact error/blocker and how it was resolved. Do NOT invent; only use what was actually discussed. If nothing in the conversation matches the keywords, say so and stop.
2. **Check for duplicates** with `mcp__commontrace__search_traces` on the issue. If a near-identical trace already exists, tell the user and offer to amend it instead of creating a new one.
3. **Draft the trace** from the discussion:
   - `title` — short, specific (e.g. "Gandi DNS: HTTP-01 ACME fails, use DNS-01").
   - `context_text` — the problem: symptom, stack, what was tried.
   - `solution_text` — what actually fixed it.
   - `tags` — language / framework / domain; use `list_tags` to align with existing tags.
   - Estimate `minutes` and `errors` from the conversation (rough is fine); `tokens` best-effort (`0` if unknown → money shows `~$0.00`).
4. **Show the SUGGEST receipt** — render and print verbatim inside a code block:

   ```
   python3 "<skill-hooks-dir>/artifacts.py" banner mode=suggest \
     title="<draft title>" \
     where="<key file or service, e.g. gandi>" \
     minutes=<estimate> errors=<estimate> tokens=<estimate>
   ```

5. **Ask approval** with `AskUserQuestion` (single choice): `Yes` → submit · `Edit` → refine a field, re-show the receipt · `Skip` → abort, save nothing.
6. **On `Yes`** — call `mcp__commontrace__contribute_trace` with the drafted `title` / `context_text` / `solution_text` / `tags` and `metadata_json = {"detection_pattern": "user_directed", "time_to_resolution_minutes": <minutes>, "error_count": <errors>, "tokens_to_resolution": <tokens>}`. Then re-render the receipt in **`mode=contributed`** with `id=<new-trace-id>` and print it. Stop here — do not fall through to Step 1.

`<skill-hooks-dir>` is the directory holding this plugin's hooks (the same `artifacts.py` the Stop hook uses).

---

You are reviewing pending CommonTrace contribution candidates and walking the user through approval.

## Step 1 — Read pending candidates

Run `ls -1 ~/.commontrace/pending/*.jsonl 2>/dev/null` then read each file. Each line is one candidate JSON object with these fields:

- `kind` — `"score"` (new contribution) or `"amend"` (suggest improving an existing trace)
- `title` — short auto-generated title
- `top_pattern` — detection pattern (e.g. `user_correction`, `error_resolution`)
- `suggested_context_text` — pre-filled context
- `suggested_solution_text` — pre-filled solution
- `suggested_tags` — pre-filled tags
- `metadata_json` — detection metadata (pass through verbatim on submit)
- `struggle_grid` — optional share line (emoji struggle grid + stats); not submitted, used for display after a successful contribution
- `trace_id` — only present for `amend` kind

If there are zero pending candidates, ask the user if they want to contribute a fresh trace from scratch instead. If yes, follow the "Fresh contribution" flow below. If no, exit.

## Step 2 — Iterate over each candidate

For every candidate, use `AskUserQuestion` with this single question:

**Question**: `Save "<title>" as CommonTrace trace?`
**Options**:
- `Yes` — submit as-is using suggested fields
- `No` — discard this candidate
- `Edit` — refine title / context / solution / tags before submit

Multi-select must be **disabled** (single choice).

## Step 3 — Act on user choice

### Yes
For `kind: score`:
Call `mcp__commontrace__contribute_trace` with:
- `title` = candidate.title
- `context_text` = candidate.suggested_context_text (if empty, use a one-line description like "Detected pattern: <top_pattern>")
- `solution_text` = candidate.suggested_solution_text (if empty, use "Resolution captured automatically from session activity.")
- `tags` = candidate.suggested_tags
- `metadata_json` = candidate.metadata_json **verbatim**

For `kind: amend`:
Call `mcp__commontrace__amend_trace` with the trace_id and ask the user one short question for the proposed solution_text improvement. Submit.

After successful submit, delete the candidate's line from the pending file (use `sed` or rewrite the file without that line). Report the new trace ID.

Then **show the CommonTrace receipt** — run the skill's banner renderer and print its output verbatim inside a code block:

```
python3 "<skill-hooks-dir>/artifacts.py" banner \
  title="<candidate.title>" \
  where="<basename of first evidence file, else candidate.top_pattern>" \
  minutes=<metadata_json.time_to_resolution_minutes> \
  errors=<metadata_json.error_count> \
  tokens=<metadata_json.tokens_to_resolution> \
  id=<new-trace-id>
```

`<skill-hooks-dir>` is the directory holding this plugin's hooks (the same `artifacts.py` the Stop hook uses). The receipt is the recognizable ⬡ CommonTrace figure showing the time/money this trace saves others or the contributor's future self — the same banner shown automatically on auto-contributions. It is a paste-anywhere share line.

### No
Delete the candidate's line from the pending file. Move to next candidate without further questions.

### Edit
Ask the user **one short question per field they want to change** using `AskUserQuestion` with a free-text option (provide reasonable defaults from the candidate). Stop refining once the user is satisfied. Then submit as in "Yes".

## Step 4 — Final report

After processing all candidates, report a one-line summary: `Contributed N traces, discarded M, kept K for later.`

## Fresh contribution flow

If user wants to contribute from scratch (no pending), ask the following one at a time via `AskUserQuestion`:
1. What problem did you solve? (free text → `context_text`)
2. What was the solution? (free text → `solution_text`)
3. Short title? (free text → `title`)
4. Tags? Use `list_tags` to surface existing ones; user picks or types new.

Then preview and ask one final `Yes/No` confirmation before submitting.

## Rules

- **Never submit without an explicit "Yes" answer** for that specific candidate.
- **Always remove processed candidates** from the pending file (whether accepted or discarded) so they aren't re-shown.
- If the API or MCP server is unavailable, report the failure and leave the candidate in the pending file for retry.
- Never use `decision: block` style nags — this command runs only when the user invokes it.
