"""Canonical dotted error-signature extraction for known-fix lookup."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))

from session_state import canonical_signature


class TestCanonicalSignature(unittest.TestCase):
    def test_python_exception(self):
        text = (
            "Traceback (most recent call last):\n"
            "  File '/app/models.py', line 42\n"
            "sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called; "
            "can't workaround asyncio greenlet spawn in (x, y)\n"
        )
        sig = canonical_signature(text)
        self.assertEqual(
            sig,
            "sqlalchemy.exc.missinggreenlet.greenlet.spawn.has.not.been.called",
        )

    def test_node_error(self):
        text = "Error: Cannot find module 'express' at /home/user/app.js:12"
        sig = canonical_signature(text)
        self.assertEqual(sig, "error.cannot.find.module.express")

    def test_fileno_exception_no_module(self):
        text = "FileNotFoundError: [Errno 2] No such file or directory: '/tmp/foo'"
        sig = canonical_signature(text)
        self.assertTrue(sig.startswith("filenotfounderror.errno.no.such.file.or.directory"))

    def test_fallback_plain_error(self):
        text = "unknown weird failure happened in subsystem xyz 1234"
        sig = canonical_signature(text)
        self.assertEqual(sig, "unknown.weird.failure.happened.in.subsystem.xyz")

    def test_idempotent_after_normalization(self):
        text1 = (
            "sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called "
            "at /home/user/app.py line 12"
        )
        text2 = (
            "sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called "
            "at /tmp/other.py line 99"
        )
        self.assertEqual(canonical_signature(text1), canonical_signature(text2))

    def test_truncates_to_500(self):
        text = "A" * 1000 + ": " + "B" * 1000
        sig = canonical_signature(text)
        self.assertLessEqual(len(sig), 500)


if __name__ == "__main__":
    unittest.main()
