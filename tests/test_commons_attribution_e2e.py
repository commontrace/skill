"""Commons attribution: the shared knowledge base gets credit for its fixes.

``local:`` markers prove *this project's own past fix* paid off. They say
nothing about the commons. The only path that ever recorded a commons
trace was ``get_trace`` (the MCP tool), and since ``/trace`` and
``/recall`` went direct-HTTP essentially nothing calls it — so a trace the
hook itself retrieved, formatted into the agent's context, and that then
fixed the error was structurally invisible.

Like tests/test_consumption_e2e.py, these drive the REAL hook entry point
(``post_tool_use.main()`` over stdin) with the payload shape Claude Code
actually sends. Calling the recorders by hand is exactly how the last
version of this bug passed its tests while production reported zero.
"""

import contextlib
import io
import json
import sys
import unittest
from unittest import mock

from tests.base import HookTestCase, append_event, read_events

import local_store  # noqa: E402
import post_tool_use  # noqa: E402
import session_state  # noqa: E402
import stop  # noqa: E402


TOP = "tr-commons-top"
SECOND = "tr-commons-second"

COMMONS_RESULTS = [
    {"id": TOP, "title": "ImportError on a renamed package",
     "solution_text": "install foo-bar, import it as foo",
     "contributor_name": "another agent"},
    {"id": SECOND, "title": "Unrelated but similar",
     "solution_text": "check your PYTHONPATH"},
]


def bash_payload(session_id, command, stdout="", stderr=""):
    """The exact PostToolUse:Bash payload Claude Code emits."""
    return {
        "session_id": session_id,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {
            "stdout": stdout,
            "stderr": stderr,
            "interrupted": False,
            "isImage": False,
            "noOutputExpected": False,
        },
    }


def edit_payload(session_id, file_path):
    return {
        "session_id": session_id,
        "tool_name": "Edit",
        "tool_input": {
            "file_path": file_path,
            "old_string": "import foo",
            "new_string": "import foo_bar as foo",
        },
        "tool_response": {"filePath": file_path, "userModified": False},
    }


def get_trace_payload(session_id, trace_id):
    return {
        "session_id": session_id,
        "tool_name": "mcp__commontrace__get_trace",
        "tool_input": {"trace_id": trace_id},
        "tool_response": {"title": "Unrelated but similar"},
    }


