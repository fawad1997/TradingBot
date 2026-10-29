import unittest
from datetime import datetime, timezone

from tests.helpers import started_core, submit, trades, rows
from app.config import SessionsCfg
from app.core.symbol_control import SymbolTradingControl, parse_enabled
from app.core.types import Gate, Session, Side
from app.market.sessions import SessionClassifier
from app.storage.repo import MemoryRepo


class TestSymbolControl(unittest.TestCase):
    def test_default_running_and_persistence(self):
        repo = MemoryRepo()
        c = SymbolTradingControl(repo)
        self.assertTrue(c.is_enabled("XAUUSDm"))
        r = c.set("XAUUSDm", False, "me", "test")
        self.assertTrue(r["changed"] and r["persisted"])
        c2 = SymbolTradingControl(repo)
        c2.load()
        self.assertFalse(c2.is_enabled("XAUUSDm"))
        self.assertTrue(c2.is_enabled("BTCUSDm"))
        self.assertEqual(c2.snapshot(["XAUUSDm", "BTCUSDm"])["XAUUSDm"]["enabled"], False)

    def test_parse_enabled(self):
        self.assertTrue(parse_enabled("start"))
        self.assertFalse(parse_enabled("false"))
        with self.assertRaises(ValueError):
            parse_enabled("maybe")

    def test_stopped_symbol_never_trades_and_start_resumes(self):
        core, sim, broker, _ = started_core()
        res = core._command("set_symbol_trading", {"symbol": core.symbol, "enabled": False})
        self.assertTrue(res["ok"])
        submit(core, Side.BUY)
        self.assertEqual(len(trades(core)), 0)
        rej = rows(core, "rejections")
        self.assertEqual(rej[-1]["gate"], str(Gate.SYMBOL_DISABLED))
        core._evaluate_strategies()
        self.assertEqual(core.diagnostics["micro_scalper"], ["SYMBOL_TRADING_STOPPED"])
        core._command("set_symbol_trading", {"symbol": core.symbol, "enabled": True})
        submit(core, Side.BUY)
        self.assertEqual(len(trades(core)), 1)

    def test_unknown_symbol_and_bad_value(self):
        core, *_ = started_core()
        self.assertFalse(core._command("set_symbol_trading", {"symbol": "NOPE", "enabled": False})["ok"])
        self.assertFalse(core._command("set_symbol_trading", {"symbol": core.symbol, "enabled": "maybe"})["ok"])

    def test_stop_keeps_open_position_managed(self):
        core, sim, broker, _ = started_core()
        submit(core, Side.BUY)
        self.assertEqual(len(broker.positions()), 1)
        core._command("set_symbol_trading", {"symbol": core.symbol, "enabled": False})
        self.assertEqual(len(broker.positions()), 1)           # not force-closed
        self.assertEqual(len(core.book.open_positions()), 1)   # still managed

    def test_published_state(self):
        core, *_ = started_core()
        core._command("set_symbol_trading", {"symbol": core.symbol, "enabled": False})
        core._publish()
        st = core.bridge.get_state()["symbol_trading"]
        self.assertFalse(st[core.symbol]["enabled"])


class TestAlwaysOpen(unittest.TestCase):
    def test_weekend(self):
        sat = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        self.assertIs(SessionClassifier(SessionsCfg()).classify(sat), Session.CLOSED)
        self.assertIs(SessionClassifier(SessionsCfg(), always_open=True).classify(sat), Session.WEEKEND)
        self.assertEqual(SessionClassifier.market_status(sat, always_open=True)["status"], "OPEN")
        self.assertEqual(SessionClassifier.market_status(sat)["status"], "MARKET_CLOSED")

    def test_config_trades_one_gold_symbol(self):
        """XAUUSD247m is the same gold as XAUUSDm: trading both took every gold signal twice, and the two biggest
        losses of 20-22 Oct 2026 were such pairs. XAUUSDm stays - it has ten years of history to research on."""
        from app.config import ConfigManager
        c = ConfigManager().app
        self.assertIn("XAUUSDm", c.broker.symbol_candidates)
        self.assertNotIn("XAUUSD247m", c.broker.symbol_candidates)
        self.assertNotIn("XAUUSD247m", c.broker.always_open_symbols)


if __name__ == "__main__":
    unittest.main()


class TestWeekendStale(unittest.TestCase):
    def test_weekend_symbol_tolerates_longer_tick_gaps(self):
        core, *_ = started_core()
        h = core.health
        h.cfg.stale_tick_sec = 30.0
        h.cfg.weekend_stale_tick_sec = 300.0
        h.market_closed = False
        h.last_tick_wall = h.wall() - 120
        h.weekend_open = False
        self.assertIn("STALE_DATA", h.report(core.now_utc()).blockers)
        h.weekend_open = True
        self.assertNotIn("STALE_DATA", h.report(core.now_utc()).blockers)
        h.last_tick_wall = h.wall() - 400
        self.assertIn("STALE_DATA", h.report(core.now_utc()).blockers)
