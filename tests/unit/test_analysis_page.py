import unittest
from datetime import datetime, timezone

from starlette.testclient import TestClient

from app.config import ConfigManager
from app.core.bridge import Bridge
from app.dashboard import analysis
from app.dashboard.api import create_app
from app.storage.repo import MemoryRepo

NOW = datetime.now(timezone.utc)


class TestClassify(unittest.TestCase):
    def test_categories(self):
        c = analysis.classify_block
        self.assertEqual(c("VISION", "VISION_UNAVAILABLE: Gemini vision grading failed"), "SYSTEM_UNRESPONSIVE")
        self.assertEqual(c("VISION", "grade C"), "VISION_REJECTED_HISTORICAL")
        self.assertEqual(c("KILL_SWITCH", "x"), "SYSTEM")
        self.assertEqual(c("SIZE", "RISK_BUDGET_EXHAUSTED"), "RISK_LIMIT")
        self.assertEqual(c("SPREAD", "SPREAD_ABOVE_MAX"), "COST_OR_SPREAD")
        self.assertEqual(c("REGIME", "regime TRANSITION"), "MARKET_CONDITION")


def seed():
    repo = MemoryRepo()
    repo.insert("signals", {"id": "s1", "created_at": NOW, "strategy_id": "micro_scalper", "symbol": "EURUSDm", "direction": "BUY",
                            "entry": 1.1, "sl": 1.09, "tp": 1.12, "quality": 90, "edge_r": 0.4, "cost_r": 0.1, "regime": "TREND_UP",
                            "session": "LONDON", "features": {"vision_grade": "A", "vision_reasoning": "clean pin bar",
                                                              "trigger_pattern": "PIN_BAR"}})
    repo.insert("trades", {"id": "t1", "signal_id": "s1", "closed_at": NOW, "state": "CLOSED", "symbol": "EURUSDm", "strategy_id": "micro_scalper",
                           "direction": "BUY", "net_pnl": -3.5, "r_multiple": -1.0, "exit_reason": "STOP_LOSS", "mfe_r": 0.2, "mae_r": -1.0})
    repo.insert("trades", {"id": "t2", "signal_id": None, "closed_at": NOW, "state": "CLOSED", "symbol": "XAUUSDm", "strategy_id": "micro_scalper",
                           "direction": "SELL", "net_pnl": 2.0})
    repo.insert("rejections", {"id": 1, "created_at": NOW, "signal_id": "s1", "strategy_id": "micro_scalper", "symbol": "EURUSDm",
                               "direction": "BUY", "gate": "VISION", "reason": "VISION_UNAVAILABLE: 404"})
    return repo


class TestApi(unittest.TestCase):
    def test_loss_and_blocked_endpoints(self):
        client = TestClient(create_app(Bridge(), seed(), ConfigManager(), "tok"))
        L = client.get("/api/analysis/losses").json()
        self.assertEqual(L["summary"]["losing_trades"], 1)
        row = L["rows"][0]
        # vision_* keys are retired: they must be filtered OUT of features, not surfaced
        self.assertNotIn("vision_grade", row["signal"])
        self.assertNotIn("vision_grade", row["signal"]["features"])
        self.assertEqual(row["signal"]["trigger"], "PIN_BAR")
        B = client.get("/api/analysis/blocked?only=positive").json()
        self.assertEqual(B["rows"][0]["category"], "SYSTEM_UNRESPONSIVE")
        self.assertEqual(B["summary"]["positive_calc_blocked_by_system"], 1)
        self.assertIn("Analysis", client.get("/analysis").text.replace("&", "") + "Analysis")
        self.assertEqual(client.get("/api/analysis/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
