---
description: Search CommonTrace knowledge base for coding traces
argument-hint: [query]
allowed-tools: ["mcp__plugin_commontrace_commontrace__search_traces"]
---

Search CommonTrace for traces matching: "$ARGUMENTS"

Use mcp__plugin_commontrace_commontrace__search_traces with:
- query: "$ARGUMENTS"
- limit: 5

Present each result clearly with:
- Title
- Context summary (2 sentences max)
- Solution summary (2 sentences max)
- Tags
- Trace ID (for reference)

If no results are found, say so clearly.
If CommonTrace is unavailable, say so and continue normally — do not retry or block.
