# Contributing to the CommonTrace Skill

Thanks for your interest in contributing! This repo is the Claude Code plugin for [CommonTrace](https://commontrace.org).

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating, you uphold it. Report issues to conduct@commontrace.org.

## Two kinds of contribution

CommonTrace accepts two distinct kinds of contribution, gated separately:

1. **Code** (this repository, via GitHub) — open to everyone. Fork, branch, open a pull request. Merging is at maintainer discretion after CI and review.
2. **Knowledge traces** (submitted to the live API by AI agents using this skill) — invitation-gated: access is earned, vouched, or founding.

**Merging a code PR does not grant trace-write access.** The two systems are independent. Improving the skill's detection or hooks does not change your contributor standing on the live API.

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR_USERNAME/skill.git`
3. Create a branch: `git checkout -b my-feature`
4. Make your changes
5. Push and open a pull request

## Development Setup

The skill is a Claude Code plugin — no build step, no runtime dependencies. You need **Python 3.12+** (declared in `pyproject.toml`) and a recent Claude Code.

Repository layout:

```
.claude-plugin/plugin.json   plugin manifest (name, version, author)
skills/commontrace/SKILL.md  skill guidance — when/how Claude uses the knowledge base
commands/trace/              slash commands (/trace:search, :contribute, :brain)
hooks/                       the structural detection pipeline (Python)
hooks/hooks.json             hook wiring (which hook fires on which event)
tests/                       unittest suite for the hooks
docs/                        how-it-works reference
```

Install your local checkout as a plugin to test it end-to-end:

```bash
claude plugin add /path/to/your/skill
```

The hooks read and write `~/.commontrace/` (config, local SQLite store, artifacts). Point the skill at a local stack with `COMMONTRACE_API_BASE_URL` and `COMMONTRACE_MCP_URL` if you don't want to hit production while developing.

### Running tests

The hook suite is `unittest`-based and fully offline — every test isolates `~/.commontrace` into a temp dir and blocks network access, so it never touches your real local.db or the live API. Run it exactly as CI does:

```bash
PYTHONPATH=tests:hooks python3 -m unittest discover -s tests
```

Run a single module while iterating:

```bash
PYTHONPATH=tests:hooks python3 -m unittest tests.test_error_recurrence
```

New hook behavior should come with a test. Extend `tests/base.py` (`HookTestCase`) so your test inherits the temp-dir + offline guarantees.

## Hooks

The skill's intelligence lives in structural hooks (session_start, user_prompt, post_tool_use, stop). Key constraints:

- **No LLM API calls.** Detection is structural only — tool-use sequences, file changes, error patterns, timing. No NLU on user messages, no classification calls.
- Syntax-check every hook before committing: `python3 -c "import py_compile; py_compile.compile('hooks/FILE.py', doraise=True)"`

## Pull Requests

- Keep PRs focused on a single change
- Include tests or a manual repro for detection changes
- Update SKILL.md / README.md if behavior changed

## Security

Found a vulnerability? Do not open a public issue — see [SECURITY.md](SECURITY.md) and email security@commontrace.org.

## License

By contributing, you agree your contributions are licensed under the Apache-2.0 license. Inbound = outbound; no CLA required.
