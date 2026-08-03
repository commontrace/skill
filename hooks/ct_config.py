"""The user-level CommonTrace config: one location, one reader, one writer.

``~/.commontrace/config.json`` holds the API key and the handful of opt-in
flags (auto_contribute, telemetry, resolved_with_trailer…). Four hooks used
to declare their own ``CONFIG_FILE`` constant and their own try/except
reader, which meant four places to patch in tests and four chances to drift.

Everything here reads the module-level constants at CALL time, so a test can
point the whole hook suite at a temp dir with a single
``mock.patch.object(ct_config, "CONFIG_FILE", ...)``.
"""

import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".commontrace"
CONFIG_FILE = CONFIG_DIR / "config.json"
COOLDOWN_DIR = CONFIG_DIR / "cooldowns"

API_BASE = "https://api.commontrace.org"


def read_config() -> dict:
    """Read the config file. Returns {} on any failure — never raises."""
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def write_config(config: dict) -> bool:
    """Persist the config with owner-only permissions. Never raises.

    The file holds the API key, so the directory is created 0700 and the
    file forced to 0600 on every write.
    """
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


def load_api_key() -> str:
    """API key from the config file, falling back to the environment."""
    key = read_config().get("api_key", "")
    if key:
        return key
    return os.environ.get("COMMONTRACE_API_KEY", "")


def api_base_url() -> str:
    """API base URL, overridable per-environment. No trailing slash."""
    return os.environ.get("COMMONTRACE_API_BASE_URL", API_BASE).rstrip("/")
