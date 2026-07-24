"""M19/M20/M23: Secret redaction for data leaving the user's machine.

Strips patterns that look like secrets, credentials, or sensitive data
before they are sent to the CommonTrace API or stored in local SQLite.
"""

import re

# Patterns that indicate secrets — matched against text before sending
SECRET_PATTERNS = [
    # API keys, tokens, passwords in URLs or assignments
    re.compile(r'(?:api[_-]?key|token|password|secret|credential|auth)\s*[=:]\s*\S+', re.IGNORECASE),
    # Connection strings with credentials
    re.compile(r'://[^@\s]+:[^@\s]+@'),
    # High-entropy strings (32+ alphanumeric chars — likely tokens)
    re.compile(r'(?:^|[\s=:"\'])([A-Za-z0-9_\-]{40,})(?:[\s"\']|$)'),
    # AWS keys
    re.compile(r'AKIA[0-9A-Z]{16}'),
    # Private keys
    re.compile(r'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----'),
    # Bearer tokens
    re.compile(r'Bearer\s+[A-Za-z0-9\-._~+/]+=*', re.IGNORECASE),
]

# File patterns that should never have their content stored
SENSITIVE_FILE_PATTERNS = {
    '.env', '.env.local', '.env.production', '.env.staging',
    'credentials.json', 'credentials.yaml', 'credentials.yml',
    'service-account.json', 'keyfile.json',
    '.pem', '.key', '.p12', '.pfx', '.jks',
    'id_rsa', 'id_ed25519', 'id_ecdsa',
    '.htpasswd', '.netrc', '.pgpass',
}


def redact_text(text: str) -> str:
    """Redact secrets from text. Returns cleaned version."""
    if not text:
        return text
    result = text
    for pattern in SECRET_PATTERNS:
        result = pattern.sub('[REDACTED]', result)
    return result


def is_sensitive_file(file_path: str) -> bool:
    """Check if a file path matches known sensitive file patterns."""
    if not file_path:
        return False
    path_lower = file_path.lower()
    name = path_lower.rsplit('/', 1)[-1]
    for pattern in SENSITIVE_FILE_PATTERNS:
        if name == pattern or name.endswith(pattern):
            return True
    return False


def redact_command(command: str) -> str:
    """Redact secrets from a bash command string before storage/transport.

    Commands are persisted (local.db fix_command, suggested_solution text),
    so they must be scrubbed as hard as free text. We first strip the
    flag/assignment forms that keep a useful key/flag for context, then run
    the full SECRET_PATTERNS sweep so header-carried secrets
    (`-H "Authorization: Bearer …"`, `-H "x-api-key: …"`), connection-string
    credentials, and AWS keys can't leak.
    """
    if not command:
        return command
    # Named env-var assignments — value redacted, key kept for context.
    result = re.sub(
        r'((?:API_KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL|AUTH_TOKEN)\s*=)\s*\S+',
        r'\1[REDACTED]',
        command,
        flags=re.IGNORECASE,
    )
    # -p/--password flag values (not covered by SECRET_PATTERNS).
    result = re.sub(r'(-p\s*|--password[= ])\S+', r'\1[REDACTED]', result)
    # Full secret-pattern sweep (Bearer, ://user:pass@, `key: value` headers,
    # AWS keys, private-key blocks, high-entropy tokens).
    result = redact_text(result)
    return result


# Agent-runtime noise: strings the harness (Claude Code) appends to tool
# output or injects into the transcript. They are NOT part of the user's
# actual problem and must never enter a captured error, an error signature,
# or a published trace — "Shell cwd was reset to /home/<user>/<project>" is
# how absolute paths leaked into the public wiki.
HARNESS_NOISE_MARKERS = (
    "shell cwd was reset",
    "cwd was reset to",
    "<system-reminder>",
    "</system-reminder>",
    "commontrace found relevant traces",
    "use get_trace with the id",
)


def contains_harness_noise(text: str) -> bool:
    """True if text carries any agent-runtime noise marker (case-insensitive)."""
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in HARNESS_NOISE_MARKERS)


def strip_harness_noise(text: str) -> str:
    """Drop lines carrying agent-runtime noise from captured output.

    Applied to bash error capture before it is stored or signatured, so
    harness notices never leak a path into local.db or a public trace.
    Returns the surviving lines joined and trimmed (may be "" if all noise).
    """
    if not text:
        return text
    kept = [ln for ln in text.splitlines() if not contains_harness_noise(ln)]
    return "\n".join(kept).strip()
