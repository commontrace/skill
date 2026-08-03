"""Did that Bash command fail? Structural classification, nothing else.

This one question sits under most of the learning loop — error events,
resolution pairing, error_resolution scoring — and it is subtle enough
(piped exit codes, tools that write normal output to stderr) to deserve
its own module with its own tests.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from redact import strip_harness_noise


# Precise failure markers for the exit-code 0/None case. A pipeline's exit
# code is the LAST command's, so `npx jest | tail` exits 0 even when jest
# failed — the failure survives only as text. Anchor on strong, specific
# signals (never the bare word "error", never a "0 failed" summary) so a GREEN
# run that merely mentions "error" or reports "0 failed" is not misclassified.
_FAILURE_MARKER_PATTERNS = (
    re.compile(r'\bFAIL'),                              # jest FAIL, go --- FAIL, pytest FAILED
    re.compile(r'\bAssertionError\b'),                  # python/unittest
    re.compile(r'\bTests?:\s*[1-9]\d*\s+failed', re.IGNORECASE),  # jest "Tests: 1 failed"
    re.compile(r'\b[1-9]\d*\s+failed\b', re.IGNORECASE),          # "1 failed" (not "0 failed")
    re.compile(r'(?im)^\s*exit code [1-9]'),            # explicit non-zero exit line
    re.compile(r'(?m)^Error:'),                         # node/js error header (line-anchored)
    re.compile(r'Traceback \(most recent call last\)'),  # python traceback
)


def _has_failure_marker(output: str, stderr: str) -> bool:
    """True if combined output+stderr carries a precise failure marker.

    Used only when the exit code is 0 or absent — the case a piped test run
    (`… | tail`) reports success it didn't earn. Strong markers only, so a
    passing run is never flagged just for containing the word "error".
    """
    combined = f"{output or ''}\n{stderr or ''}"
    return any(p.search(combined) for p in _FAILURE_MARKER_PATTERNS)


def detect_bash_error(data: dict) -> tuple[bool, str, str]:
    """Detect if a Bash command failed using structural signals only.

    Checks (in order):
    1. Non-zero exit code in tool_response (most reliable).
    2. Exit code 0 or None: scan combined output+stderr for precise failure
       markers (catches piped failures whose exit code is the pipe's last
       command). NON-empty stderr is NOT treated as failure on a clean exit —
       jest/pytest/cargo/go/npm/git all write NORMAL output to stderr on a
       GREEN run, so the old "stderr = error" rule stored passing runs as
       errors and never recorded a resolution (fail→succeed could never fire).
    3. Exit code None with no marker: fall back to the Unix stderr convention.

    The stdout key matters as much as the error signals: a command that
    succeeds carries stdout and an empty stderr, and handle_bash drops the
    event entirely when both output and error_text are empty. Miss stdout
    and every success becomes invisible — no resolution is ever paired, no
    signature is ever marked resolved, and the whole assisted-resolution
    loop reports zero.

    Claude Code sends {"stdout", "stderr", "interrupted", "isImage",
    "noOutputExpected"} for Bash — no exit code, and stdout is NOT named
    "output". Both spellings are accepted so other harnesses still work.

    Returns: (is_error, output_text, error_text_for_search)
    """
    tool_response = data.get("tool_response", {})

    if isinstance(tool_response, dict):
        output = tool_response.get("stdout")
        if not output:
            output = tool_response.get("output", "")
        stderr = tool_response.get("stderr", "")
        exit_code = tool_response.get("exitCode",
                    tool_response.get("exit_code"))

        # 1. Non-zero exit code is the clearest structural signal.
        if exit_code is not None and exit_code != 0:
            # Use stderr if available, otherwise tail of output
            error_text = stderr if stderr else output[-500:]
            return True, output, strip_harness_noise(error_text)

        # 2. Exit code is 0 or None — scan for precise failure markers so a
        #    piped test run that "succeeded" via tail/head is still caught.
        if _has_failure_marker(output, stderr):
            return True, output, strip_harness_noise((stderr or output)[-500:])

        # Explicit success (exit 0): trust it. Non-empty stderr on a clean
        # exit is normal tool chatter, NOT an error.
        if exit_code == 0:
            return False, output or stderr, ""

        # 3. Exit code unknown (None) and no marker: Unix stderr convention.
        if stderr and stderr.strip():
            return True, output, strip_harness_noise(stderr[-500:])

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
            return True, output, strip_harness_noise(output[-500:])

        # Piped failure captured as a plain string (exit code hidden) — the
        # marker scan still catches it without over-firing on a passing run.
        if _has_failure_marker(output, ""):
            return True, output, strip_harness_noise(output[-500:])

        return False, output, ""

    return False, "", ""
