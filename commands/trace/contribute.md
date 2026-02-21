---
description: Contribute a trace to CommonTrace knowledge base
allowed-tools: ["mcp__plugin_commontrace_commontrace__contribute_trace", "mcp__plugin_commontrace_commontrace__list_tags"]
---

Guide the user through contributing a trace to CommonTrace.

Follow these steps in order:

Step 1: Ask what problem was solved. This becomes the context_text.
Step 2: Ask what the solution was. This becomes the solution_text.
Step 3: Ask for a short descriptive title (under 100 characters).
Step 4: Use mcp__plugin_commontrace_commontrace__list_tags to show available tags. Ask the user to select applicable tags or suggest new ones.
Step 5: Show a complete preview of the trace:
  - Title
  - Context
  - Solution
  - Tags

  Ask for explicit confirmation: "Submit this trace to CommonTrace? (yes/no)"

Step 6: ONLY after the user confirms with "yes", use mcp__plugin_commontrace_commontrace__contribute_trace to submit with the title, context_text, solution_text, and tags.
Step 7: Report the trace ID from the result.

CRITICAL: Never submit a trace without explicit user confirmation. Always show the preview first.
If the user says "no" or wants changes, loop back to the relevant step.
If CommonTrace is unavailable, inform the user and suggest trying again later.
