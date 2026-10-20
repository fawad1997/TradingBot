"""Market status is per symbol, from the broker's quotes - not one weekend guess applied to all 13.

Observed 15 Oct 14:20 PKT (09:20 UTC, a Thursday): the Symbols panel showed MARKET=OPEN for all 13 symbols,
including NVDAm, TSLAm, AAPLm, AMZNm and MSFTm, whose US session does not start until 13:30 UTC. The order-flow
panel on the same page showed those five with CVD 1, Buy% 100 and a value area collapsed to a single price -
the signature of a symbol receiving no ticks at all.

Cause: SessionClassifier.market_status models exactly one schedule, the FX/metals weekend (Fri 21:00 -> Sun
22:00 UTC). Any weekday instant is "not the weekend", so every symbol read OPEN. The field was even labelled
schedule_type="estimated_weekend_hours".

Fix: TradingCore.market_state() answers for its own symbol from the age of its last broker quote, because a
shut market stops quoting. The weekend estimate is now only the fallback when there is no quote or no clock.
"""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from app.core.types import Tick
from app.market.sessions import SessionClassifier

THURSDAY_0920_UTC = datetime(2026, 10, 15, 9, 20, tzinfo=timezone.utc)


def core_with(last_tick, server_now, always_open=False, limit=180.0):
    """The three things market_state() reads, and nothing else."""
    from app.core.engine import TradingCore
    c = TradingCore.__new__(TradingCore)
    c.last_tick = last_tick
    c.server_now = lambda: server_now
    c.sessions = SimpleNamespace(always_open=always_open)
    c.cfg = SimpleNamespace(health=SimpleNamespace(market_closed_quote_age_sec=limit))
    return c


class TestTheOldEstimateWasTheBug(unittest.TestCase):
    def test_the_weekend_estimate_calls_a_shut_equity_market_open(self):
        """Why this test file exists: on a Thursday the estimate cannot tell a stock CFD from gold."""
        status = SessionClassifier.market_status(THURSDAY_0920_UTC)
        self.assertEqual(status["status"], "OPEN")


class TestMarketStateFromQuotes(unittest.TestCase):
    def test_a_silent_symbol_is_reported_closed(self):
        """NVDAm at 09:20 UTC: last quote hours old."""
        tick = Tick(time_msc=1_000_000_000_000, bid=220.8, ask=220.9)
        core = core_with(tick, server_now=tick.time + 4 * 3600)
        state = core.market_state()
        self.assertEqual(state["status"], "MARKET_CLOSED")
        self.assertEqual(state["schedule_type"], "broker_quotes")
        self.assertGreater(state["quote_age_sec"], 180.0)
        self.assertIn("no quote for", state["basis"])

    def test_a_quoting_symbol_is_reported_open(self):
        """XAUUSDm at the same instant: quoting normally."""
        tick = Tick(time_msc=1_000_000_000_000, bid=4142.2, ask=4142.3)
        core = core_with(tick, server_now=tick.time + 2.0)
        state = core.market_state()
        self.assertEqual(state["status"], "OPEN")
        self.assertEqual(state["schedule_type"], "broker_quotes")
        self.assertLess(state["quote_age_sec"], 180.0)

    def test_a_fresh_quote_beats_the_weekend_estimate(self):
        """XAUUSD247m trades through the weekend. If it is quoting, it is open, whatever the calendar says."""
        tick = Tick(time_msc=1_000_000_000_000, bid=4142.2, ask=4142.3)
        core = core_with(tick, server_now=tick.time + 1.0, always_open=True)
        self.assertEqual(core.market_state()["status"], "OPEN")

    def test_the_boundary_is_the_configured_limit(self):
        tick = Tick(time_msc=1_000_000_000_000, bid=1.0, ask=1.1)
        self.assertEqual(core_with(tick, tick.time + 179.0).market_state()["status"], "OPEN")
        self.assertEqual(core_with(tick, tick.time + 181.0).market_state()["status"], "MARKET_CLOSED")

    def test_no_quote_falls_back_to_the_estimate_and_says_so(self):
        """Before the first tick there is nothing to measure; the old behaviour is the fallback, labelled."""
        state = core_with(None, server_now=0.0).market_state()
        self.assertIn(state["status"], ("OPEN", "MARKET_CLOSED"))
        self.assertIsNone(state["quote_age_sec"])
        self.assertIn("estimate", state["basis"])

    def test_no_broker_clock_falls_back_too(self):
        tick = Tick(time_msc=1_000_000_000_000, bid=1.0, ask=1.1)
        state = core_with(tick, server_now=0.0).market_state()
        self.assertIsNone(state["quote_age_sec"])
        self.assertIn("estimate", state["basis"])

    def test_every_status_carries_the_keys_the_dashboard_reads(self):
        tick = Tick(time_msc=1_000_000_000_000, bid=1.0, ask=1.1)
        for core in (core_with(tick, tick.time + 1.0), core_with(tick, tick.time + 9999.0),
                     core_with(None, 0.0)):
            state = core.market_state()
            for key in ("status", "opens_at_utc", "seconds_to_open", "basis", "quote_age_sec",
                        "schedule_type"):
                self.assertIn(key, state)


class TestRunnerPublishesPerSymbol(unittest.TestCase):
    def test_symbols_can_disagree_in_one_publish(self):
        """The whole point: 13 symbols, not one shared verdict."""
        from app.core.multi import MultiAssetRunner
        fresh = Tick(time_msc=1_000_000_000_000, bid=4142.2, ask=4142.3)
        stale = Tick(time_msc=1_000_000_000_000, bid=220.8, ask=220.9)
        gold = core_with(fresh, fresh.time + 1.0); gold.symbol = "XAUUSDm"
        nvda = core_with(stale, stale.time + 4 * 3600); nvda.symbol = "NVDAm"
        published = {}
        runner = MultiAssetRunner.__new__(MultiAssetRunner)
        runner.cores = [gold, nvda]
        runner.cfgm = None
        runner.bridge = SimpleNamespace(update_state=lambda **kw: published.update(kw))
        runner._update_market_status()
        statuses = published["symbol_market_status"]
        self.assertEqual(statuses["XAUUSDm"]["status"], "OPEN")
        self.assertEqual(statuses["NVDAm"]["status"], "MARKET_CLOSED")

    def test_a_broken_core_does_not_take_the_panel_down(self):
        from app.core.multi import MultiAssetRunner
        bad = SimpleNamespace(symbol="BADm", market_state=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        published = {}
        runner = MultiAssetRunner.__new__(MultiAssetRunner)
        runner.cores = [bad]
        runner.cfgm = None
        runner.bridge = SimpleNamespace(update_state=lambda **kw: published.update(kw))
        runner._update_market_status()
        self.assertIn("BADm", published["symbol_market_status"])


if __name__ == "__main__":
    unittest.main()