class CommonsAttributionTestCase(HookTestCase):
    """Hook-level harness: state dir in tmp, search stubbed, no network."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(
            session_state, "STATE_ROOT", self.tmp_path / "sessions")
        patcher.start()
        self.addCleanup(patcher.stop)

        key_patcher = mock.patch.object(
            post_tool_use, "load_api_key", return_value="test-key")
        key_patcher.start()
        self.addCleanup(key_patcher.stop)

        search_patcher = mock.patch.object(
            post_tool_use, "search_commontrace",
            return_value=list(COMMONS_RESULTS))
        self.search = search_patcher.start()
        self.addCleanup(search_patcher.stop)

    def run_hook(self, payload):
        """Invoke the hook exactly as Claude Code does: JSON on stdin."""
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            with contextlib.redirect_stdout(out):
                post_tool_use.main()
        raw = out.getvalue().strip()
        return json.loads(raw) if raw else None

    def state_dir_for(self, session_id, project_id=None):
        d = session_state.get_state_dir({"session_id": session_id})
        if project_id is not None:
            (d / "project_id").write_text(str(project_id), encoding="utf-8")
        return d

    def consumed_ids(self, conn, session_id):
        return [r["trace_consumed_id"] for r in conn.execute(
            "SELECT trace_consumed_id FROM trigger_feedback "
            "WHERE session_id = ? AND trace_consumed_id IS NOT NULL "
            "ORDER BY consumed_at", (session_id,)).fetchall()]

    def signature_trace_id(self, conn):
        row = conn.execute(
            "SELECT trace_id FROM error_signatures "
            "WHERE resolved_at IS NOT NULL").fetchone()
        return row["trace_id"] if row else None


class TestCommonsAttributionEndToEnd(CommonsAttributionTestCase):
    def test_surfaced_commons_trace_is_credited_when_the_fix_lands(self):
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/test-project")
        sid = "sess-commons-one"
        d = self.state_dir_for(sid, pid)

        # ── 1. A command fails → the hook searches and injects real traces ──
        injected = self.run_hook(bash_payload(
            sid, "pytest tests/", stderr="ImportError: No module named foo"))
        self.assertIsNotNone(injected, "commons traces must be injected")
        self.assertIn(TOP, injected["hookSpecificOutput"]["additionalContext"])

        surfaced = read_events(d, "surfaced.jsonl")
        self.assertEqual(len(surfaced), 1,
                         "the ids put in front of the agent must be kept — "
                         "throwing them away is what made the commons "
                         "structurally unobservable")
        self.assertTrue(surfaced[0].get("sig"),
                        "surfaced traces must be keyed by error signature")
        self.assertEqual(surfaced[0]["trace_ids"][0], TOP)
        self.assertIn("t", surfaced[0], "surfacing needs a timestamp")

        # ── 2. Agent applies the fix, command now succeeds ──
        self.run_hook(edit_payload(sid, "/repo/foo.py"))
        out = self.run_hook(bash_payload(sid, "pytest tests/", stdout="3 passed"))

        self.assertEqual(
            self.consumed_ids(conn, sid), [TOP],
            "the commons trace surfaced for this signature must be credited "
            "when the command it was surfaced for starts passing")
        self.assertEqual(
            self.signature_trace_id(conn), TOP,
            "the resolved signature must carry the commons trace id, not NULL")

        # ── 3. The disclosure trailer fires — for real, for the right trace ──
        self.assertIsNotNone(out, "Resolved-with trailer must be suggested")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"Resolved-with: CommonTrace https://commontrace.org/t/{TOP}",
                      ctx)
        self.assertNotIn(SECOND, ctx)
        self.assertIn("Citation, not co-authorship", ctx)

        # ── 4. What stop.py reports: commons consumption is non-zero ──
        counters = stop._session_counters(conn, d, pid)
        self.assertGreater(counters["traces_consumed"], 0)
        self.assertGreater(counters["resolutions_assisted"], 0)

    def test_no_surfacing_means_no_attribution(self):
        """An empty search result set must never manufacture a consumption."""
        self.search.return_value = []
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/test-project")
        sid = "sess-commons-empty"
        d = self.state_dir_for(sid, pid)

        self.run_hook(bash_payload(
            sid, "pytest tests/", stderr="ImportError: No module named foo"))
        self.assertEqual(read_events(d, "surfaced.jsonl"), [])
        self.run_hook(edit_payload(sid, "/repo/foo.py"))
        out = self.run_hook(bash_payload(sid, "pytest tests/", stdout="ok"))

        self.assertEqual(self.consumed_ids(conn, sid), [])
        self.assertIsNone(self.signature_trace_id(conn))
        self.assertIsNone(out, "no commons trace → no disclosure trailer")

    def test_different_signature_is_not_credited(self):
        """Surfacing is keyed by signature — a different failure gets nothing."""
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/test-project")
        sid = "sess-commons-other-sig"
        d = self.state_dir_for(sid, pid)

        self.run_hook(bash_payload(
            sid, "pytest tests/", stderr="ImportError: No module named foo"))
        self.assertEqual(len(read_events(d, "surfaced.jsonl")), 1)

        # A different failure of the same command, then success. The traces
        # surfaced for the FIRST error say nothing about this one.
        self.run_hook(bash_payload(
            sid, "pytest tests/", stderr="AssertionError: totals differ"))
        self.run_hook(edit_payload(sid, "/repo/foo.py"))
        self.run_hook(bash_payload(sid, "pytest tests/", stdout="3 passed"))

        self.assertEqual(self.consumed_ids(conn, sid), [])
        row = conn.execute(
            "SELECT trace_id FROM error_signatures "
            "WHERE resolved_at IS NOT NULL").fetchone()
        self.assertIsNone(row["trace_id"])

    def test_get_trace_evidence_is_not_overridden_by_the_rank_one_guess(self):
        """An actual get_trace call names the trace; the heuristic must yield."""
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/test-project")
        sid = "sess-commons-exact"
        self.state_dir_for(sid, pid)

        self.run_hook(bash_payload(
            sid, "pytest tests/", stderr="ImportError: No module named foo"))
        # Agent reads the SECOND result, not the top-ranked one.
        self.run_hook(get_trace_payload(sid, SECOND))
        self.run_hook(edit_payload(sid, "/repo/foo.py"))
        self.run_hook(bash_payload(sid, "pytest tests/", stdout="3 passed"))

        self.assertEqual(self.consumed_ids(conn, sid), [SECOND])
        self.assertEqual(self.signature_trace_id(conn), SECOND)

    def test_recurrence_of_the_same_signature_credits_the_local_cache(self):
        """Second time around it is the local cache paying off, not the commons.

        The commons trace is credited once, for the episode it was surfaced
        in. When the same signature recurs and the locally cached fix is
        injected, the assist belongs to ``local:`` — crediting the commons
        again would double-count one act of help.
        """
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/test-project")
        sid = "sess-commons-recur"
        self.state_dir_for(sid, pid)

        self.run_hook(bash_payload(
            sid, "pytest tests/", stderr="ImportError: No module named foo"))
        self.run_hook(edit_payload(sid, "/repo/foo.py"))
        self.run_hook(bash_payload(sid, "pytest tests/", stdout="3 passed"))
        self.assertEqual(self.consumed_ids(conn, sid), [TOP])

        # Same error again: the resolved signature recurs → local injection.
        injected = self.run_hook(bash_payload(
            sid, "pytest tests/", stderr="ImportError: No module named foo"))
        self.assertIsNotNone(injected)
        self.assertIn("local CommonTrace history",
                      injected["hookSpecificOutput"]["additionalContext"])
        self.run_hook(edit_payload(sid, "/repo/foo.py"))
        self.run_hook(bash_payload(sid, "pytest tests/", stdout="3 passed"))

        ids = self.consumed_ids(conn, sid)
        self.assertEqual(len(ids), 2, "one commons credit, one local credit")
        self.assertEqual(ids[0], TOP)
        self.assertTrue(ids[1].startswith("local:"))
        self.assertEqual(self.signature_trace_id(conn), TOP,
                         "the commons id already attached must not be lost")


class TestCommonsOutranksLocalMarker(HookTestCase):
    """Precedence lock: a real commons id must always beat a local: marker."""

    def test_commons_id_wins_the_resolution_attribution(self):
        conn = self.get_conn()
        pid = self.write_project_bridge(conn)
        local_store.record_error_signature(conn, pid, "sig-x")
        append_event(self.state_dir, "errors.jsonl", {
            "source": "bash", "command": "pytest tests/", "sig": "sig-x",
            "t": 100.0})
        # Both claims apply in the same window: the fix was injected from the
        # local cache AND a commons trace was surfaced for this signature.
        append_event(self.state_dir, "recurrence_injected.jsonl",
                     {"sig": "sig-x", "t": 101.0})
        append_event(self.state_dir, "surfaced.jsonl",
                     {"sig": "sig-x", "trace_ids": [TOP], "t": 101.0})
        sid = self.state_dir.name
        local_store.record_trigger(conn, sid, "bash_error")
        local_store.record_trigger(conn, sid, "error_recurrence")

        out = post_tool_use._pair_resolution(
            self.state_dir, "pytest tests/",
            read_events(self.state_dir, "errors.jsonl"))

        row = conn.execute(
            "SELECT trace_id FROM error_signatures "
            "WHERE signature = 'sig-x'").fetchone()
        self.assertEqual(row["trace_id"], TOP,
                         "commons id must outrank the local: marker")
        self.assertIsNotNone(out, "commons attribution must fire the trailer")
        self.assertIn(TOP, out["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()
