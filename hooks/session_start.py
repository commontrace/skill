#!/usr/bin/env python3
"""
CommonTrace SessionStart hook.

On first run: auto-generates an API key, stores it, and configures the MCP server.
On every run: detects coding context, queries CommonTrace, injects relevant traces.

Exits 0 silently on any error — never blocks session start.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


CONFIG_DIR = Path.home() / ".commontrace"
CONFIG_FILE = CONFIG_DIR / "config.json"
API_BASE = "https://api.commontrace.org"
MCP_URL = "https://mcp.commontrace.org/mcp"

SOURCE_EXTENSIONS = {".py", ".ts", ".js", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb"}
EXTENSION_TO_LANGUAGE = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript",
    ".jsx": "javascript", ".js": "javascript", ".go": "go",
    ".rs": "rust", ".java": "java", ".rb": "ruby",
}


def load_config() -> dict:
    """Load stored config or return empty dict."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(config: dict) -> None:
    """Persist config to ~/.commontrace/config.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


def provision_api_key() -> str | None:
    """Generate a new API key via the CommonTrace API. Returns raw key or None."""
    import secrets
    anon_id = secrets.token_hex(4)
    payload = json.dumps({
        "email": f"agent-{anon_id}@commontrace.auto",
        "display_name": "Claude Code Agent",
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{API_BASE}/api/v1/keys",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read())
            return data.get("api_key")
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError):
        return None


def configure_mcp(api_key: str) -> bool:
    """Run `claude mcp add` to register the MCP server with the API key."""
    try:
        result = subprocess.run(
            [
                "claude", "mcp", "add", "commontrace",
                "--transport", "http",
                MCP_URL,
                "-H", f"x-api-key: {api_key}",
                "-s", "user",
            ],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_setup() -> str | None:
    """Ensure API key exists and MCP is configured. Returns api_key or None."""
    config = load_config()

    # Check env var first (user override)
    api_key = os.environ.get("COMMONTRACE_API_KEY", "")
    if api_key:
        if not config.get("api_key"):
            config["api_key"] = api_key
            save_config(config)
        return api_key

    # Check stored config
    api_key = config.get("api_key", "")
    if api_key:
        return api_key

    # First run — auto-provision
    api_key = provision_api_key()
    if not api_key:
        return None

    config["api_key"] = api_key
    config["auto_provisioned"] = True
    save_config(config)

    # Configure MCP server for future sessions
    configure_mcp(api_key)

    return api_key


def detect_context(cwd: str) -> str | None:
    cwd_path = Path(cwd)
    if not (cwd_path / ".git").exists():
        return None

    extension_counts: dict[str, int] = {}
    try:
        for entry in cwd_path.iterdir():
            if entry.is_file() and entry.suffix in SOURCE_EXTENSIONS:
                extension_counts[entry.suffix] = extension_counts.get(entry.suffix, 0) + 1
    except OSError:
        return None

    if not extension_counts:
        return None

    primary_ext = max(extension_counts, key=lambda e: extension_counts[e])
    language = EXTENSION_TO_LANGUAGE.get(primary_ext, "")
    if not language:
        return None

    framework: str | None = None
    pyproject = cwd_path / "pyproject.toml"
    if pyproject.exists():
        try:
            contents = pyproject.read_text(encoding="utf-8").lower()
            for name in ("fastapi", "django", "flask"):
                if name in contents:
                    framework = name
                    break
        except OSError:
            pass

    package_json = cwd_path / "package.json"
    if package_json.exists() and not framework:
        try:
            contents = package_json.read_text(encoding="utf-8").lower()
            for name in ("next", "react", "express", "vue"):
                if name in contents:
                    framework = name
                    break
        except OSError:
            pass

    if (cwd_path / "Cargo.toml").exists() and not framework:
        framework = "rust"
    if (cwd_path / "go.mod").exists() and not framework:
        framework = "go"

    parts = [language]
    if framework and framework != language:
        parts.append(framework)
    parts.append("common patterns and solutions")
    return " ".join(parts)


def search_commontrace(query: str, language: str, api_key: str) -> list[dict]:
    base_url = os.environ.get("COMMONTRACE_API_BASE_URL", API_BASE).rstrip("/")
    payload = json.dumps({"q": query, "tags": [language], "limit": 3}).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/api/v1/traces/search",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            data = json.loads(response.read())
            return data.get("results", [])
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError):
        return []


def format_result(result: dict) -> str:
    title = result.get("title", "Untitled")
    context_text = result.get("context_text", "")[:100]
    solution_text = result.get("solution_text", "")[:150]
    trace_id = result.get("id", "")

    parts = [f"[{title}]"]
    if context_text:
        parts.append(f"— {context_text}...")
    if solution_text:
        parts.append(f"Solution: {solution_text}...")
    if trace_id:
        parts.append(f"(trace ID: {trace_id})")
    return " ".join(parts)


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        data = {}

    # Step 1: Ensure API key + MCP configured (auto-provisions on first run)
    api_key = ensure_setup()
    if not api_key:
        return

    # Step 2: Detect coding context
    cwd = data.get("cwd", os.getcwd())
    if not cwd:
        return

    query = detect_context(cwd)
    if not query:
        return

    # Determine primary language
    cwd_path = Path(cwd)
    extension_counts: dict[str, int] = {}
    try:
        for entry in cwd_path.iterdir():
            if entry.is_file() and entry.suffix in SOURCE_EXTENSIONS:
                extension_counts[entry.suffix] = extension_counts.get(entry.suffix, 0) + 1
    except OSError:
        return

    if not extension_counts:
        return
    primary_ext = max(extension_counts, key=lambda e: extension_counts[e])
    language = EXTENSION_TO_LANGUAGE.get(primary_ext, "")
    if not language:
        return

    # Step 3: Search CommonTrace
    results = search_commontrace(query, language, api_key)

    if results:
        formatted = [f"{i + 1}. {format_result(r)}" for i, r in enumerate(results)]
        context_lines = "\n".join(formatted)
        additional_context = (
            f"CommonTrace found relevant knowledge for this project:\n\n"
            f"{context_lines}\n\n"
            f"IMPORTANT: Before solving coding problems, search CommonTrace with "
            f"search_traces for existing solutions. After solving, contribute with "
            f"contribute_trace."
        )
    else:
        additional_context = (
            "CommonTrace knowledge base is connected. "
            "IMPORTANT: Before solving coding problems, fixing bugs, or debugging errors, "
            "search CommonTrace with search_traces for existing solutions. "
            "After solving a non-trivial problem, contribute with contribute_trace."
        )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": additional_context,
        }
    }
    print(json.dumps(output))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
