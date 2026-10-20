import unittest
from datetime import datetime, timezone

from app.market.sessions import SessionClassifier


class TestMarketStatus(unittest.TestCase):
    def test_friday_after_close_counts_down_to_sunday_open(self):
        status = SessionClassifier.market_status(datetime(2026, 10, 9, 21, 30, tzinfo=timezone.utc))

        self.assertEqual(status["status"], "MARKET_CLOSED")
        self.assertEqual(status["opens_at_utc"], "2026-10-11T22:00:00+00:00")
        self.assertEqual(status["seconds_to_open"], 48 * 60 * 60 + 30 * 60)

    def test_saturday_counts_down_to_sunday_open(self):
        status = SessionClassifier.market_status(datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc))

        self.assertEqual(status["opens_at_utc"], "2026-10-11T22:00:00+00:00")
        self.assertEqual(status["seconds_to_open"], 34 * 60 * 60)

    def test_sunday_after_open_is_open_without_countdown(self):
        status = SessionClassifier.market_status(datetime(2026, 10, 11, 22, 0, tzinfo=timezone.utc))

        self.assertEqual(status, {"status": "OPEN", "opens_at_utc": None, "seconds_to_open": None})

    def test_regular_weekday_is_open_without_countdown(self):
        status = SessionClassifier.market_status(datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc))

        self.assertEqual(status, {"status": "OPEN", "opens_at_utc": None, "seconds_to_open": None})


if __name__ == "__main__":
    unittest.main()
