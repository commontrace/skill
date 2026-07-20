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
import re
import time
from pathlib import Path


STATE_ROOT = Path.home() / ".commontrace" / "sessions"

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
    # M18: Sanitize session_id — allow only alphanumeric, hyphens, underscores
    session_id = re.sub(r'[^a-zA-Z0-9_-]', '', session_id)
    if not session_id:
        session_id = str(os.getppid())
    d = STATE_ROOT / session_id
    # H9: Create with restrictive permissions (owner-only)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Ensure parent dir is also restrictive
    if STATE_ROOT.exists():
        try:
            os.chmod(STATE_ROOT, 0o700)
        except OSError:
            pass
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
    return hashlib.sha256(text[:300].encode()).hexdigest()[:10]


def error_signature(text: str) -> str:
    """Extract a fuzzy error signature by stripping variable parts.

    Normalizes line numbers, file paths, hex addresses, timestamps,
    and UUIDs so that the same error with different context produces
    the same (or very similar) signature. This enables cross-session
    fuzzy matching — the same exception at different line numbers or
    in different files will match.
    """
    import re
    sig = text[:500]
    # Strip file paths (keep only basename)
    sig = re.sub(r'(?:/[\w.-]+)+/([\w.-]+)', r'\1', sig)
    # Windows paths
    sig = re.sub(r'(?:[A-Z]:\\[\w.-]+\\)+([\w.-]+)', r'\1', sig)
    # Line/column numbers
    sig = re.sub(r'(?:line|ln|l)\s*\d+', 'line N', sig, flags=re.IGNORECASE)
    sig = re.sub(r':\d+:\d+', ':N:N', sig)
    sig = re.sub(r':\d+', ':N', sig)
    # Hex addresses
    sig = re.sub(r'0x[0-9a-fA-F]+', '0xADDR', sig)
    # UUIDs
    sig = re.sub(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
        'UUID', sig, flags=re.IGNORECASE)
    # Timestamps (ISO, epoch-like)
    sig = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[\w:.]*', 'TIMESTAMP', sig)
    sig = re.sub(r'\b\d{10,13}\b', 'EPOCH', sig)
    # Collapse whitespace
    sig = re.sub(r'\s+', ' ', sig).strip()
    return sig


def canonical_signature(text: str, max_msg_words: int = 6) -> str:
    """Return a compact dotted canonical signature for backend lookup.

    Tries to extract a Python/Node-style exception line:
      module.ClassName: message text
    and turns it into a lowercase dotted slug like:
      sqlalchemy.exc.missinggreenlet.greenlet.spawn.has.not.been.called

    Falls back to slugifying the fuzzy error_signature() output.
    """
    # Strip variable parts (paths, line numbers, hex, UUIDs, timestamps).
    sig = error_signature(text[:500])

    # Drop common location phrases ("at /path/to/file.js:12", "in (x, y)")
    # before token extraction so location noise does not enter the signature.
    sig = re.sub(r'\bat\s+\S+', '', sig, flags=re.IGNORECASE)
    sig = re.sub(r'\bin\s+\([^)]*\)', '', sig, flags=re.IGNORECASE)

    # Find the last exception line: optional module.Class followed by ':'.
    pattern = re.compile(
        r'(?:([a-zA-Z_][\w.]*)\.)?'
        r'([A-Z][A-Za-z0-9_]*)'
        r'\s*:\s*([^\n]+)'
    )
    matches = list(pattern.finditer(sig))

    def _words(part: str) -> list[str]:
        return [w for w in re.split(r'[^a-z0-9]+', part.lower())
                if len(w) > 1 and not w.isdigit()]

    if matches:
        m = matches[-1]
        module_part = (m.group(1) or "").lower().strip(".")
        class_part = m.group(2).lower()
        msg = m.group(3)
        parts = _words(module_part) + _words(class_part)
        parts.extend(_words(msg)[:max_msg_words])
        return ".".join(parts)[:500]

    # Fallback: slugify the whole normalized signature.
    return ".".join(_words(sig))[:500]


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
