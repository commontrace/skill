"""Slash commands must honour COMMONTRACE_API_BASE_URL, like the hooks do."""

import pathlib
import unittest

COMMANDS_DIR = pathlib.Path(__file__).resolve().parent.parent / "commands"
GATED = ["tutorial-contribution.md", "tutorial-retrieval.md", "trace.md"]
FALLBACK = "${COMMONTRACE_API_BASE_URL:-https://api.commontrace.org}"


class TestCommandBaseUrl(unittest.TestCase):
    def test_command_declares_the_base_url_fallback(self):
        for name in GATED:
            with self.subTest(name=name):
                body = (COMMANDS_DIR / name).read_text()
                self.assertIn(
                    FALLBACK, body,
                    f"{name} must resolve the API base URL from the environment")

    def test_command_has_no_bare_production_url(self):
        # Every occurrence of the production host must be inside the shell default.
        for name in GATED:
            with self.subTest(name=name):
                body = (COMMANDS_DIR / name).read_text()
                for line in body.splitlines():
                    if "api.commontrace.org/api/v1" in line:
                        self.assertIn(
                            FALLBACK, line,
                            f"{name}: bare production URL on line: {line.strip()}")


if __name__ == "__main__":
    unittest.main()
