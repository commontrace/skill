"""Session counters (v0.5.2): assisted-resolution telemetry + provenance.

Counter tests drive stop._session_counters — per-session aggregates sent
to /api/v1/telemetry/triggers (spec §4.3 north-star). Counters are scoped
by trigger_feedback.session_id = state_dir.name (NOT the sessions table).
Formatter tests pin contributor provenance display (spec §4.2) with
sanitization — display names are user-supplied (injection surface).
"""

import time
import unittest

from tests.base import HookTestCase, append_event, local_store, post_tool_use
import session_start
import stop


class TestSessionCounters(HookTestCase):
    def _write_resolutions(self, n, t0=None):
        t0 = time.time() if t0 is None else t0
        for i in range(n):
            append_event(self.state_dir, "resolutions.jsonl",
                         {"source": "bash", "t": t0 + i})

    def test_empty_session_returns_zeros(self):
        conn = self.get_conn()
        self.assertEqual(
            stop._session_counters(conn, self.state_dir, None),
            {"searches_fired": 0, "traces_consumed": 0,
             "resolutions_total": 0, "resolutions_assisted": 0})

    def test_fired_and_consumed_scoped_to_this_session(self):
        conn = self.get_conn()
        sid = self.state_dir.name
        for _ in range(3):
            local_store.record_trigger(conn, sid, "bash_error")
        local_store.record_trace_consumed(conn, sid, "trace-1")
        local_store.record_trigger(conn, "other-session", "bash_error")
        local_store.record_trigger(conn, "other-session", "bash_error")
        counters = stop._session_counters(conn, self.state_dir, None)
        self.assertEqual(counters["searches_fired"], 3)
        self.assertEqual(counters["traces_consumed"], 1)

    def test_assisted_counts_attributed_resolutions_in_window(self):
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/test-project")
        now = time.time()
        # Commons-attributed + local-attributed land in trace_id alike
        local_store.record_error_signature(conn, pid, "sig-commons")
        local_store.record_resolution(conn, pid, "sig-commons",
                                      trace_id="abc-123")
        local_store.record_error_signature(conn, pid, "sig-local")
        local_store.record_resolution(conn, pid, "sig-local",
                                      trace_id="local:deadbeef")
        # Unattributed resolution: trace_id stays NULL
        local_store.record_error_signature(conn, pid, "sig-unattributed")
        local_store.record_resolution(conn, pid, "sig-unattributed")
        # Attributed but resolved long before this session's window
        local_store.record_error_signature(conn, pid, "sig-old")
        local_store.record_resolution(conn, pid, "sig-old",
                                      trace_id="old-999")
        conn.execute(
            "UPDATE error_signatures SET resolved_at = ? WHERE signature = ?",
            (now - 99999, "sig-old"))
        conn.commit()
        self._write_resolutions(3, t0=now)
        counters = stop._session_counters(conn, self.state_dir, pid)
        self.assertEqual(counters["resolutions_total"], 3)
        self.assertEqual(counters["resolutions_assisted"], 2)

    def test_assisted_capped_at_resolutions_total(self):
        conn = self.get_conn()
        pid = local_store.ensure_project(conn, "/test-project")
        for i in range(3):
            sig = f"sig-{i}"
            local_store.record_error_signature(conn, pid, sig)
            local_store.record_resolution(conn, pid, sig,
                                          trace_id=f"trace-{i}")
        self._write_resolutions(1)
        counters = stop._session_counters(conn, self.state_dir, pid)
        self.assertEqual(counters["resolutions_total"], 1)
        self.assertEqual(counters["resolutions_assisted"], 1)

    def test_no_project_id_keeps_totals_but_zero_assisted(self):
        conn = self.get_conn()
        self._write_resolutions(2)
        counters = stop._session_counters(conn, self.state_dir, None)
        self.assertEqual(counters["resolutions_total"], 2)
        self.assertEqual(counters["resolutions_assisted"], 0)


class TestProvenanceFormatting(unittest.TestCase):
    def test_search_results_show_sanitized_contributor(self):
        out = post_tool_use.format_results([{
            "title": "T", "solution_text": "S", "id": "tid-1",
            "contributor_name": "alice<script>!",
        }])
        self.assertIn("by alicescript", out)
        self.assertNotIn("<script>", out)

    def test_session_start_result_shows_contributor(self):
        out = session_start.format_result({
            "title": "T", "context_text": "c", "solution_text": "s",
            "id": "tid-2", "contributor_name": "bob",
        })
        self.assertIn("by bob", out)

    def test_missing_contributor_name_omits_by(self):
        self.assertNotIn(" by ", post_tool_use.format_results(
            [{"title": "T", "solution_text": "S", "id": "tid-3"}]))
        self.assertNotIn(" by ", session_start.format_result(
            {"title": "T", "id": "tid-4"}))


if __name__ == "__main__":
    unittest.main()
