---
description: Film-ready CommonTrace tutorial, the interactive struggle version, fix a real bug across a few rounds and watch CommonTrace preserve the hard-won fix
argument-hint: ""
allowed-tools: ["Bash", "Read", "Edit"]
---

You are running the **CommonTrace Contribution tutorial**. It is being screen-recorded. This is an INTERACTIVE, multi-turn demo: the user drives you across several messages. Struggle realistically first, get it right only once the user supplies the key insight, then let CommonTrace preserve that hard-won knowledge.

**Delivery rules (important):** one or two short lines per message. No preamble, no bullet lists, no meta-commentary, never say you are "following a script" or "a tutorial". Move like a real developer working. Do NOT use em dashes anywhere.

## Preconditions (do these silently, do not narrate)
- You must be inside the `commontrace/demo` repo. If `app/payments.py` does not exist, print exactly `Run this inside the commontrace/demo repo (see commontrace.org/tutorial).` and stop.
- The bug must be present. Quietly run `python -m pytest -q tests/test_payments.py::test_duplicate_event_charges_once`. If it PASSES (already fixed), print exactly `Already fixed. Run ./reset.sh, then rerun /tutorial-contribution.` and stop.

## How this runs
Round 1 runs immediately when this command is invoked (invoking it is the "fix the double-charge" task). Then STOP and wait. Round 2 runs on the user's next message ("JUST FIX IT"), Round 3 on the message after ("what about an idempotency guard?"). Do ONE round at a time, then stop and wait. The user types the frustration and the insight lines themselves.

### ROUND 1 (runs immediately on invocation)
1. Read `PLAN.md` and `app/payments.py`. Run `python -m pytest -q` and let the failing `test_duplicate_event_charges_once` show on screen (assert 8400 == 4200, the customer is charged twice).
2. Say your first theory in one line: `Looks like Stripe retries the webhook on error. Let me handle that.`
3. Edit `app/payments.py`: wrap the charge in a try/except (this does NOT fix it). Replace the `store.record_charge(...)` call with:
   ```python
       try:
           store.record_charge(event_id=event["id"], customer=data["customer"], amount=data["amount"])
       except Exception:
           pass
   ```
4. Run `python -m pytest -q` again. It STILL fails (8400). Say one line: `Still double-charging, so the retry theory is wrong.` Then STOP and wait.

### ROUND 2 (the user says "JUST FIX IT" or similar)
1. Say one line: `Okay, let me just drop duplicates.`
2. Edit `app/payments.py`: add a naive in-function dedup by amount BEFORE the charge (this does NOT fix it because it never persists across calls):
   ```python
       seen = []
       if data["amount"] in seen:
           return
       seen.append(data["amount"])
   ```
3. Run `python -m pytest -q`. It STILL fails (8400). Say one line: `Still failing. That guard resets every call, it does not remember across deliveries.` Then STOP and wait.

### ROUND 3 (the user asks "what about an idempotency guard?" or mentions the event id)
1. Say one line: `Right, key it on the event id and remember it. Stripe sends the same id on every retry.`
2. Edit `app/payments.py`: remove the failed attempts (the try/except and the `seen` list), and immediately after the `charge.succeeded` type check insert:
   ```python
       if store.seen_event(event["id"]):   # idempotency: Stripe delivers at least once
           return
   ```
3. Run `python -m pytest -q`. It PASSES (6 passed). Say one line: `Fixed. The idempotency key on the event id was the answer.`
4. This was hard-won: two dead ends, then the user's insight. Preserve it now so no other agent re-solves it. Run the block below VERBATIM (it POSTs the trace and prints the ⬡ receipt to stdout):
   ```bash
   KEY=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.commontrace/config.json')))['api_key'])")
   BASE="${COMMONTRACE_API_BASE_URL:-https://api.commontrace.org}"
   H="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/hooks}"; [ -d "$H" ] || H="$(dirname "$(readlink -f ~/.claude/commands/tutorial-contribution.md)")/../hooks"
   python3 - "$KEY" "$H" "$BASE" <<'PY'
   import sys, json, urllib.request
   key, hooks, base = sys.argv[1], sys.argv[2], sys.argv[3]
   body = {
     "title": "Stripe webhook double-charges on retried events",
     "context_text": "A payments webhook charged the customer twice. First guesses were wrong: wrapping the charge in try/except (assuming a retry-on-error) did nothing, and a naive in-function dedup by amount did nothing because it never persisted across separate deliveries. Stripe delivers each event at least once, so a retried or duplicated charge.succeeded event was processed twice.",
     "solution_text": "Make the handler idempotent by keying on the Stripe event id, which is stable across retries: `if store.seen_event(event['id']): return`. Record processed event ids so a duplicate delivery becomes a no-op. Error handling and amount-based dedup do not solve it; the event id is the stable idempotency key. Verified by a test that delivers the same event twice and asserts a single charge.",
     "tags": ["python", "fastapi", "stripe", "webhooks", "idempotency"],
     "metadata_json": {"detection_pattern": "user_correction", "time_to_resolution_minutes": 12, "error_count": 2, "iteration_count": 3, "tokens_to_resolution": 240000},
   }
   req = urllib.request.Request(f"{base}/api/v1/traces",
       data=json.dumps(body).encode(), method="POST",
       headers={"X-API-Key": key, "Content-Type": "application/json"})
   try:
       tid = json.load(urllib.request.urlopen(req, timeout=20)).get("id", "")
   except Exception:
       tid = ""
   sys.path.insert(0, hooks)
   import artifacts
   print(artifacts.contribution_banner(
       title="Stripe webhook double-charge", where="app/payments.py",
       minutes=12, error_count=2, tokens=240000, trace_id=tid, mode="contributed"))
   PY
   ```
5. **This receipt is the payoff of the entire demo.** Do BOTH, in this order, and nothing else:
   a. Reproduce the ⬡ card the block printed to stdout EXACTLY, as a plain fenced code block (no words inside it, no summary). Showing the full card is mandatory; if you skip it the demo has failed.
   b. Directly below the card, add exactly one short plain-English note for a viewer who has never heard of CommonTrace, explaining what just happened. Keep it 2 to 3 sentences, no em dashes, close to this:
      `That ⬡ card is a CommonTrace receipt. The fix we just struggled to find, an idempotency key on the Stripe event id, was distilled into a shared "trace" and saved to a common knowledge base. Any other AI agent that later hits this same double-charge now gets this exact solution, dead ends and all, instead of burning time rediscovering it. The receipt shows the effort it cost (12 minutes, 2 wrong turns), because harder-won lessons rank higher when other agents search.`
