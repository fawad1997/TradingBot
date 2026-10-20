"""Per-trade notifications, and the guarantee that they can never damage a trade.

These messages are formatted inside TradeBook.transition, which is on the path of EVERY state change a trade
makes - the fill, the breakeven move, the close, the reconciliation. So the first and most important property
is not what the message says but that nothing in this module can interrupt that path. A notification failing is
an inconvenience; a notification taking an order down with it is not.

After that: the message must be readable by someone who does not trade for a living. It carries what the
operator would otherwise open the dashboard for - what was done, what is at stake, what it made or lost and
why it ended - with no codes, no abbreviations and no unexplained jargon. It must also not lie about
precision: a 5-digit FX price shown at 2 decimals makes entry, stop and target look identical.
"""
import unittest
from types import SimpleNamespace

from app.notifications.trade_alerts import TradeAlerts, attach, format_close, format_open

OPEN_TRADE = {
    "id": "t1", "strategy_id": "htf_continuation_v1", "symbol": "GBPUSDm", "direction": "BUY",
    "open_volume": 0.06, "entry_price": 1.32442, "sl": 1.32112, "tp": 1.32937,
    "initial_risk_money": 19.80, "regime": "TREND_UP", "session": "LONDON",
    "extra": {"features": {"rule": "htf_continuation", "atr": 0.00118, "stop_atr": 2.0}},
}
CLOSED_TRADE = {
    "id": "t1", "strategy_id": "micro_scalper", "symbol": "XAUUSDm", "direction": "BUY",
    "filled_volume": 0.07, "entry_price": 4138.882, "exit_price": 4135.44, "net_pnl": -18.96,
    "r_multiple": -1.0, "exit_reason": "SL", "bars_held": 0, "mfe_r": 0.02,
}


class Spy:
    def __init__(self, explode=False):
        self.sent, self.explode = [], explode

    def send(self, text):
        if self.explode:
            raise RuntimeError("network down")
        self.sent.append(text)


class TestNothingCanBreakATrade(unittest.TestCase):
    def test_a_transport_that_raises_is_swallowed(self):
        TradeAlerts(Spy(explode=True)).on_transition(OPEN_TRADE, "OPEN", "FILLED")

    def test_a_malformed_trade_is_swallowed(self):
        for bad in ({}, {"strategy_id": None}, {"entry_price": "not a number"},
                    {"extra": "not a dict"}, {"r_multiple": "x"}):
            TradeAlerts(Spy()).on_transition(bad, "CLOSED", "POSITION_CLOSED")

    def test_the_book_hook_cannot_propagate(self):
        """The real guarantee: TradeBook.transition wraps the observer."""
        import inspect
        from app.execution.book import TradeBook
        src = inspect.getsource(TradeBook.transition)
        self.assertIn("on_transition", src)
        self.assertIn("except Exception", src)

    def test_attach_is_a_no_op_when_switched_off(self):
        book = SimpleNamespace()
        cfg = SimpleNamespace(trade_alerts=SimpleNamespace(enabled=False))
        self.assertIsNone(attach(book, Spy(), cfg))
        self.assertFalse(hasattr(book, "on_transition"))

    def test_attach_survives_a_config_without_the_block(self):
        self.assertIsNone(attach(SimpleNamespace(), Spy(), SimpleNamespace()))


class TestWhatGetsSent(unittest.TestCase):
    def test_only_open_and_close_produce_messages(self):
        spy = Spy()
        a = TradeAlerts(spy)
        for state in ("APPROVED", "ORDER_SENT", "PROTECTION", "PARTIAL", "RUNNER",
                      "RECONCILIATION_REQUIRED", "CANCELLED", "REJECTED"):
            a.on_transition(OPEN_TRADE, state, "X")
        self.assertEqual(spy.sent, [], "intermediate states must not generate traffic")
        a.on_transition(OPEN_TRADE, "OPEN", "FILLED")
        a.on_transition(CLOSED_TRADE, "BROKER_CLOSED", "POSITION_CLOSED")
        self.assertEqual(len(spy.sent), 2)

    def test_each_direction_can_be_switched_off_independently(self):
        spy = Spy()
        TradeAlerts(spy, on_open=False).on_transition(OPEN_TRADE, "OPEN", "FILLED")
        self.assertEqual(spy.sent, [])
        TradeAlerts(spy, on_close=False).on_transition(CLOSED_TRADE, "CLOSED", "POSITION_CLOSED")
        self.assertEqual(spy.sent, [])

    def test_min_abs_r_suppresses_only_the_smallest_closes(self):
        spy = Spy()
        a = TradeAlerts(spy, min_abs_r=0.2)
        a.on_transition({**CLOSED_TRADE, "r_multiple": 0.05}, "CLOSED", "X")
        self.assertEqual(spy.sent, [])
        a.on_transition({**CLOSED_TRADE, "r_multiple": -0.9}, "CLOSED", "X")
        self.assertEqual(len(spy.sent), 1, "a loser must never be filtered out by a size threshold")


