"""The account-value chart draws the range it is asked for, and the account is recorded once a minute, not once per
symbol core.

23 Oct 2026: "Last 7 days" showed about a day. The endpoint read the newest 20,000 snapshots and filtered them by
time afterwards, and with 13 symbol cores each writing the same account every minute, 20,000 rows reached back only
26 hours. Every longer range was silently the same day.

The endpoint is called directly rather than through TestClient, so these tests run without httpx installed.
"""
import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone

from starlette.requests import Request

from tests.helpers import started_core
from app.config import ConfigManager
from app.core.bridge import Bridge
from app.dashboard.api import EQUITY_POINTS, create_app
from app.storage.repo import MemoryRepo

NOW = datetime.now(timezone.utc)
CORES = 13


def seeded(hours: int) -> MemoryRepo:
    """`hours` of one-minute snapshots, each written by all 13 cores, oldest first (as the bot inserts them)."""
    repo = MemoryRepo()
    for m in range(hours * 60, -1, -1):
        at = NOW - timedelta(minutes=m)
        for _ in range(CORES):
            repo.insert("account_snapshots", {"created_at": at, "environment": "DEMO",
                                              "balance": 5000.0, "equity": 5000.0 + m / 100})
    return repo


def equity(repo, hours) -> list:
    app = create_app(Bridge(), repo, ConfigManager(), "tok")
    endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/api/equity")
    req = Request({"type": "http", "method": "GET", "path": "/api/equity",
                   "query_string": f"hours={hours}".encode(), "headers": []})
    return json.loads(asyncio.run(endpoint(req)).body)


class TestEquityWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = seeded(36)          # 28,000+ rows: the newest 20,000 cover only about 26 hours

    def test_a_week_reaches_the_oldest_snapshot_not_the_newest_20000_rows(self):
        pts = equity(self.repo, 168)
        span_h = (pts[-1]["t"] - pts[0]["t"]) / 3600
        self.assertGreater(span_h, 35.5, f"the chart covered {span_h:.1f} h of 36 h of history")

    def test_the_newest_snapshot_is_always_the_last_point(self):
        pts = equity(self.repo, 168)
        self.assertAlmostEqual(pts[-1]["t"], NOW.timestamp(), delta=1)
        self.assertEqual(pts[-1]["equity"], 5000.0)

    def test_points_are_in_time_order_and_bounded(self):
        for hours in (24, 168, 8760):
            pts = equity(self.repo, hours)
            self.assertLessEqual(len(pts), EQUITY_POINTS + 1)
            self.assertEqual([p["t"] for p in pts], sorted(p["t"] for p in pts))

    def test_a_long_range_over_short_history_keeps_its_detail(self):
        """Buckets are sized to the history that exists: a year over 36 hours is not 36 hours in six points."""
        self.assertGreater(len(equity(self.repo, 8760)), 1000)

    def test_24_hours_leaves_older_snapshots_out(self):
        pts = equity(self.repo, 24)
        self.assertGreaterEqual(pts[0]["t"], (NOW - timedelta(hours=24)).timestamp() - 1)
        self.assertGreater((pts[-1]["t"] - pts[0]["t"]) / 3600, 23.5)

    def test_no_snapshots_draws_nothing(self):
        self.assertEqual(equity(MemoryRepo(), 168), [])


class TestOneSnapshotPerMinute(unittest.TestCase):
    """All symbol cores share one account, so only the first portfolio core records it."""

    def setUp(self):
        self.core = started_core()[0]
        self.assertIsNotNone(self.core.account)
        self.tables = []
        enqueue = self.core.telemetry.enqueue
        self.core.telemetry.enqueue = lambda table, row: (self.tables.append(table), enqueue(table, row))

    def test_a_core_that_is_not_first_does_not_record_the_account(self):
        self.core.portfolio_cores = [object(), self.core]
        self.core._minute_tasks()
        self.assertNotIn("account_snapshots", self.tables)
        self.assertIn("system_health", self.tables, "per-core health is still recorded")

    def test_the_first_core_records_it(self):
        self.core.portfolio_cores = [self.core, object()]
        self.core._minute_tasks()
        self.assertEqual(self.tables.count("account_snapshots"), 1)

    def test_a_single_symbol_core_records_it(self):
        self.assertFalse(hasattr(self.core, "portfolio_cores"))
        self.core._minute_tasks()
        self.assertEqual(self.tables.count("account_snapshots"), 1)


if __name__ == "__main__":
    unittest.main()
