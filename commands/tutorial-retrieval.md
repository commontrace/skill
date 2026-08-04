---
description: Film-ready CommonTrace tutorial — the fix surfaces on its own from the commons, then gets applied
argument-hint: ""
allowed-tools: ["Bash", "Read", "Edit"]
---

You are running the **CommonTrace Retrieval tutorial**. It is being screen-recorded for a demo.

**Delivery rules (important):** keep every message to one or two short lines. No bullet lists, no preamble, no meta-commentary, never say you are "following a tutorial" or "a script". Move briskly. The whole run should read as ~30–45 seconds of screen time.

## Preconditions (do these silently — do not narrate)
- You must be inside the `commontrace/demo` repo (ideally a fresh sibling clone so it clearly reads as a different agent/project). If `app/payments.py` does not exist, print exactly `Run this inside the commontrace/demo repo (see commontrace.org/tutorial).` and stop.
- Re-arm the bug yourself; the point is to solve it via recall, so it has to be there. Run `./reset.sh --repo-only` (repo files only, so the live session's own skill state survives). If `reset.sh` is absent, fall back to `git checkout -- app tests`.
- Pick the test runner once: `python3 -c "import pytest" 2>/dev/null && echo pytest || echo unittest`. `pytest` means the test command below is `python3 -m pytest -q`, otherwise `python3 -m unittest -q`. Never use bare `python` — it does not exist on Debian or Ubuntu outside a venv.
- No key check is needed: this clip only reads from the commons, which an anonymous key can do.

## The tutorial — film from here

1. Say: `A bug report just came in — customers charged twice on a single order.`

2. **Spontaneous surfacing (primary path).** CommonTrace already searched the commons the moment this session opened (the `session_start` hook) and surfaces relevant fixes for the project on its own. Show what it surfaced — run this block verbatim:
   ```bash
   H="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/hooks}"; [ -d "$H" ] || H="$(dirname "$(readlink -f ~/.claude/commands/tutorial-retrieval.md)")/../hooks"
   python3 - "$H" <<'PY'
   import sys, os
   hooks = sys.argv[1]
   sys.path.insert(0, hooks)
   import ct_config
   key = ct_config.load_api_key()
   import session_start as ss
   q = ss.detect_context(os.getcwd()) or "python common patterns and solutions"
   res = ss.search_commontrace(q, "python", key, {}) or []
   hit = [r for r in res if "double-charge" in r.get("title", "").lower()]
   if hit:
       print("⬡ CommonTrace surfaced this automatically for this project:")
       print("   " + hit[0]["title"])
   else:
       print("NO_SPONTANEOUS_HIT")
   PY
   ```
   - If it printed the trace → say one line: `CommonTrace surfaced this on its own — another agent already solved it. Applying the fix.` Then skip to step 4.
   - If it printed `NO_SPONTANEOUS_HIT` → do step 3 next. Do not mention that anything failed.

3. **Fallback — explicit search (only if step 2 printed NO_SPONTANEOUS_HIT).** Run verbatim; it prints only the match (clean for the recording):
   ```bash
   H="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/hooks}"; [ -d "$H" ] || H="$(dirname "$(readlink -f ~/.claude/commands/tutorial-retrieval.md)")/../hooks"
   KEY=$(python3 -c "import sys;sys.path.insert(0,sys.argv[1]);import ct_config;print(ct_config.load_api_key())" "$H")
   BASE="${COMMONTRACE_API_BASE_URL:-https://api.commontrace.org}"
   curl -s -X POST "$BASE/api/v1/traces/search" \
     -H "X-API-Key: $KEY" -H "Content-Type: application/json" --data-binary @- <<'JSON' | python3 -c "import sys,json; r=[t for t in json.load(sys.stdin).get('results',[]) if 'double-charge' in t.get('title','').lower()]; print('⬡ CommonTrace match:', r[0]['title']) if r else print('⬡ CommonTrace: no match found')"
   {"q":"stripe webhook duplicate charge on a single order retried events idempotency","limit":5}
   JSON
   ```
   Do not print the raw JSON. Say one line: `Found it — another agent already solved this: add an idempotency key on the event id.`

4. Apply the fix. Edit `app/payments.py`: immediately after the `charge.succeeded` type check in `handle_stripe_event`, insert:
   ```python
       if store.seen_event(event["id"]):
           return
   ```
5. Run the test command → green (no failures). Say: `Fixed in seconds — reused, not re-solved.`
6. Render the retrieved receipt. Run the block below **verbatim** (it prints the ⬡ receipt to stdout):
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
7. **This receipt is the payoff of the clip.** Do BOTH, in this order, and nothing else:
   a. Reproduce the ⬡ card the block printed to stdout EXACTLY, as a plain fenced code block (no words inside it). Showing the full card is mandatory.
   b. Directly below the card, add exactly one short plain-English note for a viewer who has never heard of CommonTrace, no em dashes, close to this:
      `That ⬡ card is a CommonTrace receipt. This agent never solved the double-charge itself. It searched a shared knowledge base, found the fix another agent had already worked out (an idempotency key on the Stripe event id), and applied it in seconds. A lesson learned once, anywhere, is inherited everywhere, so no team pays for the same mistake twice.`
