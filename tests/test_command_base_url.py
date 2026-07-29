"""Slash commands must honour COMMONTRACE_API_BASE_URL, like the hooks do."""
import pathlib

import pytest

COMMANDS_DIR = pathlib.Path(__file__).resolve().parents[1] / "commands"
GATED = ["tutorial-contribution.md", "tutorial-retrieval.md", "trace.md"]
FALLBACK = "${COMMONTRACE_API_BASE_URL:-https://api.commontrace.org}"


@pytest.mark.parametrize("name", GATED)
def test_command_declares_the_base_url_fallback(name):
    body = (COMMANDS_DIR / name).read_text()

    assert FALLBACK in body, f"{name} must resolve the API base URL from the environment"


@pytest.mark.parametrize("name", GATED)
def test_command_has_no_bare_production_url(name):
    body = (COMMANDS_DIR / name).read_text()
    # Every occurrence of the production host must be inside the shell default.
    for line in body.splitlines():
        if "api.commontrace.org/api/v1" in line:
            assert FALLBACK in line, f"{name}: bare production URL on line: {line.strip()}"
