---
description: Search CommonTrace knowledge base for coding traces
argument-hint: [query or error text]
allowed-tools: ["mcp__commontrace__search_traces"]
---

Search CommonTrace for traces matching: "$ARGUMENTS"

Determine whether "$ARGUMENTS" is an error message or stack trace. Signals:
- Contains "Traceback", "Error:", "Exception:", or a line like `module.ClassName: some message`
- Contains `sqlalchemy.exc.`, `asyncio.exceptions.`, `FileNotFoundError`, `npm ERR!`, etc.

If it is an error:
1. Extract the exception class and first ~6 words of its message.
2. Build a lowercase dotted canonical signature, e.g. `sqlalchemy.exc.missinggreenlet.greenlet.spawn.has.not.been.called`.
3. Call `mcp__commontrace__search_traces` with `error_signature: "<the signature>"` and `query: ""` (so the server uses canonical short-circuit).

Otherwise, call `mcp__commontrace__search_traces` with:
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
