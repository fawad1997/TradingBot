import unittest
from datetime import timedelta

from tests.helpers import started_core, rows
from app.core.types import Side, Tick, TradeState
from app.market.events import EconomicEvent
from app.market.calendar_sync import parse_feed

OVR = {"news_straddle.enabled": True, "news_straddle.symbols": ["XAUUSD"], "risk.max_concurrent_positions": 10,
       "health.stale_tick_sec": 3600}


def armed_core(minutes=4, **kw):
    core, sim, broker, d = started_core(overrides={**OVR, **kw})
    now = core.now_utc()
    core.calendar.set_events([EconomicEvent(now + timedelta(minutes=minutes), "USD", "HIGH", "CPI")])
    core.straddle.cfg_fn = lambda: core.cfg
    return core, sim, broker


def legs(core):
    return [t for t in rows(core, "trades") if t["strategy_id"] == "news_straddle"]


class TestStraddle(unittest.TestCase):
    def arm(self, **kw):
        core, sim, broker = armed_core(**kw)
        core.straddle.step()
        return core, sim, broker

    def test_arms_two_stop_orders_with_brackets(self):
        core, sim, broker = self.arm()
        self.assertEqual(core.straddle.cur["state"], "ARMED", core.straddle.last_skip)
        orders = broker.pending_orders(core.symbol)
        self.assertEqual(sorted(str(o.side) for o in orders), ["BUY", "SELL"])
        buy = next(o for o in orders if str(o.side) == "BUY")
        sell = next(o for o in orders if str(o.side) == "SELL")
        tick = core.last_tick
        self.assertGreater(buy.price, tick.ask)
        self.assertLess(sell.price, tick.bid)
        self.assertTrue(buy.sl < buy.price < buy.tp and sell.tp < sell.price < sell.sl)
        self.assertTrue(all(o.expiration > 0 for o in orders))
        self.assertEqual({t["state"] for t in legs(core)}, {"ORDER_SENT"})
        # risk is split: two legs together stay within one normal trade's research risk (1%)
        total = sum(t["risk_fraction"] for t in legs(core))
        self.assertLessEqual(total, 0.0101)

    def test_not_armed_when_far_from_event_or_symbol_not_listed(self):
        """Two separate reasons a straddle stays idle, and they must not be confused with each other.

        The shipped config enables the straddle on all 13 traded symbols (it used to ship off, on gold only),
        so 'idle' now has to be explained by the event clock or by the symbol list - never by the feature
        being switched off.
        """
        core, *_ = armed_core(minutes=30)      # event is 30 min away; arming window is 5 min
        core.straddle.step()
        self.assertIsNone(core.straddle.cur)
        core2, sim, b, _ = started_core()      # the simulator's symbol is not one of the 13
        self.assertEqual(core2.straddle.disabled_reason(), "SYMBOL_NOT_IN_STRADDLE_LIST")

    def test_oco_cancels_opposite_leg(self):
        core, sim, broker = self.arm()
        buy = next(o for o in broker.pending_orders(core.symbol) if str(o.side) == "BUY")
        t = core.last_tick
        broker.on_tick(Tick(t.time_msc + 1000, buy.price + 0.2, buy.price + 0.3), core.symbol)
        core.straddle.step()
        self.assertEqual(broker.pending_orders(core.symbol), [])
        self.assertEqual(len(broker.positions(core.symbol)), 1)
        by = {t["direction"]: t for t in legs(core)}
        self.assertEqual(by["BUY"]["state"], str(TradeState.OPEN))
        self.assertEqual(by["SELL"]["state"], str(TradeState.CANCELLED))
        self.assertIsNone(core.straddle.cur)
        self.assertIn("FILLED_BUY", core.straddle.last_outcome["outcome"])
        # the reconciler must not flag the filled leg as an unexpected position
        core.recon.run()
        self.assertFalse([r for r in rows(core, "trade_events") if r["event_type"] == "UNEXPECTED_POSITION_ADOPTED"])

    def test_reconciler_keeps_resting_legs(self):
        core, sim, broker = self.arm()
        core.recon.run(startup=True)
        self.assertEqual({t["state"] for t in legs(core)}, {"ORDER_SENT"})
        self.assertEqual(len(broker.pending_orders(core.symbol)), 2)

    def test_symbol_stop_cancels_resting_legs(self):
        core, sim, broker = self.arm()
        core._command("set_symbol_trading", {"symbol": core.symbol, "enabled": False})
        self.assertEqual(broker.pending_orders(core.symbol), [])
        self.assertEqual({t["state"] for t in legs(core)}, {"CANCELLED"})
        core.straddle.step()
        self.assertIsNone(core.straddle.cur)

    def test_spread_shock_before_release_stands_down(self):
        core, sim, broker = self.arm()
        core.health.spread_shock = True
        core.straddle.step()
        self.assertEqual(broker.pending_orders(core.symbol), [])
        self.assertEqual(core.straddle.last_outcome["outcome"], "SPREAD_SHOCK_BEFORE_RELEASE")

    def test_kill_switch_cancels(self):
        core, sim, broker = self.arm()
        core.kill.engage("test", "me")
        core.straddle.step()
        self.assertEqual(broker.pending_orders(core.symbol), [])

    def test_expiry_cancels_and_does_not_rearm_same_event(self):
        core, sim, broker = self.arm()
        core.straddle.cur["expires_broker"] = int(core.server_now()) - 1
        core.straddle.step()
        self.assertEqual(broker.pending_orders(core.symbol), [])
        core.straddle.step()
        self.assertIsNone(core.straddle.cur)           # same event is not armed twice
        self.assertEqual(len(legs(core)), 2)

    def test_placement_failure_never_leaves_one_sided(self):
        core, sim, broker = armed_core()
        orig = broker.place_pending
        calls = []

        def flaky(req):
            calls.append(req)
            if len(calls) == 2:
                from app.core.types import OrderResult
                return OrderResult(False, 10016, "INVALID_STOPS")
            return orig(req)
        broker.place_pending = flaky
        core.straddle.step()
        self.assertEqual(broker.pending_orders(core.symbol), [])
        self.assertIsNone(core.straddle.cur)
        self.assertNotIn("ORDER_SENT", {t["state"] for t in legs(core)})

    def test_restart_recovers_active_straddle(self):
        core, sim, broker = self.arm()
        cur_id = core.straddle.cur["id"]
        core.straddle.cur = None
        core.straddle.recover()
        self.assertEqual(core.straddle.cur["id"], cur_id)

    def test_shutdown_cancels_resting(self):
        core, sim, broker = self.arm()
        core.shutdown()
        self.assertEqual(broker.pending_orders(core.symbol), [])


