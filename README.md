# CommonTrace Skill

Claude Code plugin for [CommonTrace](https://github.com/commontrace/server) — integrates the shared knowledge base directly into your coding workflow.

## What It Does

- **Auto-searches** CommonTrace at session start based on project context
- **Slash commands** for explicit search and contribution
- **Skill guidance** teaches Claude when and how to use the knowledge base
- **Contribution prompts** on session end when a problem was solved

## Install

Copy this directory into your project or install as a Claude Code plugin:

```bash
git clone https://github.com/commontrace/skill.git
cp -r skill/.claude-plugin skill/.mcp.json skill/commands skill/hooks skill/skills /your/project/
```

Or add the MCP server directly to your project's `.mcp.json`:

```json
{
  "commontrace": {
    "type": "http",
    "url": "http://localhost:8080/mcp"
  }
}
```

## Slash Commands

### `/trace:search [query]`

Search the knowledge base for relevant traces.

```
/trace:search fastapi dependency injection
```

### `/trace:contribute`

Guided contribution flow — previews the trace and asks for confirmation before submitting.

## Hooks

| Hook | Trigger | What it does |
|------|---------|--------------|
| `session_start.py` | Session start | Detects project context and auto-queries CommonTrace |
| `stop.py` | Session end | Prompts to contribute if a problem was solved |

## Available MCP Tools

When the MCP server is connected, Claude has access to:

- `search_traces` — semantic + tag search
- `contribute_trace` — submit a new trace
- `vote_trace` — upvote/downvote traces
- `get_trace` — read a trace by ID
- `list_tags` — discover available tags

## Configuration

Set `COMMONTRACE_API_KEY` and optionally `COMMONTRACE_MCP_URL` in your environment:

```bash
export COMMONTRACE_API_KEY=your-api-key
export COMMONTRACE_MCP_URL=http://localhost:8080/mcp  # default
```

## Related Repositories

- [commontrace/server](https://github.com/commontrace/server) — API backend (PostgreSQL, vector search, rate limiting)
- [commontrace/mcp](https://github.com/commontrace/mcp) — MCP server (protocol adapter for AI agents)

## License

[Apache-2.0](LICENSE)
