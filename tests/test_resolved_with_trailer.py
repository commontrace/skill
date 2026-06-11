"""Resolved-with trailer: disclosure suggestion after commons-assisted fixes."""

import json
import time

from base import HookTestCase

import post_tool_use


def _error_event(t, sig="E: ModuleNotFoundError boom", command="pytest"):
    return {"source": "bash", "sig": sig, "command": command, "t": t}


class TestSuggestTrailer(HookTestCase):
    def _seed_consumed(self, conn, trace_id, base_t):
        conn.execute(
            "INSERT INTO trigger_feedback (session_id, trigger_name, "
            "triggered_at, trace_consumed_id, consumed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.state_dir.name, "error_search", base_t - 5, trace_id,
             base_t + 10))
        conn.commit()

    def test_server_trace_fires_trailer_with_first_use_notice(self):
        conn = self.get_conn()
        self.write_project_bridge(conn)
        err_t = time.time() - 60
        self._seed_consumed(conn, "tr_42", err_t)
        out = post_tool_use._pair_resolution(
            self.state_dir, "pytest", [_error_event(err_t)])
        self.assertIsNotNone(out)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Resolved-with: CommonTrace "
                      "https://commontrace.org/t/tr_42", ctx)
        self.assertIn("Citation, not co-authorship", ctx)
        self.assertIn("resolved_with_trailer", ctx)  # opt-out, first use
        config = json.loads(
            post_tool_use.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertTrue(config["trailer_notice_shown"])

    def test_once_per_session_per_trace(self):
        conn = self.get_conn()
        self.write_project_bridge(conn)
        err_t = time.time() - 60
        self._seed_consumed(conn, "tr_42", err_t)
        first = post_tool_use._pair_resolution(
            self.state_dir, "pytest", [_error_event(err_t)])
        self.assertIsNotNone(first)
        second = post_tool_use._pair_resolution(
            self.state_dir, "pytest", [_error_event(err_t)])
        self.assertIsNone(second)

    def test_opt_out_line_shown_only_first_time_ever(self):
        conn = self.get_conn()
        self.write_project_bridge(conn)
        err_t = time.time() - 60
        self._seed_consumed(conn, "tr_1", err_t)
        first = post_tool_use._pair_resolution(
            self.state_dir, "pytest", [_error_event(err_t, sig="E: one")])
        self.assertIn("One-line opt-out",
                      first["hookSpecificOutput"]["additionalContext"])
        # later consumption of a different trace wins the latest-consumed
        # lookup; the notice must not repeat
        self._seed_consumed(conn, "tr_2", err_t + 20)
        second = post_tool_use._pair_resolution(
            self.state_dir, "pytest", [_error_event(err_t, sig="E: two")])
        ctx = second["hookSpecificOutput"]["additionalContext"]
        self.assertIn("tr_2", ctx)
        self.assertNotIn("One-line opt-out", ctx)

    def test_local_trace_never_fires(self):
        conn = self.get_conn()
        self.write_project_bridge(conn)
        err_t = time.time() - 60
        self._seed_consumed(conn, "local:abc123", err_t)
        out = post_tool_use._pair_resolution(
            self.state_dir, "pytest", [_error_event(err_t)])
        self.assertIsNone(out)

    def test_config_opt_out_disables_trailer(self):
        post_tool_use.CONFIG_FILE.write_text(
            json.dumps({"resolved_with_trailer": False}), encoding="utf-8")
        conn = self.get_conn()
        self.write_project_bridge(conn)
        err_t = time.time() - 60
        self._seed_consumed(conn, "tr_42", err_t)
        out = post_tool_use._pair_resolution(
            self.state_dir, "pytest", [_error_event(err_t)])
        self.assertIsNone(out)
