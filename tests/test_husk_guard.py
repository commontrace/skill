"""Harness-noise filter.

Defense against agent-runtime notices leaking into captured errors, error
signatures, or published traces:

  redact.strip_harness_noise / contains_harness_noise — notices like
  "Shell cwd was reset to /home/..." never enter a captured error, an
  error signature, or a published trace (the path-leak vector).

NOTE: the old stop._is_husk auto-submit quality floor was removed with the
instant-handoff pivot (commit 686a2d7) — /trace now runs in a background
subagent that authors real content, so the main thread never silently
auto-submits a template husk. Its tests were dropped with it.
"""

import sys
import unittest
from pathlib import Path

from base import HookTestCase  # noqa: F401  (path bootstrap)

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import redact  # noqa: E402
import post_tool_use  # noqa: E402


class HarnessNoiseTests(unittest.TestCase):
    def test_contains_noise_true(self):
        self.assertTrue(
            redact.contains_harness_noise(
                "Shell cwd was reset to /home/bitnami/project"))

    def test_contains_noise_system_reminder(self):
        self.assertTrue(
            redact.contains_harness_noise("<system-reminder>do x</system-reminder>"))

    def test_contains_noise_false_on_real_error(self):
        self.assertFalse(
            redact.contains_harness_noise(
                "ModuleNotFoundError: No module named 'asyncpg'"))

    def test_strip_removes_reset_line_keeps_real_error(self):
        raw = (
            "Traceback (most recent call last):\n"
            "  File 'app.py', line 3\n"
            "ImportError: cannot import name 'foo'\n"
            "Shell cwd was reset to /home/bitnami/secret-project"
        )
        cleaned = redact.strip_harness_noise(raw)
        self.assertIn("ImportError", cleaned)
        self.assertNotIn("Shell cwd was reset", cleaned)
        self.assertNotIn("secret-project", cleaned)

    def test_strip_all_noise_yields_empty(self):
        raw = "Shell cwd was reset to /home/x\ncwd was reset to /home/y"
        self.assertEqual(redact.strip_harness_noise(raw), "")

    def test_strip_empty_passthrough(self):
        self.assertEqual(redact.strip_harness_noise(""), "")


class DetectBashErrorNoiseTests(unittest.TestCase):
    def test_error_text_scrubbed_of_reset_notice(self):
        data = {
            "tool_response": {
                "output": "boom\nShell cwd was reset to /home/bitnami/proj",
                "stderr": "",
                "exitCode": 1,
            }
        }
        is_error, _output, error_text = post_tool_use.detect_bash_error(data)
        self.assertTrue(is_error)
        self.assertNotIn("Shell cwd was reset", error_text)
        self.assertNotIn("/home/bitnami", error_text)

    def test_stderr_path_preserved_when_clean(self):
        data = {
            "tool_response": {
                "output": "",
                "stderr": "fatal: not a git repository",
                "exitCode": 128,
            }
        }
        is_error, _output, error_text = post_tool_use.detect_bash_error(data)
        self.assertTrue(is_error)
        self.assertIn("not a git repository", error_text)


if __name__ == "__main__":
    unittest.main()
