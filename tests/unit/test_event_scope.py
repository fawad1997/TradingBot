"""The economic calendar is scoped per symbol: a UK release stops GBP pairs, not NVDA or gold, and the news
straddle still trades only the releases it is configured for."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.config import EventsCfg
from app.core.types import EventState
from app.market.events import EconomicEvent, EventCalendar, symbol_currencies
from tests.helpers import started_core

NOW = datetime(2026, 10, 21, 5, 50, tzinfo=timezone.utc)
CFG = EventsCfg(currencies=["USD", "EUR", "GBP", "JPY"])
GBP_CPI = EconomicEvent(NOW + timedelta(minutes=10), "GBP", "HIGH", "CPI y/y")


def cal(currencies=None, events=(GBP_CPI,)):
    return EventCalendar(CFG, list(events), symbol_currencies=currencies)


class TestSymbolScope(unittest.TestCase):
    def test_currencies_come_from_the_broker_spec(self):
        self.assertEqual(symbol_currencies(SimpleNamespace(currency_base="GBP", currency_profit="JPY")), {"GBP", "JPY"})
        self.assertEqual(symbol_currencies(SimpleNamespace(currency_base="USD", currency_profit="USD")), {"USD"})
        self.assertIsNone(symbol_currencies(SimpleNamespace(currency_base="", currency_profit="")))

    def test_a_uk_release_blocks_gbp_pairs_only(self):
        self.assertEqual(cal({"GBP", "USD"}).state(NOW), EventState.PRE_EVENT)
        self.assertEqual(cal({"GBP", "JPY"}).state(NOW), EventState.PRE_EVENT)
        self.assertEqual(cal({"USD"}).state(NOW), EventState.NORMAL)          # NVDA
        self.assertEqual(cal({"XAU", "USD"}).state(NOW), EventState.NORMAL)   # gold

    def test_unscoped_calendar_applies_every_watched_event(self):
        self.assertEqual(cal(None).state(NOW), EventState.PRE_EVENT)

    def test_coverage_is_judged_on_the_whole_feed(self):
        # No JPY event at all is not a stale calendar: the feed itself is fresh.
        self.assertTrue(cal({"JPY"}).coverage_ok(NOW))
        self.assertEqual(cal({"JPY"}).state(NOW), EventState.NORMAL)
        self.assertFalse(cal({"JPY"}, events=()).coverage_ok(NOW))

    def test_unwatched_currencies_and_impacts_are_ignored(self):
        c = cal(None, events=[EconomicEvent(NOW + timedelta(minutes=10), "CAD", "HIGH", "CPI"),
                              EconomicEvent(NOW + timedelta(minutes=10), "GBP", "MEDIUM", "PMI")])
        self.assertEqual(c.events, [])

    def test_next_event_can_be_narrowed(self):
        usd = EconomicEvent(NOW + timedelta(hours=3), "USD", "HIGH", "NFP")
        c = cal({"GBP", "USD"}, events=[GBP_CPI, usd])
        self.assertEqual(c.next_event(NOW).title, "CPI y/y")
        self.assertEqual(c.next_event(NOW, ["USD"]).title, "NFP")

    def test_rescoping_keeps_the_loaded_events(self):
        c = cal(None)
        c.scope_to({"USD"})
        self.assertEqual(c.events, [])
        c.scope_to({"GBP"})
        self.assertEqual(len(c.events), 1)


class TestEngineAndStraddle(unittest.TestCase):
    def test_the_core_scopes_its_calendar_to_its_symbol(self):
        core, *_ = started_core()
        self.assertEqual(core.calendar.symbol_currencies, symbol_currencies(core.spec))

    def test_the_straddle_ignores_releases_outside_its_currencies(self):
        core, sim, broker, _ = started_core(overrides={"news_straddle.enabled": True,
                                                       "news_straddle.symbols": ["XAUUSD"],
                                                       "events.currencies": ["USD", "GBP"],
                                                       "health.stale_tick_sec": 3600})
        now = core.now_utc()
        core.calendar.scope_to(None)                    # let a GBP event reach this core
        core.calendar.set_events([EconomicEvent(now + timedelta(minutes=4), "GBP", "HIGH", "CPI")])
        core.straddle.cfg_fn = lambda: core.cfg
        core.straddle.step()
        self.assertIsNone(core.straddle.cur)
        self.assertEqual(broker.pending_orders(core.symbol), [])

    def test_live_config_watches_the_traded_currencies_and_keeps_the_straddle_on_usd(self):
        cfg = json.loads(Path("config/config.json").read_text(encoding="utf-8"))
        self.assertTrue({"USD", "EUR", "GBP", "JPY"} <= set(cfg["events"]["currencies"]))
        self.assertEqual(cfg["news_straddle"].get("currencies", ["USD"]), ["USD"])


if __name__ == "__main__":
    unittest.main()
