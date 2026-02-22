#!/usr/bin/env python3
"""
CommonTrace SessionStart hook.

Detects coding context at session startup, queries the CommonTrace backend
API directly (bypassing MCP), and injects relevant traces as additionalContext.

Exits 0 silently on any error — never blocks session start.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


# Source file extensions that indicate a coding project
SOURCE_EXTENSIONS = {".py", ".ts", ".js", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb"}

# Extension to language name mapping
EXTENSION_TO_LANGUAGE = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".js": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
}


def detect_context(cwd: str) -> str | None:
    """
    Detect coding context in cwd.

    Returns a query string if a coding context is found, or None if
    this does not appear to be a coding project (or if detection fails).
    """
    cwd_path = Path(cwd)

    # Must be a git repo
    if not (cwd_path / ".git").exists():
        return None

    # Scan top-level files for source extensions
    extension_counts: dict[str, int] = {}
    try:
        for entry in cwd_path.iterdir():
            if entry.is_file() and entry.suffix in SOURCE_EXTENSIONS:
                ext = entry.suffix
                extension_counts[ext] = extension_counts.get(ext, 0) + 1
    except OSError:
        return None

    if not extension_counts:
        return None

    # Primary language = most frequent extension
    primary_ext = max(extension_counts, key=lambda e: extension_counts[e])
    language = EXTENSION_TO_LANGUAGE.get(primary_ext, "")
    if not language:
        return None

    # Framework detection from manifest files
    framework: str | None = None

    pyproject = cwd_path / "pyproject.toml"
    if pyproject.exists():
        try:
            contents = pyproject.read_text(encoding="utf-8").lower()
            if "fastapi" in contents:
                framework = "fastapi"
            elif "django" in contents:
                framework = "django"
            elif "flask" in contents:
                framework = "flask"
        except OSError:
            pass

    package_json = cwd_path / "package.json"
    if package_json.exists() and framework is None:
        try:
            contents = package_json.read_text(encoding="utf-8").lower()
            if "next" in contents:
                framework = "next"
            elif "react" in contents:
                framework = "react"
            elif "express" in contents:
                framework = "express"
            elif "vue" in contents:
                framework = "vue"
        except OSError:
            pass

    if (cwd_path / "Cargo.toml").exists() and framework is None:
        framework = "rust"

    if (cwd_path / "go.mod").exists() and framework is None:
        framework = "go"

    # Build query string
    parts = [language]
    if framework and framework not in (language,):
        parts.append(framework)
    parts.append("common patterns and solutions")
    return " ".join(parts)


def search_commontrace(query: str, language: str) -> list[dict]:
    """
    Query CommonTrace backend API directly via HTTP.

    Returns a list of result dicts on success, or an empty list on any error.
    """
    base_url = os.environ.get("COMMONTRACE_API_BASE_URL", "https://api.commontrace.org").rstrip("/")
    api_key = os.environ.get("COMMONTRACE_API_KEY", "")
    if not api_key:
        return []

    url = f"{base_url}/api/v1/traces/search"
    payload = json.dumps({"q": query, "tags": [language], "limit": 3}).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            body = response.read()
            data = json.loads(body)
            return data.get("results", [])
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError):
        return []


def format_result(result: dict) -> str:
    """Format a single trace result as a human-readable string."""
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
    # Read stdin (may be empty or missing cwd)
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        data = {}

    cwd = data.get("cwd", os.getcwd())
    if not cwd:
        return

    # Detect coding context
    query = detect_context(cwd)
    if not query:
        return

    # Determine primary language for tag filter
    cwd_path = Path(cwd)
    extension_counts: dict[str, int] = {}
    try:
        for entry in cwd_path.iterdir():
            if entry.is_file() and entry.suffix in SOURCE_EXTENSIONS:
                ext = entry.suffix
                extension_counts[ext] = extension_counts.get(ext, 0) + 1
    except OSError:
        return

    if not extension_counts:
        return

    primary_ext = max(extension_counts, key=lambda e: extension_counts[e])
    language = EXTENSION_TO_LANGUAGE.get(primary_ext, "")
    if not language:
        return

    # Query CommonTrace
    results = search_commontrace(query, language)
    if not results:
        return

    # Format results and inject as additionalContext
    formatted = [f"{i + 1}. {format_result(r)}" for i, r in enumerate(results)]
    context_lines = "\n".join(formatted)
    additional_context = (
        f"CommonTrace found relevant knowledge for this project:\n\n"
        f"{context_lines}\n\n"
        f"Use /trace:search for more specific queries."
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
        # Never block session start — silently exit 0
        pass
