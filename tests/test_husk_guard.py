"""Husk-guard + harness-noise filter.

Two defenses against the auto-contribute faucet that minted 49 template
"husk" traces into the public wiki:

  1. redact.strip_harness_noise / contains_harness_noise — agent-runtime
     notices ("Shell cwd was reset to /home/...") never enter a captured
     error, an error signature, or a published trace (the path-leak vector).
  2. stop._is_husk — the Stop hook refuses to *silently* auto-submit a
     candidate whose context/solution is empty, the bare journey template,
     or noise-tainted. Husks route to the manual-review pending file instead.
"""

import sys
import unittest
from pathlib import Path

from base import HookTestCase  # noqa: F401  (path bootstrap)

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import redact  # noqa: E402
import stop  # noqa: E402
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


class IsHuskTests(unittest.TestCase):
    def test_empty_context_is_husk(self):
        self.assertTrue(stop._is_husk({
            "suggested_context_text": "",
            "suggested_solution_text": "anything real here",
        }))

    def test_empty_solution_is_husk(self):
        self.assertTrue(stop._is_husk({
            "suggested_context_text": "a genuine paragraph of context",
            "suggested_solution_text": "   ",
        }))

    def test_template_context_is_husk(self):
        self.assertTrue(stop._is_husk({
            "suggested_context_text": "When working with , encountered: foo...",
            "suggested_solution_text": "real solution prose",
        }))

    def test_template_context_with_language_still_husk(self):
        # The husks with a language slot ("...python, encountered...") are
        # still the mechanical template, not knowledge.
        self.assertTrue(stop._is_husk({
            "suggested_context_text": "When working with python fastapi, encountered: KeyError...",
            "suggested_solution_text": "real solution prose",
        }))

    def test_template_solution_is_husk(self):
        self.assertTrue(stop._is_husk({
            "suggested_context_text": "a genuine paragraph of context",
            "suggested_solution_text": "Resolution involved changing auth.py, db.py.",
        }))

    def test_noise_tainted_context_is_husk(self):
        self.assertTrue(stop._is_husk({
            "suggested_context_text": "Shell cwd was reset to /home/bitnami/x",
            "suggested_solution_text": "real solution prose",
        }))

    def test_real_candidate_is_not_husk(self):
        self.assertFalse(stop._is_husk({
            "suggested_context_text": (
                "Deploying a Vite SPA to Railway via Nixpacks fails with EBUSY "
                "on node_modules because the builder symlinks the volume."),
            "suggested_solution_text": (
                "Add a .nixpacks config pinning the install phase to a copied "
                "node_modules dir; set NIXPACKS_NODE_MODULES_CACHE=false."),
        }))


if __name__ == "__main__":
    unittest.main()
