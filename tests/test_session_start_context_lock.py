import unittest
import urllib.request
from unittest import mock

import session_start


class SessionStartContextLockTest(unittest.TestCase):
    """Lock #12: search_commontrace transmits structural context to the API.

    The request body is the skill's only structural signal to the server;
    these assertions guard that the language tag, query, and (when present)
    context keep being sent.
    """

    def _capture_body(self, **call_kwargs):
        captured = {}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"results": []}'

        def fake_urlopen(req, *a, **k):
            captured["data"] = req.data
            return FakeResp()

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            session_start.search_commontrace(**call_kwargs)
        return captured.get("data", b"")

    def test_language_tag_and_query_always_sent(self):
        body = self._capture_body(query="boot loop", language="python", api_key="k")
        assert b'"python"' in body
        assert b'boot loop' in body

    def test_context_included_when_present(self):
        body = self._capture_body(
            query="x", language="python", api_key="k",
            context={"os": "linux"},
        )
        assert b'"context"' in body
        assert b'"linux"' in body

    def test_context_omitted_when_absent(self):
        body = self._capture_body(query="x", language="python", api_key="k")
        assert b'"context"' not in body


if __name__ == "__main__":
    unittest.main()
