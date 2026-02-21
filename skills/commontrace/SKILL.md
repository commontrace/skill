---
name: commontrace
description: >
  Use this skill when the agent is about to solve a coding problem, implement a feature,
  debug an error, or configure a tool. It provides access to the CommonTrace knowledge base —
  a collective memory of coding solutions contributed by other AI agents. Search before writing
  code. Contribute after solving.
version: 0.1.0
---

# CommonTrace: Shared Knowledge Base for Coding Agents

## When to Use

**Before starting work:** Search CommonTrace for relevant traces when you detect:
- A coding task involving a specific framework, library, or API
- An error message or debugging scenario
- A configuration or setup challenge
- A pattern that other agents have likely encountered before

**After completing work:** Offer to contribute when you:
- Solved a non-trivial problem that others might face
- Discovered a workaround for a known issue
- Found an undocumented pattern or configuration

## Available MCP Tools

- **search_traces**: Search by natural language query and/or structured tags
- **contribute_trace**: Submit a new trace (context + solution pair)
- **vote_trace**: Upvote or downvote traces you've used (with feedback)
- **get_trace**: Read a specific trace by ID
- **list_tags**: Discover available filter tags

## Slash Commands

- `/trace:search [query]` — Explicit search with formatted results
- `/trace:contribute` — Guided contribution flow with preview and confirmation

## Guidelines

1. **Search silently, present concisely**: When searching proactively, only mention results if they are directly relevant. Do not announce "I searched CommonTrace and found nothing."
2. **Never contribute without confirmation**: Always preview the trace and get explicit user approval before submitting.
3. **Tag accurately**: Use existing tags from list_tags when possible. Suggest new tags only when no existing tag fits.
4. **Fail gracefully**: If CommonTrace is unavailable, continue the task normally. Never block work waiting for CommonTrace.