class TestArmingCeiling(unittest.TestCase):
    """Resting legs must not be charged against the ordinary open-position limit.

    Before this was separated, two legs on one symbol counted as two positions, so with
    max_concurrent_positions=10 the first five symbols consumed the entire allowance and symbols six to
    thirteen were refused with MAX_CONCURRENT_POSITIONS at every single event - arbitrarily, by whichever
    order the round robin happened to reach them in. Enabling the straddle on 13 symbols would have
    quietly delivered 5.
    """

    def test_an_armed_symbol_does_not_consume_the_position_allowance(self):
        core, sim, broker, _ = started_core(overrides={**OVR, "risk.max_concurrent_positions": 2})
        now = core.now_utc()
        core.straddle.calendar.events = [EconomicEvent(time=now + timedelta(minutes=4), title="CPI",
                                                       currency="USD", impact="HIGH")]
        core.straddle.step()
        self.assertIsNotNone(core.straddle.cur, "a straddle must arm even on a tight position limit")
        # the two resting legs exist, but they are not counted as open positions
        self.assertEqual(core._non_straddle_open_count(), 0)
        self.assertEqual(core._armed_straddle_symbols(), 1)

    def test_the_straddle_has_its_own_ceiling(self):
        """With the cap reached on OTHER symbols, this symbol is refused for the straddle's own reason.

        The same-symbol case is covered by SYMBOL_ALREADY_EXPOSED and is checked first, so the cap is
        exercised here by reporting the armed count the way several cores sharing one account would.
        """
        core, sim, broker, _ = started_core(overrides={**OVR, "news_straddle.max_armed_symbols": 3})
        self.assertIsNone(core._straddle_block_reason(arming=True), "nothing armed yet; must be free to arm")
        core._armed_straddle_symbols = lambda: 3          # three other symbols are already armed
        self.assertEqual(core._straddle_block_reason(arming=True), "MAX_ARMED_STRADDLES")
        core._armed_straddle_symbols = lambda: 2          # one slot frees up
        self.assertIsNone(core._straddle_block_reason(arming=True))

    def test_is_armed_reports_true_while_legs_rest_and_false_when_idle(self):
        core, sim, broker, _ = started_core(overrides=OVR)
        self.assertFalse(core.straddle.is_armed(), "nothing armed yet")
        now = core.now_utc()
        core.straddle.calendar.events = [EconomicEvent(time=now + timedelta(minutes=4), title="CPI",
                                                       currency="USD", impact="HIGH")]
        core.straddle.step()
        self.assertTrue(core.straddle.is_armed())


class TestCalendarParse(unittest.TestCase):
    def test_parse(self):
        feed = [{"title": "CPI", "country": "USD", "date": "2026-10-14T08:30:00-04:00", "impact": "High"},
                {"title": "x", "country": "EUR", "date": "2026-10-14T08:30:00-04:00", "impact": "High"},
                {"title": "y", "country": "USD", "date": "2026-10-14T08:30:00Z", "impact": "Low"},
                {"title": "bad", "country": "USD", "date": "nope", "impact": "High"}]
        ev = parse_feed(feed, ["HIGH"], ["USD"])
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0].time.hour, 12)


if __name__ == "__main__":
    unittest.main()
