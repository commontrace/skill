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
    """Redact secrets from a bash command string."""
    if not command:
        return command
    # Redact inline env vars with secret-like names
    result = re.sub(
        r'(?:API_KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL|AUTH_TOKEN)\s*=\s*\S+',
        r'\g<0>'.split('=')[0] + '=[REDACTED]' if '=' in command else '[REDACTED]',
        command,
        flags=re.IGNORECASE,
    )
    # Simpler approach: just redact the value part
    result = re.sub(
        r'((?:API_KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL|AUTH_TOKEN)\s*=)\s*\S+',
        r'\1[REDACTED]',
        command,
        flags=re.IGNORECASE,
    )
    # Redact -p/--password arguments
    result = re.sub(r'(-p\s*|--password[= ])\S+', r'\1[REDACTED]', result)
    return result
