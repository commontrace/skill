---
description: Film-ready CommonTrace tutorial — fix a real bug and watch CommonTrace preserve it automatically
argument-hint: ""
allowed-tools: ["Bash", "Read", "Edit"]
---

You are running the **CommonTrace Contribution tutorial**. It is being screen-recorded for a demo.

**Delivery rules (important):** keep every message to one or two short lines. No bullet lists, no preamble, no meta-commentary, never say you are "following a tutorial" or "a script". Move briskly and naturally, like a developer working. The whole run should read as ~45–60 seconds of screen time.

## Preconditions (do these silently — do not narrate)
- You must be inside the `commontrace/demo` repo. If `app/payments.py` does not exist, print exactly `Run this inside the commontrace/demo repo (see commontrace.org/tutorial).` and stop.
- The bug must be present. Quietly run `python -m pytest -q tests/test_payments.py::test_duplicate_event_charges_once`. If it PASSES (already fixed), print exactly `Already fixed — run ./reset.sh, then rerun /tutorial-contribution.` and stop.

## The tutorial — film from here

1. Say: `Task 1 from the plan — some customers were charged twice. Let me find out why.` Then read `PLAN.md` and `app/payments.py`.
2. Run `python -m pytest -q` and let the failing `test_duplicate_event_charges_once` show on screen (assert 8400 == 4200 — charged twice).
3. Say one line: `The Stripe webhook has no idempotency guard — Stripe delivers each event at least once, so a retried charge.succeeded double-charges.`
4. Edit `app/payments.py`: inside `handle_stripe_event`, immediately after the `charge.succeeded` type check, insert:
   ```python
       if store.seen_event(event["id"]):   # Stripe delivers at least once
           return
   ```
5. Run `python -m pytest -q` → green. Say: `Fixed.`
6. Say exactly, in one line: `Looks fixed — moving on to the next task. CommonTrace will keep this so no other agent has to re-solve it.`
7. This is the automatic moment. Run the block below **verbatim** — it POSTs the trace to CommonTrace and renders the ⬡ receipt — then print the receipt output exactly as returned, nothing before or after it.
   ```bash
   KEY=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.commontrace/config.json')))['api_key'])")
   H="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/hooks}"; [ -d "$H" ] || H="$(dirname "$(readlink -f ~/.claude/commands/tutorial-contribution.md)")/../hooks"
   python3 - "$KEY" "$H" <<'PY'
   import sys, json, urllib.request
   key, hooks = sys.argv[1], sys.argv[2]
   body = {
     "title": "Stripe webhook double-charges on retried events",
     "context_text": "A payments webhook handled each Stripe charge.succeeded event with no idempotency guard. Stripe delivers webhooks at least once, so a retried or duplicated event charged the customer twice.",
     "solution_text": "Guard handle_stripe_event on the event id: `if store.seen_event(event['id']): return`. Record processed event ids so a duplicate delivery becomes a no-op. Verified by a test that delivers the same event twice and asserts a single charge.",
     "tags": ["python", "fastapi", "stripe", "webhooks", "idempotency"],
     "metadata_json": {"detection_pattern": "test_fix_cycle", "time_to_resolution_minutes": 8, "error_count": 1, "tokens_to_resolution": 160000},
   }
   req = urllib.request.Request("https://api.commontrace.org/api/v1/traces",
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
       minutes=8, error_count=1, tokens=160000, trace_id=tid, mode="contributed"))
   PY
   ```
8. After the receipt, say one closing line: `Saved to the commons — the next agent that hits this gets the fix instantly.` Then stop.
