import unittest
from datetime import datetime, timedelta, timezone

from app.dashboard.api import summarize_candidate_evaluations


class TestCandidateAnalytics(unittest.TestCase):
    def test_groups_outcomes_by_symbol_strategy_stage_gate_and_reason(self):
        now = datetime.now(timezone.utc)
        rows = [
            {"created_at": now, "symbol": "XAUUSDm", "strategy_id": "micro_scalper",
             "stage": "STRATEGY", "outcome": "NO_SETUP", "reason": "NO_CANDLESTICK_TRIGGER"},
            {"created_at": now, "symbol": "XAUUSDm", "strategy_id": "micro_scalper",
             "stage": "STRATEGY", "outcome": "NO_SETUP", "reason": "NO_CANDLESTICK_TRIGGER"},
            {"created_at": now, "symbol": "BTCUSDm", "strategy_id": "pullback_v1",
             "stage": "RISK", "gate": "RISK", "outcome": "REJECTED", "reason": "RISK_STATE_NO_NEW_TRADES"},
            {"created_at": now - timedelta(days=2), "symbol": "XAUUSDm",
             "strategy_id": "micro_scalper", "stage": "STRATEGY",
             "outcome": "NO_SETUP", "reason": "OLD"},
        ]

        result = summarize_candidate_evaluations(rows, now.timestamp() - 86400)

        self.assertEqual(result["evaluations"], 3)
        self.assertEqual(result["groups"][0], {
            "symbol": "XAUUSDm", "strategy_id": "micro_scalper", "stage": "STRATEGY",
            "gate": "?", "reason": "NO_CANDLESTICK_TRIGGER", "count": 2,
            "outcomes": {"NO_SETUP": 2},
        })


if __name__ == "__main__":
    unittest.main()
