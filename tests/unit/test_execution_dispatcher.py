import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from app.core.types import Signal, Side, Tick, TradePlan
from app.execution.dispatcher import ExecutionDispatcher
from app.execution.engine import ExecOutcome


class TestExecutionDispatcher(unittest.TestCase):
    def setUp(self):
        self.signal = Signal(
            id="signal-1", strategy_id="micro_scalper", strategy_version="1.5.0",
            symbol="XAUUSD", created_at=datetime.now(timezone.utc), created_broker_time=1.0,
            valid_until_broker=2.0, snapshot_id="snapshot-1",
            plan=TradePlan(Side.BUY, 2000.0, 1999.0, 2002.0, {}, "setup-1"),
            regime="TREND_UP", session="LONDON", event_state="NORMAL",
        )
        self.tick = Tick(time_msc=1000, bid=2000.0, ask=2000.1)

    def test_routes_by_execution_category_and_strategy_with_category_fallback(self):
        generic = Mock()
        specific = Mock()
        generic.execute.return_value = ExecOutcome(None, "FILLED")
        specific.execute.return_value = ExecOutcome(None, "CANCELLED")
        dispatcher = ExecutionDispatcher({
            ("MT5", "*"): generic,
            ("MT5", "micro_scalper"): specific,
        })

        outcome = dispatcher.dispatch(
            "MT5", self.signal, self.tick, 1.0, Mock(), Mock(), "LONDON", "TREND_UP")

        self.assertEqual(outcome.status, "CANCELLED")
        specific.execute.assert_called_once()
        generic.execute.assert_not_called()

        self.signal.strategy_id = "pullback_v1"
        outcome = dispatcher.dispatch(
            "MT5", self.signal, self.tick, 1.0, Mock(), Mock(), "LONDON", "TREND_UP")
        self.assertEqual(outcome.status, "FILLED")
        generic.execute.assert_called_once()

    def test_unknown_execution_category_is_rejected_without_dispatch(self):
        handler = Mock()
        dispatcher = ExecutionDispatcher({("MT5", "*"): handler})

        outcome = dispatcher.dispatch(
            "BINANCE", self.signal, self.tick, 1.0, Mock(), Mock(), "LONDON", "TREND_UP")

        self.assertEqual(outcome.status, "REJECTED")
        self.assertEqual(outcome.reason, "UNKNOWN_EXECUTION_ROUTE:BINANCE:micro_scalper")
        handler.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