class TestMessageContent(unittest.TestCase):
    def test_the_open_message_answers_the_dashboard_questions(self):
        m = format_open(OPEN_TRADE)
        for token in ("buy", "GBPUSDm", "0.06", "1.32442", "1.32112", "1.32937", "19.80",
                      "htf_continuation_v1", "trending up", "london"):
            self.assertIn(token, m, token)

    def test_the_open_message_says_what_is_at_stake_in_money(self):
        """"1.5R" means nothing to most people; "losing 19.80 ... making about 29.70" does."""
        m = format_open(OPEN_TRADE)
        self.assertIn("losing 19.80", m)
        self.assertIn("making about 29.70", m)
        self.assertNotIn("R target", m)

    def test_risk_is_never_shown_with_a_plus_sign(self):
        """An amount at risk is not a gain."""
        self.assertNotIn("risk +", format_open(OPEN_TRADE))
        self.assertNotIn("+19.80", format_open(OPEN_TRADE))

    def test_the_close_message_leads_with_the_money(self):
        m = format_close(CLOSED_TRADE)
        self.assertTrue(m.startswith("Lost 18.96 on XAUUSDm"), m.splitlines()[0])
        self.assertIn("micro_scalper", m)

    def test_exit_codes_are_translated_not_printed(self):
        """SMALL_WIN_PROFIT_FLOOR tells a phone-reader nothing. The sentence has to carry it."""
        m = format_close({**CLOSED_TRADE, "net_pnl": 5.94, "r_multiple": 0.61,
                          "exit_reason": "SMALL_WIN_PROFIT_FLOOR"})
        self.assertNotIn("SMALL_WIN_PROFIT_FLOOR", m)
        self.assertIn("worth protecting", m)

    def test_a_winner_reads_as_a_gain(self):
        m = format_close({**CLOSED_TRADE, "r_multiple": 0.61, "net_pnl": 5.94, "exit_reason": "TP"})
        self.assertTrue(m.startswith("Made 5.94"), m.splitlines()[0])
        self.assertIn("reached its target", m)

    def test_a_full_loss_is_spelled_out(self):
        self.assertIn("the full amount it had put at risk", format_close(CLOSED_TRADE))

    def test_a_trade_that_was_well_ahead_before_losing_says_so(self):
        """The single most useful thing to know about a loss: was it ever winning?"""
        m = format_close({**CLOSED_TRADE, "mfe_r": 3.65})
        self.assertIn("ahead by 3.65 times the risk", m)
        # ... but not for a trade that never really moved, where it would be noise
        self.assertNotIn("ahead by", format_close(CLOSED_TRADE))

    def test_fx_and_metal_prices_each_get_sensible_decimals(self):
        self.assertIn("1.32442", format_open(OPEN_TRADE))                 # 5dp for FX
        self.assertIn("4,138.88", format_close(CLOSED_TRADE))             # 2dp for gold

    def test_a_close_without_a_result_still_reports(self):
        m = format_close({**CLOSED_TRADE, "r_multiple": None, "net_pnl": None})
        self.assertIn("micro_scalper", m)
        self.assertIn("XAUUSDm", m)

    def test_no_uppercase_codes_leak_into_a_message(self):
        """A single SHOUTING_TOKEN undoes the whole point of the rewrite."""
        import re
        for m in (format_open(OPEN_TRADE), format_close(CLOSED_TRADE),
                  format_close({**CLOSED_TRADE, "exit_reason": "DYNAMIC_PROFIT_FLOOR"})):
            leaked = [w for w in re.findall(r"[A-Z][A-Z0-9_]{4,}", m) if "_" in w]
            self.assertEqual(leaked, [], f"untranslated code in: {m}")

    def test_messages_stay_short_enough_to_read_on_a_phone(self):
        for m in (format_open(OPEN_TRADE), format_close(CLOSED_TRADE)):
            self.assertLessEqual(len(m.splitlines()), 7)
            self.assertLessEqual(len(m), 500)


class TestWiring(unittest.TestCase):
    def test_the_config_block_exists_with_safe_defaults(self):
        from app.config import TradeAlertsCfg
        d = TradeAlertsCfg()
        self.assertTrue(d.on_open)
        self.assertTrue(d.on_close)
        self.assertEqual(d.min_abs_r, 0.0)

    def test_the_execution_path_carries_the_indicator_values(self):
        import inspect
        from app.execution.engine import ExecutionEngine
        self.assertIn('"features"', inspect.getsource(ExecutionEngine))

    def test_attach_installs_the_hook_when_enabled(self):
        book = SimpleNamespace()
        cfg = SimpleNamespace(trade_alerts=SimpleNamespace(enabled=True, on_open=True, on_close=True,
                                                           min_abs_r=0.0))
        self.assertIsNotNone(attach(book, Spy(), cfg))
        self.assertTrue(callable(book.on_transition))


if __name__ == "__main__":
    unittest.main()
