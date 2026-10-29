import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np

from app.features.frame import FeatureFrame
from app.risk.correlation import rolling_h1_correlation
from app.storage.repo import MemoryRepo, PostgresRepo


class RecordingCursor:
    def __init__(self, calls):
        self.calls = calls
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, _params=None):
        self.calls.append(sql)
        self.rows = []

    def fetchall(self):
        return self.rows


class RecordingConnection:
    def __init__(self):
        self.calls = []
        self.closed = False

    def cursor(self):
        return RecordingCursor(self.calls)

    def rollback(self):
        self.calls.append("ROLLBACK")


class TestPortfolioRiskReservations(unittest.TestCase):
    def reserve(self, repo, signal_id, symbol="XAUUSD", risk=3.0, **overrides):
        trade_id = str(uuid.uuid4())
        repo.insert("trades", {"id": trade_id, "signal_id": signal_id, "symbol": symbol,
                               "state": "APPROVED", "initial_risk_money": risk})
        args = {
            "account_key": "123:demo:DEMO",
            "signal_id": signal_id,
            "trade_id": trade_id,
            "symbol": symbol,
            "risk_money": risk,
            "max_total_risk": 10.0,
            "max_correlated_risk": 5.0,
            "correlation_threshold": 0.7,
            "correlations": {},
            "external_risk_by_symbol": {},
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=60),
        }
        args.update(overrides)
        return repo.reserve_portfolio_risk(**args)

    def test_concurrent_reservations_cannot_exceed_correlated_budget(self):
        repo = MemoryRepo()
        barrier = threading.Barrier(2)

        def submit():
            barrier.wait()
            return self.reserve(repo, str(uuid.uuid4()))

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))

        self.assertEqual(results.count(None), 1)
        self.assertEqual(results.count("CORRELATED_OPEN_RISK_LIMIT"), 1)
        self.assertEqual(len(repo.select("risk_reservations", {"state": "RESERVED"})), 1)

    def test_insufficient_correlation_history_counts_as_correlated_exposure(self):
        repo = MemoryRepo()
        self.assertIsNone(self.reserve(repo, str(uuid.uuid4()), symbol="EURUSD", risk=1.0))

        # unmeasurable, so assumed fully correlated: 1 + 1 fits the correlated cap of 5 ...
        self.assertIsNone(self.reserve(repo, str(uuid.uuid4()), symbol="GBPUSD", risk=1.0,
                                       correlations={"EURUSD": None}))
        # ... and 2 already in the group + 4 more does not
        result = self.reserve(repo, str(uuid.uuid4()), symbol="AAPL", risk=4.0,
                              correlations={"EURUSD": None, "GBPUSD": None})
        self.assertEqual(result, "CORRELATED_OPEN_RISK_LIMIT")

    def test_uncorrelated_exposures_still_obey_total_portfolio_cap(self):
        repo = MemoryRepo()
        self.assertIsNone(self.reserve(
            repo, str(uuid.uuid4()), symbol="EURUSD", risk=6.0,
            max_correlated_risk=10.0))

        result = self.reserve(
            repo, str(uuid.uuid4()), symbol="GBPUSD", risk=5.0,
            max_correlated_risk=10.0, correlations={"EURUSD": 0.2})

        self.assertEqual(result, "PORTFOLIO_OPEN_RISK_LIMIT")

    def test_active_reservations_extend_and_terminal_reservations_release(self):
        repo = MemoryRepo()
        now = datetime.now(timezone.utc)
        active_id, terminal_id = str(uuid.uuid4()), str(uuid.uuid4())
        for trade_id, state in ((active_id, "OPEN"), (terminal_id, "CLOSED")):
            repo.insert("trades", {"id": trade_id, "signal_id": str(uuid.uuid4()),
                                   "symbol": "XAUUSD", "state": state, "initial_risk_money": 1.0})
            repo.insert("risk_reservations", {
                "id": str(uuid.uuid4()), "account_key": "123:demo:DEMO",
                "signal_id": str(uuid.uuid4()), "trade_id": trade_id, "symbol": "XAUUSD",
                "risk_money": 1.0, "state": "RESERVED", "expires_at": now - timedelta(seconds=1),
            })

        changed = repo.recover_portfolio_risk_reservations(
            "123:demo:DEMO", now, timedelta(seconds=60))
        reservations = repo.select("risk_reservations")

        self.assertEqual(changed, 2)
        self.assertEqual({row["state"] for row in reservations}, {"RESERVED", "RELEASED"})
        active_reservation = next(row for row in reservations if row["state"] == "RESERVED")
        self.assertGreater(active_reservation["expires_at"], now)

    def test_expired_orphan_reservation_is_expired(self):
        repo = MemoryRepo()
        now = datetime.now(timezone.utc)
        repo.insert("risk_reservations", {
            "id": str(uuid.uuid4()), "account_key": "123:demo:DEMO",
            "signal_id": str(uuid.uuid4()), "trade_id": str(uuid.uuid4()), "symbol": "XAUUSD",
            "risk_money": 1.0, "state": "RESERVED", "expires_at": now - timedelta(seconds=1),
        })

        changed = repo.recover_portfolio_risk_reservations(
            "123:demo:DEMO", now, timedelta(seconds=60))

        self.assertEqual(changed, 1)
        self.assertEqual(repo.select("risk_reservations")[0]["state"], "EXPIRED")

    def test_rolling_h1_correlation_uses_synchronized_hourly_returns(self):
        times = np.arange(401, dtype=np.int64) * 3600
        returns = np.where(np.arange(400) % 2, -0.01, 0.01)
        closes = np.concatenate(([100.0], 100.0 * np.cumprod(1.0 + returns)))
        a = FeatureFrame(times, closes, closes, closes, closes, timeframe="H1")
        b = FeatureFrame(times, closes, closes, closes, closes, timeframe="H1")

        correlation = rolling_h1_correlation(a, b)

        self.assertIsNotNone(correlation)
        assert correlation is not None
        self.assertAlmostEqual(correlation, 1.0)

    def test_rolling_h1_correlation_rejects_insufficient_history(self):
        times = np.arange(400, dtype=np.int64) * 3600
        closes = np.linspace(100.0, 110.0, len(times))
        a = FeatureFrame(times, closes, closes, closes, closes, timeframe="H1")
        b = FeatureFrame(times, closes, closes, closes, closes, timeframe="H1")

        self.assertIsNone(rolling_h1_correlation(a, b))

    def test_postgres_reservation_locks_before_read_and_commits(self):
        connection = RecordingConnection()
        repo = PostgresRepo("unused", connect=lambda: connection)

        reason = repo.reserve_portfolio_risk(
            account_key="123:demo:DEMO", signal_id=str(uuid.uuid4()), trade_id=str(uuid.uuid4()),
            symbol="XAUUSD", risk_money=1.0, max_total_risk=10.0, max_correlated_risk=5.0,
            correlation_threshold=0.7, correlations={}, external_risk_by_symbol={},
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )

        lock_index = next(i for i, statement in enumerate(connection.calls)
                          if "pg_advisory_xact_lock" in statement)
        read_index = next(i for i, statement in enumerate(connection.calls)
                          if statement.startswith("SELECT id, signal_id, symbol"))
        self.assertIsNone(reason)
        self.assertLess(lock_index, read_index)
        self.assertEqual(connection.calls[0], "BEGIN")
        self.assertEqual(connection.calls[-1], "COMMIT")


if __name__ == "__main__":
    unittest.main()
