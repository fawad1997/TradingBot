"""/api/backtest/latest serves the replay report: summaries by default, one rule's trades on request, and an
empty shape (not an error) before the first replay. The Analysis page carries the card that reads it."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from app.config import ConfigManager
from app.core.bridge import Bridge
from app.dashboard.api import create_app
from app.research.lab import replay
from app.storage.repo import MemoryRepo

TRADES = [{"sym": "EURUSDm", "t": 1760000000 + i * 3600, "x": 1760000000 + i * 3600 + 900, "s": "BUY",
           "r": r, "why": ""} for i, r in enumerate([1.0, -1.0, 2.0])]


class TestBacktestApi(unittest.TestCase):
    def client(self):
        return TestClient(create_app(Bridge(), MemoryRepo(), ConfigManager(), "tok"))

    def test_summaries_and_trades_on_request(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(replay, "ROOT", Path(d)):
            replay.save(replay.build({"pullback_v1": {"EURUSDm": {"trades": TRADES}}}))
            c = self.client()
            got = c.get("/api/backtest/latest").json()
            self.assertEqual(got["strategies"]["pullback_v1"]["pooled"]["n"], 3)
            self.assertNotIn("trades", got)
            self.assertEqual(got["trade_counts"], {"pullback_v1": 3})
            one = c.get("/api/backtest/latest?trades=pullback_v1").json()
            self.assertEqual(len(one["trades"]["pullback_v1"]), 3)

    def test_no_report_yet(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(replay, "ROOT", Path(d)):
            r = self.client().get("/api/backtest/latest")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"strategies": {}})

    def test_analysis_page_has_the_card(self):
        html = Path("app/dashboard/static/analysis.html").read_text(encoding="utf-8")
        js = Path("app/dashboard/static/app.js").read_text(encoding="utf-8")
        for el in ("rpTable", "rpCurve", "rpSyms", "rpHead"):
            self.assertIn(f'id="{el}"', html)
        self.assertIn('fetch("/api/backtest/latest")', js)
        self.assertIn("loadReplay()", js)


if __name__ == "__main__":
    unittest.main()
