#!/usr/bin/env python3
"""
CommonTrace SessionStart hook.

First run: auto-provisions an anonymous API key (zero-decision onboarding),
stores it in ~/.commontrace/config.json, registers the MCP server, and
queues a one-time disclosure notice. If provisioning fails, a one-time
setup notice is emitted and provisioning retries silently on later sessions.
Every run: detects coding context, queries CommonTrace, injects relevant traces.

Never blocks session start — failures degrade to a short notice or silence.
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path


CONFIG_DIR = Path.home() / ".commontrace"
CONFIG_FILE = CONFIG_DIR / "config.json"
PENDING_DIR = CONFIG_DIR / "pending"
PING_MARKER = CONFIG_DIR / "last_ping_date"
API_BASE = "https://api.commontrace.org"
MCP_URL = "https://mcp.commontrace.org/mcp"
SKILL_VERSION = "0.5.0"

SETUP_FAILED_NOTICE = (
    "CommonTrace setup could not complete (API unreachable). The skill will "
    "retry automatically next session; local knowledge tracking works in the "
    "meantime. To configure manually, set the COMMONTRACE_API_KEY environment "
    "variable — see https://github.com/commontrace/skill#install. Mention "
    "this to the user only if they ask about CommonTrace."
)

FIRST_RUN_NOTICE = (
    "CommonTrace first-run notice — relay this to the user in one short "
    "paragraph at the start of your reply: CommonTrace is now connected. An "
    "anonymous account was created automatically (random ID, no personal "
    "data) and the API key is stored at ~/.commontrace/config.json. Sessions "
    "now search a shared knowledge base of coding fixes, and solved problems "
    "are auto-contributed back in anonymized, secret-redacted form (set "
    "auto_contribute to false in ~/.commontrace/config.json to review before "
    "anything is shared). To use a personal account: set the "
    "COMMONTRACE_API_KEY environment variable. To disconnect entirely: "
    "run 'claude plugin remove commontrace' and 'claude mcp remove "
    "commontrace', then delete ~/.commontrace. MCP "
    "tools (search_traces, contribute_trace) load from the next session "
    "onward."
)

FIRST_RUN_NOTICE_DEGRADED = (
    "CommonTrace first-run notice — relay this to the user in one short "
    "paragraph at the start of your reply: CommonTrace is connected but MCP "
    "tool registration did not complete automatically. The API key is stored "
    "at ~/.commontrace/config.json. To register the MCP server manually run: "
    "claude mcp add commontrace --transport http https://mcp.commontrace.org/mcp "
    "-H 'x-api-key: <your-key>' -s user  (replace <your-key> with the value "
    "in config.json). Do not include the actual key in your reply to the user."
)

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
    """Persist config to ~/.commontrace/config.json atomically (crash-safe).

    Uses tempfile.mkstemp in the same directory + os.replace so a crash
    mid-write cannot corrupt config.json. Permissions are set to 0o600
    before the rename so the file is never world-readable even briefly.
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_DIR.chmod(0o700)
        fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR)
        try:
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(config, fh, indent=2)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        os.replace(tmp_path, CONFIG_FILE)
    except OSError:
        return


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
    """Register the MCP server via `claude mcp add` (idempotent remove-then-add).

    The raw key is embedded in the stored MCP config deliberately. This
    function only runs right after auto-provisioning, when no
    COMMONTRACE_API_KEY env var exists for `${...}` expansion at MCP
    connect time — env-var indirection here would 401 on every MCP call.
    Accepted tradeoff (supersedes H8 for this call site): the key is an
    anonymous, low-value credential, and the argv exposure window is the
    few seconds `claude mcp add` runs. Manual installs that export the
    env var never reach this code path.

    The remove step is best-effort (ignore return code) so re-running
    after a partial failure always produces a clean registration.
    """
    try:
        # Best-effort remove first (idempotency — ignore all errors)
        try:
            subprocess.run(
                ["claude", "mcp", "remove", "commontrace", "-s", "user"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            pass  # Remove failure must not prevent add
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
    except Exception:
        return False


def ensure_setup() -> str | None:
    """Ensure API key exists and MCP is configured. Returns api_key or None.

    Zero-decision onboarding (spec 2026-06-10 §10 Phase 1): installing the
    plugin IS the opt-in, so the first run auto-provisions an anonymous
    account (random ID, no PII) and queues a one-time disclosure notice
    (pending_first_run_notice) that main() delivers and clears. A failed
    provisioning attempt stores nothing, so every later session start
    retries until it succeeds.

    Concurrency: a POSIX fcntl exclusive lock on the config dir prevents
    duplicate provisioning when two sessions start simultaneously. The
    loser re-reads config after acquiring the lock and, if a key was
    already written by the winner, returns that key without re-queuing
    any notices.

    MCP registration: configure_mcp() return value is stored in
    mcp_configured. On failure, FIRST_RUN_NOTICE_DEGRADED is queued
    instead of FIRST_RUN_NOTICE so the user gets actionable guidance.
    """
    config = load_config()

    # Check env var first (user override)
    api_key = os.environ.get("COMMONTRACE_API_KEY", "")
    if api_key:
        updated = False
        if not config.get("api_key"):
            config["api_key"] = api_key
            updated = True
        # Fix I3: If anonymous key was auto-provisioned and env var now set,
        # re-register MCP with env-var indirection so manual key takes effect.
        # Gated strictly on anonymous provenance — never touch MCP for manual installs.
        if config.get("anonymous") and not config.get("env_mcp_reconfigured"):
            configure_mcp("${COMMONTRACE_API_KEY}")
            config["env_mcp_reconfigured"] = True
            updated = True
        if updated:
            save_config(config)
        return api_key

    # Check stored config
    api_key = config.get("api_key", "")
    if api_key:
        # Retry MCP registration if it failed previously
        if config.get("mcp_configured") is False:
            mcp_ok = configure_mcp(api_key)
            if mcp_ok:
                fresh = load_config()
                fresh["mcp_configured"] = True
                fresh.pop("pending_first_run_notice", None)
                fresh["pending_first_run_notice"] = True
                save_config(fresh)
        return api_key

    # First run: auto-provision an anonymous account.
    # Use fcntl lock to prevent duplicate provisioning from concurrent sessions.
    try:
        import fcntl
        _lock_available = True
    except ImportError:
        _lock_available = False

    lock_fd = None
    try:
        if _lock_available:
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                lock_fd = open(CONFIG_DIR / ".provision_lock", "w")
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                # Another session is provisioning — wait for it, then re-read
                if lock_fd is not None:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    except OSError:
                        pass
                refreshed = load_config()
                winner_key = refreshed.get("api_key", "")
                if lock_fd is not None:
                    try:
                        lock_fd.close()
                    except OSError:
                        pass
                return winner_key or None

        # We hold the lock (or fcntl unavailable) — re-check under lock
        config = load_config()
        api_key = config.get("api_key", "")
        if api_key:
            return api_key

        api_key = provision_api_key()
        if not api_key:
            return None

        mcp_ok = configure_mcp(api_key)

        config["api_key"] = api_key
        config["anonymous"] = True
        config["mcp_configured"] = mcp_ok
        if mcp_ok:
            config["pending_first_run_notice"] = True
        else:
            config["pending_first_run_notice_degraded"] = True
        save_config(config)

        # Fire-and-forget install beacon (silent on failure)
        try:
            report_install(api_key)
        except Exception:
            pass

        return api_key

    finally:
        if lock_fd is not None:
            try:
                lock_fd.close()
            except OSError:
                pass


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


def _compiled_drop(config):
    """Monthly Compiled recap — fires once on the first session of each
    month, covering the previous month. The user's own numbers, generated
    locally; never an interpretation.

    The "last_compiled_month" marker is set even when the month was empty
    (one db query per month, then silence). Returns additionalContext
    text, or None.

    Note: directories that do not emit context (no .git, no source files)
    return before reaching this function — the marker is therefore only
    set when the session actually produces output, deferring the drop
    until the first context-emitting session of the month.
    """
    import time as _time
    t = _time.localtime()
    current = f"{t.tm_year}-{t.tm_mon:02d}"
    if config.get("last_compiled_month") == current:
        return None
    if t.tm_mon > 1:
        year, month = t.tm_year, t.tm_mon - 1
    else:
        year, month = t.tm_year - 1, 12
    text = None
    path = None
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from artifacts import compiled_recap, write_artifact
        from local_store import _get_conn
        conn = _get_conn()
        try:
            text = compiled_recap(conn, year, month)
        finally:
            conn.close()
        if text:
            path = write_artifact(f"compiled-{year}-{month:02d}.txt", text)
    except Exception:
        return None
    # Fix I1: Re-load config from disk before writing the marker to avoid
    # overwriting flags written by other hooks between session_start's
    # initial load and this point.
    fresh = load_config()
    fresh["last_compiled_month"] = current
    # Keep the caller's in-memory dict consistent with what was persisted.
    config["last_compiled_month"] = current
    try:
        save_config(fresh)
    except OSError:
        pass
    if not text:
        return None
    return (f"CommonTrace monthly Compiled recap is ready "
            f"(saved to {path}):\n\n{text}\n\n"
            f"Mention it to the user at a natural moment — it is their "
            f"own data, generated locally.")


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

def _emit_setup_notice() -> None:
    """One-time notice when provisioning failed — replaces the old silent exit.

    Shown once ever (setup_notice_shown flag); provisioning itself still
    retries silently on every later session start.
    """
    config = load_config()
    if config.get("setup_notice_shown"):
        return
    config["setup_notice_shown"] = True
    try:
        save_config(config)
    except OSError:
        pass
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": SETUP_FAILED_NOTICE,
        }
    }))

def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        data = {}

    # Step 1: Ensure API key + MCP configured (auto-provisions on first run)
    api_key = ensure_setup()
    if not api_key:
        _emit_setup_notice()
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
    session_id = data.get("session_id") or f"unknown-{uuid.uuid4().hex[:12]}"
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

    # First-run disclosure (M21 zero-decision transparency) — queued by
    # ensure_setup at provisioning time, delivered once in the first
    # session that actually emits context, then cleared.
    if config.get("pending_first_run_notice"):
        additional_context = f"{FIRST_RUN_NOTICE}\n\n{additional_context}"
        config.pop("pending_first_run_notice", None)
        try:
            save_config(config)
        except OSError:
            pass
    elif config.get("pending_first_run_notice_degraded"):
        additional_context = f"{FIRST_RUN_NOTICE_DEGRADED}\n\n{additional_context}"
        config.pop("pending_first_run_notice_degraded", None)
        try:
            save_config(config)
        except OSError:
            pass

    # Monthly Compiled drop (first session of a new month → previous
    # month's numbers). Local-only; must never block session start.
    try:
        recap_note = _compiled_drop(config)
        if recap_note:
            additional_context += f"\n\n{recap_note}"
    except Exception:
        pass

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
