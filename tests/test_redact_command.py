"""redact_command must apply the full SECRET_PATTERNS set.

Regression: commands were stored via redact_command, which only stripped
VAR=val assignments and -p/--password flags. Header-form secrets carried
by curl (`-H "Authorization: Bearer …"`, `-H "x-api-key: …"`) and
connection-string credentials leaked into local.db and suggested_solution
text. redact_command must run the same SECRET_PATTERNS as redact_text.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import redact  # noqa: E402


class TestRedactCommand(unittest.TestCase):
    def test_bearer_authorization_header_redacted(self):
        out = redact.redact_command(
            'curl -H "Authorization: Bearer sk-abc123XYZtoken" https://api.x')
        self.assertNotIn("sk-abc123XYZtoken", out)
        self.assertIn("[REDACTED]", out)

    def test_x_api_key_header_redacted(self):
        out = redact.redact_command(
            'curl -H "x-api-key: SUPERSECRETVALUE99" https://api.x')
        self.assertNotIn("SUPERSECRETVALUE99", out)
        self.assertIn("[REDACTED]", out)

    def test_connection_string_credentials_redacted(self):
        out = redact.redact_command(
            'psql postgres://dbuser:hunter2pw@db.internal:5432/app')
        self.assertNotIn("hunter2pw", out)
        self.assertIn("[REDACTED]", out)

    def test_aws_key_redacted(self):
        out = redact.redact_command('aws configure set AKIAIOSFODNN7EXAMPLE')
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)

    def test_password_flag_still_redacted(self):
        out = redact.redact_command('mysql -u root -p mypassword123 db')
        self.assertNotIn("mypassword123", out)

    def test_env_var_assignment_redacted(self):
        out = redact.redact_command('TOKEN=ghp_abcdef1234567890 gh api /user')
        self.assertNotIn("ghp_abcdef1234567890", out)
        self.assertIn("[REDACTED]", out)

    def test_empty_passthrough(self):
        self.assertEqual(redact.redact_command(""), "")

    def test_benign_command_untouched(self):
        cmd = "git commit --amend"
        self.assertEqual(redact.redact_command(cmd), cmd)


if __name__ == "__main__":
    unittest.main()
