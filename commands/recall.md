---
description: Ask CommonTrace for a solution to the problem you're facing now — retrieval twin of /trace
argument-hint: "[keywords about the problem, e.g. gandi http]"
allowed-tools: ["mcp__commontrace__search_traces", "mcp__commontrace__get_trace"]
---

Retrieval twin of `/trace`: force a CommonTrace lookup for a solution to the specific problem in THIS conversation. Where `/trace <keywords>` proposes a contribution from the discussion, `/recall <keywords>` pulls a solution back out.

## Flow

1. **Locate the problem.** Use the keywords `$ARGUMENTS` as a hint to the exact blocker being discussed right now — the error/symptom and its stack. If `$ARGUMENTS` is empty, target the most recent unresolved problem in the conversation. Do NOT invent a problem.
2. **Build a focused query** from that problem — symptom + language/framework + key terms — not just the raw keywords. A precise query retrieves far better than the bare args.
3. **Search** with `mcp__commontrace__search_traces` (`query` = your focused query, `limit` = 5).
4. **Present the top matches**, best first, each as:
   - **Title**
   - Context — 2 lines max
   - Solution — 2 lines max
   - Tags · Trace ID · similarity/score if returned
   Pull full detail for the top hit with `mcp__commontrace__get_trace` when the summary is thin.
5. **Recommend.** State which trace (if any) actually fits the problem and the concrete next step to apply it. If several fit, rank them.
6. **No match** → say so plainly and keep solving normally.

If CommonTrace is unavailable, say so and continue — do not retry or block.
