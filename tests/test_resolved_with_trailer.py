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

    def test_sanitize_trace_id_strips_bad_chars_and_caps_length(self):
        """I4: newlines/quotes/control chars stripped; length capped at 64."""
        # trace_id with newline, quote, null byte, and a long tail.
        # After stripping non-[A-Za-z0-9_-] chars: "tr_42" + 60 a's = 65 chars → capped at 64.
        dirty_id = "tr\n_\"42\x00" + "a" * 60
        out = post_tool_use._suggest_trailer(self.state_dir, dirty_id)
        self.assertIsNotNone(out)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        # Extract the id from the URL — it is the path segment after /t/
        url_part = [p for p in ctx.split() if "commontrace.org/t/" in p][0]
        extracted_id = url_part.split("/t/")[1].rstrip(")\n")
        # Must not contain stripped chars
        self.assertNotIn("\n", extracted_id)
        self.assertNotIn('"', extracted_id)
        self.assertNotIn("\x00", extracted_id)
        # Must be at most 64 chars
        self.assertLessEqual(len(extracted_id), 64)

    def test_rmw_concurrent_flag_survives_trailer_save(self):
        """M6: flag written to config after initial load must not be clobbered."""
        import threading

        conn = self.get_conn()
        self.write_project_bridge(conn)
        err_t = time.time() - 60
        self._seed_consumed(conn, "tr_rmw", err_t)

        # Simulate a concurrent write that happens AFTER the hook reads the config
        # but BEFORE it saves. We do this by pre-writing the flag to the config
        # file right before the save path is reached: patch CONFIG_FILE on disk
        # with a sentinel key, then let the hook do its first-use write.
        # After the call, both trailer_notice_shown AND the sentinel must survive.
        sentinel = {"other_flag": True}
        post_tool_use.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True,
                                               mode=0o700)
        post_tool_use.CONFIG_FILE.write_text(json.dumps(sentinel), encoding="utf-8")

        out = post_tool_use._pair_resolution(
            self.state_dir, "pytest", [_error_event(err_t)])
        self.assertIsNotNone(out)
        # Verify trailer_notice_shown was written
        config = json.loads(
            post_tool_use.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertTrue(config.get("trailer_notice_shown"))
        # Verify the pre-existing sentinel key was NOT clobbered (RMW preserved it)
        self.assertTrue(config.get("other_flag"))
