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
PENDING_DIR = CONFIG_DIR / "pending"
PING_MARKER = CONFIG_DIR / "last_ping_date"
API_BASE = "https://api.commontrace.org"
MCP_URL = "https://mcp.commontrace.org/mcp"
SKILL_VERSION = "0.3.0"

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
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")
    # H7: Restrict file permissions — owner read/write only
    os.chmod(CONFIG_FILE, 0o600)


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


def _post_json(path: str, payload: dict, api_key: str, timeout: float = 3.0) -> bool:
    """POST JSON to API with X-API-Key. Returns True on 2xx, False otherwise.
    Always silent — telemetry must never affect the user-facing session."""
    base_url = os.environ.get("COMMONTRACE_API_BASE_URL", API_BASE).rstrip("/")
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def report_install(api_key: str) -> None:
    """One-shot install beacon — fires once per install after key provisioning."""
    payload = {
        "platform": "Claude Code",
        "skill_version": SKILL_VERSION,
        "install_source": "plugin",
    }
    _post_json("/api/v1/telemetry/install", payload, api_key)


def maybe_ping(api_key: str) -> None:
    """Daily heartbeat — fires once per UTC day per install (local rate-limit)."""
    import datetime as _dt
    today = _dt.datetime.utcnow().date().isoformat()
    try:
        if PING_MARKER.exists():
            last = PING_MARKER.read_text(encoding="utf-8").strip()
            if last == today:
                return
    except OSError:
        pass
    if _post_json("/api/v1/telemetry/ping", {}, api_key, timeout=2.0):
        try:
            PING_MARKER.write_text(today, encoding="utf-8")
        except OSError:
            pass


def configure_mcp(api_key: str) -> bool:
    """Run `claude mcp add` to register the MCP server with the API key.

    H8: API key passed via environment variable, not CLI argument,
    to avoid exposure in process listing (ps aux / /proc/pid/cmdline).
    """
    try:
        env = os.environ.copy()
        env["COMMONTRACE_API_KEY"] = api_key
        result = subprocess.run(
            [
                "claude", "mcp", "add", "commontrace",
                "--transport", "http",
                MCP_URL,
                "-H", "x-api-key: ${COMMONTRACE_API_KEY}",
                "-s", "user",
            ],
            capture_output=True, text=True, timeout=10, env=env,
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

    # M21: Check if user has explicitly opted in before auto-provisioning
    if not config.get("consent_given"):
        # First run — don't auto-provision without consent.
        # User must set COMMONTRACE_API_KEY env var or run setup manually.
        return None

    # Provision with consent
    api_key = provision_api_key()
    if not api_key:
        return None

    config["api_key"] = api_key
    save_config(config)

    # Configure MCP server for future sessions
    configure_mcp(api_key)

    # Fire-and-forget install beacon (silent on failure)
    try:
        report_install(api_key)
    except Exception:
        pass

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


def search_commontrace(query: str, language: str, api_key: str,
                       context: dict | None = None) -> list[dict]:
    base_url = os.environ.get("COMMONTRACE_API_BASE_URL", API_BASE).rstrip("/")
    body: dict = {"q": query, "tags": [language], "limit": 3}
    if context:
        body["context"] = context
    payload = json.dumps(body).encode("utf-8")

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


def count_pending_traces() -> int:
    """Count pending contribution candidates across all sessions.

    Each line in ~/.commontrace/pending/*.jsonl is one candidate. Used in
    manual mode to nudge the user about /trace contribute.
    """
    if not PENDING_DIR.exists():
        return 0
    total = 0
    try:
        for path in PENDING_DIR.glob("*.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        total += 1
            except OSError:
                continue
    except OSError:
        return 0
    return total


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

    # Step 1b: Daily DAU heartbeat (silent, rate-limited to 1/day locally)
    try:
        maybe_ping(api_key)
    except Exception:
        pass

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

    # Step 2b: Persistent local store — register project + build context
    context_dict = None
    session_id = data.get("session_id") or str(os.getppid())
    contribution_recall = ""
    try:
        from local_store import (
            _get_conn, ensure_project, start_session, get_project_context,
            get_cached_traces, get_trigger_effectiveness,
        )
        conn = _get_conn()
        # Detect framework for the project record
        framework = None
        if query:
            for fw in ("fastapi", "django", "flask", "react", "next", "express", "vue"):
                if fw in query:
                    framework = fw
                    break
        project_id = ensure_project(conn, cwd, language, framework)
        start_session(conn, session_id, project_id)
        context_dict = get_project_context(conn, cwd)

        # Contribution recall — surface previously useful traces
        cached = get_cached_traces(conn, project_id, limit=3)
        if cached:
            titles = [t["title"][:60] for t in cached]
            contribution_recall = (
                f"Previously useful traces: {'; '.join(titles)}. "
            )

        # Write bridge files for Layer 1 hooks
        from session_state import get_state_dir
        state_dir = get_state_dir(data)
        try:
            (state_dir / "project_id").write_text(str(project_id), encoding="utf-8")
            if context_dict:
                (state_dir / "context_fingerprint.json").write_text(
                    json.dumps(context_dict), encoding="utf-8")
            # Write trigger stats bridge file for adaptive cooldowns
            trigger_stats = get_trigger_effectiveness(conn, project_id)
            if trigger_stats:
                (state_dir / "trigger_stats.json").write_text(
                    json.dumps(trigger_stats), encoding="utf-8")
        except OSError:
            pass

        conn.close()
    except Exception:
        context_dict = None

    # Step 3: Search CommonTrace (with context if available)
    results = search_commontrace(query, language, api_key, context_dict)

    if results:
        formatted = [f"{i + 1}. {format_result(r)}" for i, r in enumerate(results)]
        context_lines = "\n".join(formatted)
        additional_context = (
            f"{contribution_recall}"
            f"CommonTrace found relevant knowledge for this project:\n\n"
            f"{context_lines}\n\n"
            f"IMPORTANT: Before solving coding problems, search CommonTrace with "
            f"search_traces for existing solutions. After solving, contribute with "
            f"contribute_trace."
        )
    else:
        additional_context = (
            f"{contribution_recall}"
            "CommonTrace knowledge base is connected. "
            "IMPORTANT: Before solving coding problems, fixing bugs, or debugging errors, "
            "search CommonTrace with search_traces for existing solutions. "
            "After solving a non-trivial problem, contribute with contribute_trace."
        )

    # Pending traces hint (manual mode only — auto mode submits live).
    config = load_config()
    if not config.get("auto_contribute", True):
        pending_n = count_pending_traces()
        if pending_n > 0:
            additional_context += (
                f"\n\n{pending_n} pending CommonTrace contribution(s) await user "
                f"review. The user can run /trace contribute when they want to "
                f"review them. Do not proactively prompt — only mention if the "
                f"user asks about CommonTrace."
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
