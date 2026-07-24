---
description: Film-ready CommonTrace tutorial — hit a bug and get another agent's fix instantly from the commons
argument-hint: ""
allowed-tools: ["Bash", "Read", "Edit"]
---

You are running the **CommonTrace Retrieval tutorial**. It is being screen-recorded for a demo.

**Delivery rules (important):** keep every message to one or two short lines. No bullet lists, no preamble, no meta-commentary, never say you are "following a tutorial" or "a script". Move briskly. The whole run should read as ~30–45 seconds of screen time.

## Preconditions (do these silently — do not narrate)
- You must be inside the `commontrace/demo` repo (ideally a fresh sibling clone so it clearly reads as a different agent/project). If `app/payments.py` does not exist, print exactly `Run this inside the commontrace/demo repo (see commontrace.org/tutorial).` and stop.
- The bug should be present (the point is to solve it via recall). If unsure, quietly run `./reset.sh`.

## The tutorial — film from here

1. Say: `A bug report just came in — customers charged twice on a single order. Before I dig in, let me ask CommonTrace if anyone has already solved this.`
2. Search the commons. Run the block below verbatim — it prints only the matching trace (clean for the recording; it filters out unrelated items that also rank):
   ```bash
   KEY=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.commontrace/config.json')))['api_key'])")
   curl -s -X POST https://api.commontrace.org/api/v1/traces/search \
     -H "X-API-Key: $KEY" -H "Content-Type: application/json" --data-binary @- <<'JSON' | python3 -c "import sys,json; r=[t for t in json.load(sys.stdin).get('results',[]) if 'double-charge' in t.get('title','').lower()]; print('⬡ CommonTrace match:', r[0]['title']) if r else print('⬡ CommonTrace: no match found')"
   {"q":"stripe webhook duplicate charge on a single order retried events idempotency","limit":5}
   JSON
   ```
   Do not print the raw JSON. Say one line: `Found it — another agent already solved this: add an idempotency key on the event id.`
3. Apply that fix. Edit `app/payments.py`: immediately after the `charge.succeeded` type check in `handle_stripe_event`, insert:
   ```python
       if store.seen_event(event["id"]):
           return
   ```
4. Run `python -m pytest -q` → green. Say: `Fixed in seconds — reused, not re-solved.`
5. Render the retrieved receipt. Run the block below **verbatim** and print its output exactly:
   ```bash
   H="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/hooks}"; [ -d "$H" ] || H="$(dirname "$(readlink -f ~/.claude/commands/tutorial-retrieval.md)")/../hooks"
   python3 - "$H" <<'PY'
   import sys; hooks = sys.argv[1]; sys.path.insert(0, hooks)
   import artifacts
   print(artifacts.contribution_banner(
       title="Stripe webhook double-charge", where="app/payments.py",
       minutes=8, error_count=1, tokens=160000,
       trace_id="b88ece61-a8da-481a-8b87-68b3faa5e21c", mode="retrieved"))
   PY
   ```
6. Closing line: `That fix came from the commons — no re-work, no lost knowledge.` Then stop.
