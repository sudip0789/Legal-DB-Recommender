import unittest

from core.rate_limit import check_and_record


class RateLimitTest(unittest.TestCase):
    def test_allows_up_to_minute_limit_then_blocks(self):
        times = []
        # 10 requests inside the same minute (t = 0..9) are all allowed.
        for t in range(10):
            allowed, limit = check_and_record(times, float(t), per_minute=10)
            self.assertTrue(allowed, f"request {t} should be allowed")
            self.assertIsNone(limit)

        # The 11th, still inside the 60s window, is blocked on the minute rule.
        allowed, limit = check_and_record(times, 9.0, per_minute=10)
        self.assertFalse(allowed)
        self.assertEqual(limit, "minute")
        # Blocked requests are not recorded.
        self.assertEqual(len(times), 10)

    def test_minute_window_frees_up_after_60s(self):
        times = []
        for t in range(10):
            check_and_record(times, float(t), per_minute=10)

        # At t=59 still blocked (t=0 hasn't aged out of the 60s window yet).
        allowed, _ = check_and_record(times, 59.0, per_minute=10)
        self.assertFalse(allowed)

        # At t=61 the t=0 entry (and only it) has left the minute window, so
        # in_minute drops to 9 and the request is allowed.
        allowed, limit = check_and_record(times, 61.0, per_minute=10)
        self.assertTrue(allowed)
        self.assertIsNone(limit)

    def test_hour_limit_blocks_101st(self):
        times = []
        # 100 requests spread across the hour (well under 10/min): every 30s.
        for i in range(100):
            allowed, limit = check_and_record(
                times, i * 30.0, per_minute=10, per_hour=100
            )
            self.assertTrue(allowed, f"request {i} should be allowed")

        # 101st request, still within the hour of the earliest, hits the hour cap.
        allowed, limit = check_and_record(
            times, 100 * 30.0, per_minute=10, per_hour=100
        )
        self.assertFalse(allowed)
        self.assertEqual(limit, "hour")

    def test_entries_older_than_hour_are_pruned(self):
        times = [0.0]  # a request an hour+ ago
        # 3601s later the old entry has aged out and must not count.
        allowed, limit = check_and_record(times, 3601.0, per_minute=10, per_hour=100)
        self.assertTrue(allowed)
        self.assertIsNone(limit)
        # Old entry pruned; only the new one remains.
        self.assertEqual(times, [3601.0])


if __name__ == "__main__":
    unittest.main()
