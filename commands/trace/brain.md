---
description: Render your agent's brain — local knowledge graph from local.db
allowed-tools: ["Bash", "Read"]
---

You are generating the user's local CommonTrace brain artifacts.

## Step 1 — Generate

Run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/hooks/artifacts.py brain
```

## Step 2 — Report

Report the printed file paths and the stats line (N solved · M open · K projects). Tell the user:

- Open `~/.commontrace/artifacts/brain.html` in a browser to see the full page.
- To embed the badge in a README, copy `~/.commontrace/artifacts/badge.svg` into the repo and add: `![CommonTrace brain](./badge.svg)`

If the user asks for the monthly recap instead, run `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/artifacts.py recap` (optionally `recap YYYY-MM`).

## Rules

- Everything is generated locally from `~/.commontrace/local.db` — no network calls, aggregate shapes only (no code, no error text, no file names).
- If the command fails, report the error and stop — do not retry in a loop.
