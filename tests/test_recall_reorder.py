import unittest
from base import HookTestCase
import local_store


class RecallReorderTest(HookTestCase):
    def test_use_count_wins_over_recency(self):
        conn = self.get_conn()
        pid = self.write_project_bridge(conn)

        # Three cached pointers.
        for tid, title in [("A", "trace A"), ("B", "trace B"), ("C", "trace C")]:
            local_store.cache_trace_pointer(conn, tid, pid, title)

        # B used 3x, A 2x, C 1x.
        for _ in range(2):
            local_store.mark_trace_used_v2(conn, "A", pid)
        for _ in range(3):
            local_store.mark_trace_used_v2(conn, "B", pid)
        local_store.mark_trace_used_v2(conn, "C", pid)

        # Force C to be the most-recently-seen (last_seen_at is a float epoch),
        # so recency alone would rank it first.
        conn.execute(
            "UPDATE trace_cache SET last_seen_at = ? WHERE project_id = ? AND trace_id = ?",
            (9_999_999_999.0, pid, "C"),
        )
        conn.commit()

        rows = local_store.get_cached_traces(conn, pid, limit=10)
        order = [r["trace_id"] for r in rows]
        assert order == ["B", "A", "C"], order  # use_count DESC wins


if __name__ == "__main__":
    unittest.main()
