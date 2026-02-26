#!/usr/bin/env python3
"""
CommonTrace PostToolUse hook — Layer 1 state writer + knowledge detection.

Two responsibilities:
1. Record structural signals (errors, changes, research, contributions)
2. Detect knowledge crystallization moments in real-time

Knowledge crystallization = state transitions where "not knowing" becomes
"knowing". Detected structurally from tool-use sequences:

  Search→Implement:    research events then code changes (no errors)
  Fail→Succeed:        bash error then changes then bash success
  Iterate→Converge:    same file edited N times then different files
  Approach Reversal:   Write to a file previously Edit-ed 3+ times
  Cross-file Breadth:  changes spanning 3+ directories

Each detected transition writes a "knowledge candidate" to
candidates.jsonl with context for the stop hook to score.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from session_state import (
    get_state_dir, append_event, read_events, is_config_file,
)


CONFIG_FILE = Path.home() / ".commontrace" / "config.json"
API_BASE = "https://api.commontrace.org"
COOLDOWN_DIR = Path("/tmp/commontrace-cooldowns")

EXTENSION_TO_LANGUAGE = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript",
    ".jsx": "javascript", ".js": "javascript", ".go": "go",
    ".rs": "rust", ".java": "java", ".rb": "ruby",
}

# Package manager commands for dependency_resolution detection
PACKAGE_COMMANDS = {
    "pip", "pip3", "npm", "yarn", "pnpm", "cargo", "go mod",
    "bundle", "composer", "poetry", "pdm", "uv",
}

# Test commands for test_fix_cycle detection
TEST_COMMANDS = {
    "pytest", "jest", "mocha", "vitest", "cargo test", "go test",
    "npm test", "yarn test", "rspec", "phpunit", "unittest",
    "npm run test", "yarn run test",
}

# Security-related file name fragments
SECURITY_FILE_PATTERNS = {
    "auth", "security", "cors", "csp", "middleware",
    "permission", "sanitiz", "validat", "secret", "crypt",
}

# Security audit tools
SECURITY_COMMANDS = {
    "bandit", "npm audit", "snyk", "cargo audit", "safety check",
    "trivy", "semgrep",
}

# Infrastructure file path patterns
INFRA_PATTERNS = {
    "dockerfile", "docker-compose", ".github/workflows",
    "terraform", "nginx", "procfile", "railway",
    "vercel.json", "netlify.toml", "fly.toml",
    ".gitlab-ci", "jenkinsfile", "cloudbuild",
    "k8s", "kubernetes", "helm",
}


def _is_security_file(file_path: str) -> bool:
    name = Path(file_path).name.lower()
    return any(p in name for p in SECURITY_FILE_PATTERNS)


def _is_infra_file(file_path: str) -> bool:
    path_lower = file_path.lower()
    return any(p in path_lower for p in INFRA_PATTERNS)


def load_api_key() -> str:
    try:
        if CONFIG_FILE.exists():
            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return config.get("api_key", "")
    except (json.JSONDecodeError, OSError):
        pass
    return os.environ.get("COMMONTRACE_API_KEY", "")


def is_on_cooldown(trigger_name: str, seconds: int) -> bool:
    """Per-trigger cooldown check."""
    path = COOLDOWN_DIR / f"{trigger_name}.ts"
    try:
        if path.exists():
            last = float(path.read_text(encoding="utf-8"))
            if time.time() - last < seconds:
                return True
    except (ValueError, OSError):
        pass
    return False


def set_cooldown(trigger_name: str) -> None:
    """Set cooldown timestamp for a trigger."""
    COOLDOWN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (COOLDOWN_DIR / f"{trigger_name}.ts").write_text(
            str(time.time()), encoding="utf-8")
    except OSError:
        pass


def search_commontrace(query: str, api_key: str,
                       context: dict | None = None) -> list[dict]:
    import urllib.error
    import urllib.request

    base_url = os.environ.get("COMMONTRACE_API_BASE_URL", API_BASE).rstrip("/")
    body: dict = {"q": query, "limit": 3}
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
    except (urllib.error.URLError, urllib.error.HTTPError,
            json.JSONDecodeError, OSError):
        return []


def format_results(results: list[dict]) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        solution = r.get("solution_text", "")[:200]
        trace_id = r.get("id", "")
        lines.append(f"{i}. [{title}] — {solution}... (ID: {trace_id})")
    return "\n".join(lines)


def detect_bash_error(data: dict) -> tuple[bool, str, str]:
    """Detect if a Bash command failed using structural signals only.

    Checks (in order):
    1. Exit code field in tool_response (most reliable)
    2. Presence of stderr content (structural — stderr is for errors)
    3. If tool_response is a plain string, we cannot structurally
       determine error vs success — default to not-error.

    Returns: (is_error, output_text, error_text_for_search)
    """
    tool_response = data.get("tool_response", {})

    if isinstance(tool_response, dict):
        output = tool_response.get("output", "")
        stderr = tool_response.get("stderr", "")
        exit_code = tool_response.get("exitCode",
                    tool_response.get("exit_code"))

        # Non-zero exit code is the clearest structural signal
        if exit_code is not None and exit_code != 0:
            # Use stderr if available, otherwise tail of output
            error_text = stderr if stderr else output[-500:]
            return True, output, error_text

        # Stderr with content = error (by Unix convention)
        if stderr and stderr.strip():
            return True, output, stderr[-500:]

        return False, output, ""

    if isinstance(tool_response, str):
        # Plain string — can't structurally determine error.
        # But Claude Code often includes exit code info in the string.
        # Check for non-zero exit code at the end (this is structural
        # metadata appended by Claude Code, not error message parsing).
        output = tool_response
        # Claude Code appends "exit code: N" or similar
        exit_match = re.search(r'exit\s*code[:\s]+(\d+)', output[-100:],
                               re.IGNORECASE)
        if exit_match and int(exit_match.group(1)) != 0:
            return True, output, output[-500:]

        return False, output, ""

    return False, "", ""


# ── Tool handlers ────────────────────────────────────────────────────────

def handle_bash(data: dict, state_dir: Path) -> dict | None:
    """Handle Bash tool: record errors/resolutions, search on errors."""
    tool_input = data.get("tool_input", {})
    command = ""
    if isinstance(tool_input, dict):
        command = tool_input.get("command", "")

    is_error, output, error_text = detect_bash_error(data)

    if not output and not error_text:
        return None

    if is_error:
        # ── Error: record to state + store signature ──
        append_event(state_dir, "errors.jsonl", {
            "source": "bash",
            "command": command[:200],
            "output_tail": error_text[:500],
        })

        # Check for error recurrence in local store (takes priority)
        # Also stores the error signature for future fuzzy matching
        recurrence_output = _check_error_recurrence(error_text, state_dir)
        if recurrence_output:
            return recurrence_output

        # Search CommonTrace with raw error output (let search engine
        # handle relevance — no keyword extraction needed)
        if not is_on_cooldown("bash_error", 30):
            api_key = load_api_key()
            if api_key:
                set_cooldown("bash_error")
                _record_trigger_safe(state_dir, "bash_error")
                # Use last 200 chars as search query
                query = error_text.strip()[-200:]
                if query:
                    results = search_commontrace(query, api_key)
                    if results:
                        formatted = format_results(results)
                        return {
                            "hookSpecificOutput": {
                                "hookEventName": "PostToolUse",
                                "additionalContext": (
                                    f"CommonTrace found relevant traces "
                                    f"for this error:\n\n{formatted}\n\n"
                                    f"Use get_trace with the ID to read "
                                    f"the full solution."
                                ),
                            }
                        }
    else:
        # ── Success: check if this resolves a previous error ──
        previous_errors = read_events(state_dir, "errors.jsonl")
        if previous_errors:
            append_event(state_dir, "resolutions.jsonl", {
                "source": "bash",
                "command": command[:200],
                "output_preview": output[:200] if output else "",
                "errors_before": len(previous_errors),
            })

    return None


def handle_code_change(data: dict, state_dir: Path) -> dict | None:
    """Handle Write/Edit/NotebookEdit: record file changes + smart triggers."""
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return None

    file_path = tool_input.get("file_path", "")
    if not file_path:
        return None

    tool_name = data.get("tool_name", "")

    # Check pre-code trigger BEFORE recording change (file may not exist yet)
    trigger_output = _check_pre_code(file_path, tool_name, state_dir)

    append_event(state_dir, "changes.jsonl", {
        "tool": tool_name,
        "file": file_path,
        "is_config": is_config_file(file_path),
    })

    # Check domain entry trigger after recording
    if trigger_output is None:
        trigger_output = _check_domain_entry(file_path, state_dir)

    return trigger_output


def handle_research(data: dict, state_dir: Path) -> None:
    """Handle WebSearch/WebFetch: record research activity."""
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return

    tool_name = data.get("tool_name", "")
    query = tool_input.get("query", tool_input.get("url", ""))

    append_event(state_dir, "research.jsonl", {
        "tool": tool_name,
        "query": str(query)[:200],
    })


def _record_trigger_safe(state_dir: Path, trigger_name: str) -> None:
    """Record a trigger fire for reinforcement tracking. Never fails."""
    try:
        from local_store import _get_conn, record_trigger
        session_id = state_dir.name
        conn = _get_conn()
        record_trigger(conn, session_id, trigger_name)
        conn.close()
    except Exception:
        pass


def _read_project_id(state_dir: Path) -> int | None:
    """Read project_id bridge file written by session_start."""
    try:
        return int((state_dir / "project_id").read_text(encoding="utf-8").strip())
    except (ValueError, OSError, FileNotFoundError):
        return None


def _read_context_fingerprint(state_dir: Path) -> dict | None:
    """Read context fingerprint bridge file written by session_start."""
    try:
        return json.loads(
            (state_dir / "context_fingerprint.json").read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return None


def _check_error_recurrence(error_text: str, state_dir: Path) -> dict | None:
    """Trigger enriched search when error matches previous session patterns.

    Uses fuzzy signature matching (Jaccard similarity on normalized tokens)
    instead of exact hash — catches the same exception at different line
    numbers, different file paths, etc.
    """
    if is_on_cooldown("error_recurrence", 60):
        return None

    project_id = _read_project_id(state_dir)
    if project_id is None:
        return None

    try:
        from local_store import (
            _get_conn, find_similar_errors, record_error_signature,
            record_trigger,
        )
        from session_state import error_signature

        sig = error_signature(error_text)
        session_id = state_dir.name
        conn = _get_conn()

        # Store this error's signature for future sessions
        record_error_signature(conn, project_id, session_id, sig,
                               error_text[-500:])

        # Find similar errors from previous sessions
        matches = find_similar_errors(conn, project_id, sig, session_id)

        if matches:
            set_cooldown("error_recurrence")
            record_trigger(conn, session_id, "error_recurrence")
            conn.close()

            api_key = load_api_key()
            if api_key:
                context = _read_context_fingerprint(state_dir)
                query = error_text.strip()[-200:]
                results = search_commontrace(query, api_key, context)
                if results:
                    formatted = format_results(results)
                    best_sim = max(m["similarity"] for m in matches)
                    session_count = len(matches)
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PostToolUse",
                            "additionalContext": (
                                f"Similar error seen in {session_count} "
                                f"previous session(s) ({best_sim:.0%} match). "
                                f"CommonTrace found relevant traces:\n\n"
                                f"{formatted}\n\n"
                                f"Use get_trace with the ID to read "
                                f"the full solution."
                            ),
                        }
                    }
        else:
            conn.close()
    except Exception:
        pass
    return None


def _check_domain_entry(file_path: str, state_dir: Path) -> dict | None:
    """Trigger search when entering an unfamiliar language domain."""
    if is_on_cooldown("domain_entry", 120):
        return None

    ext = Path(file_path).suffix.lower()
    lang = EXTENSION_TO_LANGUAGE.get(ext)
    if not lang:
        return None

    project_id = _read_project_id(state_dir)
    if project_id is None:
        return None

    try:
        from local_store import _get_conn, get_known_languages
        conn = _get_conn()
        known = get_known_languages(conn, project_id)
        conn.close()

        if lang not in known:
            set_cooldown("domain_entry")
            _record_trigger_safe(state_dir, "domain_entry")
            # Write bridge file for stop hook novelty scoring
            try:
                (state_dir / "domain_entry_fired").write_text(
                    lang, encoding="utf-8")
            except OSError:
                pass
            api_key = load_api_key()
            if api_key:
                query = f"{lang} common patterns and gotchas"
                results = search_commontrace(query, api_key)
                if results:
                    formatted = format_results(results)
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PostToolUse",
                            "additionalContext": (
                                f"You're working in {lang} for the first "
                                f"time in this project. CommonTrace found "
                                f"relevant knowledge:\n\n{formatted}\n\n"
                                f"Use get_trace with the ID to read "
                                f"the full solution."
                            ),
                        }
                    }
    except Exception:
        pass
    return None


# ── Knowledge candidate detection ────────────────────────────────────────

def _detect_knowledge_candidates(tool_name: str, data: dict,
                                  state_dir: Path) -> None:
    """Detect knowledge crystallization moments from tool-use sequences.

    Writes candidates to candidates.jsonl when a state transition occurs.
    Each candidate captures the pattern type and surrounding context so
    the stop hook can score importance and pre-assemble contribution drafts.
    """
    now = time.time()

    # ── Search→Implement: research followed by code changes, no errors ──
    if tool_name in ("Write", "Edit", "NotebookEdit"):
        research = read_events(state_dir, "research.jsonl")
        errors = read_events(state_dir, "errors.jsonl")
        changes = read_events(state_dir, "changes.jsonl")

        if research and not errors:
            # Research happened, no errors — agent learned then implemented
            last_research_t = max(r.get("t", 0) for r in research)
            # Only fire if research was recent (within last 10 minutes)
            if now - last_research_t < 600:
                # Dedup: check if we already recorded this transition
                if not _has_candidate(state_dir, "research_then_implement"):
                    queries = [r.get("query", "")[:100] for r in research[-3:]]
                    file_path = ""
                    ti = data.get("tool_input", {})
                    if isinstance(ti, dict):
                        file_path = ti.get("file_path", "")
                    append_event(state_dir, "candidates.jsonl", {
                        "pattern": "research_then_implement",
                        "research_queries": queries,
                        "file": file_path,
                        "research_count": len(research),
                        "changes_count": len(changes) + 1,
                    })

    # ── Fail→Succeed: bash success after previous errors + changes ──
    if tool_name == "Bash":
        is_error, output, error_text = detect_bash_error(data)
        if not is_error and output:
            errors = read_events(state_dir, "errors.jsonl")
            changes = read_events(state_dir, "changes.jsonl")
            if errors and changes:
                last_error_t = max(e.get("t", 0) for e in errors)
                last_change_t = max(c.get("t", 0) for c in changes)
                # Error → change → success (temporal order)
                if last_error_t < last_change_t:
                    if not _has_candidate(state_dir, "fail_then_succeed"):
                        error_summary = errors[-1].get("output_tail", "")[:200]
                        changed_files = list({
                            c.get("file", "") for c in changes
                            if c.get("t", 0) > last_error_t
                        })
                        append_event(state_dir, "candidates.jsonl", {
                            "pattern": "fail_then_succeed",
                            "error_count": len(errors),
                            "error_summary": error_summary,
                            "fix_files": changed_files[:5],
                            "verification": output[:200],
                        })

    # ── Approach Reversal: Write to file previously Edit-ed 3+ times ──
    if tool_name == "Write":
        ti = data.get("tool_input", {})
        file_path = ti.get("file_path", "") if isinstance(ti, dict) else ""
        if file_path:
            changes = read_events(state_dir, "changes.jsonl")
            edit_count = sum(
                1 for c in changes
                if c.get("file") == file_path and c.get("tool") == "Edit"
            )
            if edit_count >= 3:
                if not _has_candidate(state_dir, "approach_reversal",
                                      file_path):
                    append_event(state_dir, "candidates.jsonl", {
                        "pattern": "approach_reversal",
                        "file": file_path,
                        "previous_edits": edit_count,
                    })

    # ── Cross-file Breadth: changes spanning 3+ directories ──
    if tool_name in ("Write", "Edit", "NotebookEdit"):
        changes = read_events(state_dir, "changes.jsonl")
        ti = data.get("tool_input", {})
        file_path = ti.get("file_path", "") if isinstance(ti, dict) else ""
        all_files = [c.get("file", "") for c in changes]
        if file_path:
            all_files.append(file_path)
        dirs = {str(Path(f).parent) for f in all_files if f}
        if len(dirs) >= 3:
            if not _has_candidate(state_dir, "cross_file_breadth"):
                append_event(state_dir, "candidates.jsonl", {
                    "pattern": "cross_file_breadth",
                    "directories": list(dirs)[:10],
                    "file_count": len(set(all_files)),
                })

    # ── User Correction: same file changed before and after user message ──
    if tool_name in ("Write", "Edit", "NotebookEdit"):
        ti = data.get("tool_input", {})
        file_path = ti.get("file_path", "") if isinstance(ti, dict) else ""
        if file_path:
            user_turns = read_events(state_dir, "user_turns.jsonl")
            changes = read_events(state_dir, "changes.jsonl")
            if user_turns and len(changes) >= 2:
                last_turn_t = max(u.get("t", 0) for u in user_turns)
                # Changes to same file BEFORE the last user turn
                pre_turn_edits = [
                    c for c in changes
                    if c.get("file") == file_path
                    and c.get("t", 0) < last_turn_t
                ]
                # Current edit is AFTER user turn (we're in post_tool_use)
                if pre_turn_edits and now > last_turn_t:
                    if not _has_candidate(state_dir, "user_correction",
                                          file_path):
                        append_event(state_dir, "candidates.jsonl", {
                            "pattern": "user_correction",
                            "file": file_path,
                            "pre_turn_edits": len(pre_turn_edits),
                        })

    # ── Test Fix Cycle: test fails → code changes → test passes ──
    if tool_name == "Bash":
        ti = data.get("tool_input", {})
        command = ti.get("command", "") if isinstance(ti, dict) else ""
        is_error, output, error_text = detect_bash_error(data)
        if not is_error and any(tc in command for tc in TEST_COMMANDS):
            errors = read_events(state_dir, "errors.jsonl")
            changes = read_events(state_dir, "changes.jsonl")
            test_failures = [
                e for e in errors
                if any(tc in e.get("command", "") for tc in TEST_COMMANDS)
            ]
            non_test_changes = [
                c for c in changes
                if "test" not in c.get("file", "").lower()
                and "spec" not in c.get("file", "").lower()
            ]
            if test_failures and non_test_changes:
                if not _has_candidate(state_dir, "test_fix_cycle"):
                    append_event(state_dir, "candidates.jsonl", {
                        "pattern": "test_fix_cycle",
                        "test_failures": len(test_failures),
                        "fix_files": [
                            c.get("file") for c in non_test_changes[:5]
                        ],
                    })

    # ── Dependency Resolution: package errors → config changes → success ──
    if tool_name == "Bash":
        ti = data.get("tool_input", {})
        command = ti.get("command", "") if isinstance(ti, dict) else ""
        is_error, output, error_text = detect_bash_error(data)
        if not is_error and any(pc in command for pc in PACKAGE_COMMANDS):
            errors = read_events(state_dir, "errors.jsonl")
            changes = read_events(state_dir, "changes.jsonl")
            pkg_errors = [
                e for e in errors
                if any(pc in e.get("command", "") for pc in PACKAGE_COMMANDS)
            ]
            config_changes = [
                c for c in changes if c.get("is_config")
            ]
            if pkg_errors and config_changes:
                if not _has_candidate(state_dir, "dependency_resolution"):
                    append_event(state_dir, "candidates.jsonl", {
                        "pattern": "dependency_resolution",
                        "error_count": len(pkg_errors),
                        "config_files": [
                            c.get("file") for c in config_changes[:3]
                        ],
                    })

    # ── Security Hardening: security file changes + errors ──
    if tool_name in ("Write", "Edit", "NotebookEdit"):
        ti = data.get("tool_input", {})
        file_path = ti.get("file_path", "") if isinstance(ti, dict) else ""
        if file_path and _is_security_file(file_path):
            errors = read_events(state_dir, "errors.jsonl")
            if errors:
                if not _has_candidate(state_dir, "security_hardening"):
                    append_event(state_dir, "candidates.jsonl", {
                        "pattern": "security_hardening",
                        "security_files": [file_path],
                        "error_count": len(errors),
                    })

    # Also detect security tool success after failures
    if tool_name == "Bash":
        ti = data.get("tool_input", {})
        command = ti.get("command", "") if isinstance(ti, dict) else ""
        is_error, output, error_text = detect_bash_error(data)
        if not is_error and any(sc in command for sc in SECURITY_COMMANDS):
            errors = read_events(state_dir, "errors.jsonl")
            security_errors = [
                e for e in errors
                if any(sc in e.get("command", "") for sc in SECURITY_COMMANDS)
            ]
            if security_errors:
                if not _has_candidate(state_dir, "security_hardening"):
                    changes = read_events(state_dir, "changes.jsonl")
                    append_event(state_dir, "candidates.jsonl", {
                        "pattern": "security_hardening",
                        "security_files": [
                            c.get("file") for c in changes
                            if _is_security_file(c.get("file", ""))
                        ][:3],
                        "error_count": len(security_errors),
                    })

    # ── Infra Discovery: infrastructure file changes + errors ──
    if tool_name in ("Write", "Edit", "NotebookEdit"):
        ti = data.get("tool_input", {})
        file_path = ti.get("file_path", "") if isinstance(ti, dict) else ""
        if file_path and _is_infra_file(file_path):
            errors = read_events(state_dir, "errors.jsonl")
            if errors:
                if not _has_candidate(state_dir, "infra_discovery"):
                    changes = read_events(state_dir, "changes.jsonl")
                    infra_files = [
                        c.get("file") for c in changes
                        if _is_infra_file(c.get("file", ""))
                    ]
                    append_event(state_dir, "candidates.jsonl", {
                        "pattern": "infra_discovery",
                        "infra_files": (infra_files + [file_path])[:3],
                        "error_count": len(errors),
                    })

    # ── Migration Pattern: 5+ files across 2+ dirs + config changes ──
    if tool_name in ("Write", "Edit"):
        changes = read_events(state_dir, "changes.jsonl")
        config_changes = [c for c in changes if c.get("is_config")]
        all_files = set(c.get("file", "") for c in changes)
        all_dirs = set(str(Path(f).parent) for f in all_files if f)
        if len(all_files) >= 5 and config_changes and len(all_dirs) >= 2:
            if not _has_candidate(state_dir, "migration_pattern"):
                append_event(state_dir, "candidates.jsonl", {
                    "pattern": "migration_pattern",
                    "total_files": len(all_files),
                    "config_files": [
                        c.get("file") for c in config_changes[:3]
                    ],
                    "directories": list(all_dirs)[:5],
                })


def _has_candidate(state_dir: Path, pattern: str,
                   extra_key: str = "") -> bool:
    """Check if a knowledge candidate of this type already exists."""
    candidates = read_events(state_dir, "candidates.jsonl")
    for c in candidates:
        if c.get("pattern") == pattern:
            if extra_key and c.get("file") != extra_key:
                continue
            return True
    return False


def _check_pre_code(file_path: str, tool_name: str,
                    state_dir: Path = None) -> dict | None:
    """Trigger search before implementing a new file."""
    if tool_name != "Write":
        return None
    if is_on_cooldown("pre_code", 180):
        return None
    if Path(file_path).exists():
        return None

    ext = Path(file_path).suffix.lower()
    lang = EXTENSION_TO_LANGUAGE.get(ext)
    if not lang:
        return None

    set_cooldown("pre_code")
    if state_dir:
        _record_trigger_safe(state_dir, "pre_code")
    api_key = load_api_key()
    if api_key:
        name = Path(file_path).stem.lower()
        query = f"{lang} {name} implementation patterns"
        results = search_commontrace(query, api_key)
        if results:
            formatted = format_results(results)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"Before implementing {Path(file_path).name}, "
                        f"CommonTrace found relevant patterns:\n\n"
                        f"{formatted}\n\n"
                        f"Use get_trace with the ID to read "
                        f"the full solution."
                    ),
                }
            }
    return None


def handle_trace_consumption(data: dict, state_dir: Path) -> None:
    """Handle get_trace: record that a trace was consumed (reinforcement)."""
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return

    trace_id = tool_input.get("trace_id", "")
    if not trace_id:
        return

    try:
        from local_store import _get_conn, record_trace_consumed
        session_id = state_dir.name
        conn = _get_conn()
        record_trace_consumed(conn, session_id, trace_id)
        conn.close()
    except Exception:
        pass


def handle_contribution(data: dict, state_dir: Path) -> None:
    """Handle MCP contribute_trace: record contribution."""
    tool_response = data.get("tool_response", {})
    response_text = str(tool_response)

    match = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}", response_text)
    trace_id = match.group(0) if match else ""

    append_event(state_dir, "contributions.jsonl", {
        "trace_id": trace_id,
    })

    # Record turn count at contribution time so Stop hook can detect
    # how many user messages came AFTER the contribution
    try:
        path = state_dir / "user_turn_count"
        count = int(path.read_text(encoding="utf-8").strip()) if path.exists() else 0
        (state_dir / "user_turns_at_contribution").write_text(
            str(count), encoding="utf-8")
    except (ValueError, OSError):
        pass


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return

    tool_name = data.get("tool_name", "")
    if not tool_name:
        return

    state_dir = get_state_dir(data)
    output = None

    # Detect knowledge crystallization on every tool use
    _detect_knowledge_candidates(tool_name, data, state_dir)

    if tool_name == "Bash":
        output = handle_bash(data, state_dir)

    elif tool_name in ("Write", "Edit", "NotebookEdit"):
        output = handle_code_change(data, state_dir)

    elif tool_name in ("WebSearch", "WebFetch"):
        handle_research(data, state_dir)

    elif "get_trace" in tool_name:
        handle_trace_consumption(data, state_dir)

    elif "contribute_trace" in tool_name:
        handle_contribution(data, state_dir)

    if output:
        print(json.dumps(output))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
