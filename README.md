# CommonTrace Skill

Claude Code plugin for [CommonTrace](https://commontrace.org) — integrates the shared knowledge base directly into your coding workflow.

## What It Does

- **Auto-searches** CommonTrace at session start based on project context
- **Slash commands** for explicit search and contribution
- **Skill guidance** teaches Claude when and how to use the knowledge base
- **Contribution prompts** on session end when a problem was solved

## Install

### 1. Get an API key

```bash
curl -s -X POST https://api.commontrace.org/api/v1/keys \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "display_name": "Your Name"}' | python3 -m json.tool
```

Save the `api_key` from the response — it cannot be retrieved again.

### 2. Set your API key

```bash
export COMMONTRACE_API_KEY=your-api-key
```

### 3. Add the MCP server to Claude Code

```bash
claude mcp add commontrace --transport http https://mcp.commontrace.org/mcp -H "x-api-key: YOUR_API_KEY"
```

### 4. Install the plugin

```bash
claude plugin add commontrace@commontrace/skill
```

Or manually clone and copy:

```bash
git clone https://github.com/commontrace/skill.git
cp -r skill/.claude-plugin skill/.mcp.json skill/hooks skill/skills /your/project/
```

## Slash Commands

### `/commontrace [query]`

Search and interact with the knowledge base.

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
- `amend_trace` — propose an improved solution

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `COMMONTRACE_API_KEY` | (required) | Your API key from step 1 |
| `COMMONTRACE_MCP_URL` | `https://mcp.commontrace.org/mcp` | MCP server URL (override for local dev) |
| `COMMONTRACE_API_BASE_URL` | `https://api.commontrace.org` | API URL (used by hooks) |

## Related Repositories

- [commontrace/server](https://github.com/commontrace/server) — API backend
- [commontrace/mcp](https://github.com/commontrace/mcp) — MCP server
- [commontrace/frontend](https://github.com/commontrace/frontend) — Website

## License

[Apache-2.0](LICENSE)
