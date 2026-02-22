"""Shared session state for CommonTrace hooks.

All Layer 1 hooks write structural signals here. The Stop hook (Layer 2)
reads everything to detect patterns.

State lives in /tmp/commontrace-session-{session_id}/ as JSONL files
(append-only, one JSON object per line). No locking needed — atomic
appends on Linux for short lines.

Files:
  errors.jsonl      — Bash errors and tool failures
  resolutions.jsonl — successful Bash runs after previous errors
  changes.jsonl     — files modified by Write/Edit/NotebookEdit
  research.jsonl    — WebSearch/WebFetch usage
  contributions.jsonl — traces contributed via MCP
  user_turn_count   — plain integer, incremented per real user message
"""

import hashlib
import json
import os
import time
from pathlib import Path


STATE_ROOT = Path("/tmp/commontrace-sessions")

# Config-like file patterns (for detecting configuration changes)
CONFIG_EXTENSIONS = {
    ".json", ".yaml", ".yml", ".toml", ".ini", ".env", ".cfg",
    ".conf", ".config", ".properties", ".xml", ".plist",
}
CONFIG_NAME_FRAGMENTS = {
    "config", "settings", "setup", ".env", "nginx", "apache",
    "docker", "compose", "makefile", "gemfile", "cargo",
    "package.json", "tsconfig", "webpack", "vite", "babel",
    "eslint", "prettier", "pyproject", "setup.py", "setup.cfg",
}


def get_state_dir(data: dict) -> Path:
    """Get or create session state directory."""
    session_id = data.get("session_id") or str(os.getppid())
    d = STATE_ROOT / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def append_event(state_dir: Path, filename: str, entry: dict) -> None:
    """Append a JSON event to a JSONL state file."""
    entry.setdefault("t", time.time())
    try:
        with open(state_dir / filename, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def read_events(state_dir: Path, filename: str) -> list[dict]:
    """Read all events from a JSONL state file."""
    try:
        path = state_dir / filename
        if not path.exists():
            return []
        entries = []
        for line in path.read_text(encoding="utf-8").strip().split("\n"):
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries
    except OSError:
        return []


def read_counter(state_dir: Path, filename: str) -> int:
    """Read a simple integer counter."""
    try:
        path = state_dir / filename
        if not path.exists():
            return 0
        return int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return 0


def increment_counter(state_dir: Path, filename: str) -> int:
    """Increment and return a simple integer counter."""
    count = read_counter(state_dir, filename) + 1
    try:
        (state_dir / filename).write_text(str(count), encoding="utf-8")
    except OSError:
        pass
    return count


def error_hash(text: str) -> str:
    """Short hash for deduplicating errors."""
    return hashlib.md5(text[:300].encode()).hexdigest()[:10]


def is_config_file(file_path: str) -> bool:
    """Check if a file path looks like a configuration file."""
    p = Path(file_path)
    name_lower = p.name.lower()
    suffix_lower = p.suffix.lower()

    if suffix_lower in CONFIG_EXTENSIONS:
        return True
    for fragment in CONFIG_NAME_FRAGMENTS:
        if fragment in name_lower:
            return True
    return False
