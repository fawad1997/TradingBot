"""Three fixes that came out of reading the 16 Oct trade timelines.

Each one existed because the system was telling the truth in a way nobody could read, or not telling it at
all. None of them changes a trading decision; all three change what the evidence says, which is worse,
because every judgement after them was made on that evidence.

1. The best and worst price a trade reached were saved only on a bar close, so a trade that made its final
   run and closed mid-bar recorded a stale peak. One GBPJPY trade was recorded peaking at +0.30R while it
   was closing at its +1.51R target - not imprecise, impossible.

2. Our own stop-move cooldown was reported with MT5's retcode 10024 / "THROTTLED", which means the BROKER is
   rate-limiting. 120 of 217 stop moves on 16 Oct were refused by us and looked like Exness throttling.

3. "SL" names the order that filled, not the result. 7 of 12 "SL" exits on 16 Oct were profits, because the
   stop had been trailed past the entry first.
"""
import unittest
from types import SimpleNamespace

from app.alpha.exits import OpenTradeState
from app.core.types import Side
from app.dashboard.analysis import build_loss_rows, exit_outcome, loss_summary
from app.execution.trade_manager import MIN_SL_COOLDOWN_SEC, RETCODE_SELF_COOLDOWN, TradeManager


def state(entry=100.0, sl=90.0, side=Side.BUY):
    # mfe_r / mae_r are derived properties, not fields - they read best_price / worst_price
    return OpenTradeState(side=side, entry=entry, initial_sl=sl, sl=sl, tp=entry + 15,
                          volume=0.1, initial_volume=0.1, entry_time=0,
                          best_price=entry, worst_price=entry)


class Book:
    """Just enough TradeBook to record what was written."""

    def __init__(self):
        self.writes = []

    def update(self, trade, **kw):
        self.writes.append(kw)

    def open_positions(self):
        return []


def manager_with(book, step=0.02):
    m = TradeManager.__new__(TradeManager)         # skip __init__: this test is about one method
    m.book = book
    m._last_persisted_extremes = {}
    m.peak_persist_step_r = step
    return m


class TestThePeakIsSavedAsItHappens(unittest.TestCase):
    def test_a_new_high_is_written_immediately(self):
        book = Book(); m = manager_with(book)
        st = state(); trade = {"id": "t1"}
        st.observe(103.0, 100.0)                   # +0.3R
        m._persist_extremes(trade, st)
        self.assertEqual(len(book.writes), 1)
        self.assertAlmostEqual(book.writes[0]["mfe_r"], 0.3, places=6)
        self.assertAlmostEqual(book.writes[0]["best_price"], 103.0, places=6)

    def test_the_peak_that_mattered_is_not_lost_to_a_bar_boundary(self):
        """The GBPJPY case: the final run to target happens between bar closes."""
        book = Book(); m = manager_with(book)
        st = state(); trade = {"id": "t1"}
        st.observe(103.0, 100.0)                   # +0.30R - all a bar close would have caught
        m._persist_extremes(trade, st)
        st.observe(115.1, 100.0)                   # the run into target, mid-bar
        m._persist_extremes(trade, st)
        self.assertAlmostEqual(book.writes[-1]["mfe_r"], 1.51, places=2)

    def test_tiny_moves_do_not_cause_a_write_per_tick(self):
        book = Book(); m = manager_with(book, step=0.02)
        st = state(); trade = {"id": "t1"}
        st.observe(103.0, 100.0)
        m._persist_extremes(trade, st)
        n = len(book.writes)
        for px in (103.01, 103.02, 103.05, 103.1):   # all inside the 0.02R (0.2 price) step
            st.observe(px, 100.0)
            m._persist_extremes(trade, st)
        self.assertEqual(len(book.writes), n, "a drifting position must not write on every tick")
        st.observe(103.3, 100.0)                     # now past the step
        m._persist_extremes(trade, st)
        self.assertEqual(len(book.writes), n + 1)

    def test_a_worse_low_is_recorded_too(self):
        book = Book(); m = manager_with(book)
        st = state(); trade = {"id": "t1"}
        st.observe(100.0, 95.0)
        m._persist_extremes(trade, st)
        self.assertAlmostEqual(book.writes[-1]["mae_r"], -0.5, places=6)

    def test_a_failure_to_record_never_reaches_the_caller(self):
        """Recording how far a trade ran must never interfere with managing it."""
        class Broken(Book):
            def update(self, trade, **kw):
                raise RuntimeError("database down")
        m = manager_with(Broken())
        st = state(); st.observe(103.0, 100.0)
        m._persist_extremes({"id": "t1"}, st)        # must not raise

    def test_the_setting_exists_with_a_sane_default(self):
        from app.config import ExecutionCfg
        self.assertAlmostEqual(ExecutionCfg().peak_persist_step_r, 0.02, places=6)


class TestOurCooldownIsNotBlamedOnTheBroker(unittest.TestCase):
    def test_the_retcode_is_outside_mt5s_range(self):
        """MT5 uses 10000-10046. A self-imposed wait must not be mistakable for a broker reply."""
        self.assertGreater(RETCODE_SELF_COOLDOWN, 10046)
        self.assertNotEqual(RETCODE_SELF_COOLDOWN, 10024, "10024 means the BROKER is rate-limiting")

    def test_the_cooldown_floor_can_be_shortened_but_not_removed(self):
        self.assertLess(MIN_SL_COOLDOWN_SEC, 5.0, "5s was hard-coded; it should be tunable")
        self.assertGreater(MIN_SL_COOLDOWN_SEC, 0.0, "but an attempt consumes the window, so never zero")

    def test_the_source_no_longer_calls_it_THROTTLED(self):
        import inspect
        src = inspect.getsource(TradeManager)
        self.assertNotIn('retcode_text="THROTTLED"', src)
        self.assertIn('retcode_text="OWN_COOLDOWN"', src)

    def test_the_dashboard_explains_it_as_ours(self):
        from pathlib import Path
        g = (Path(__file__).resolve().parents[2] / "app/dashboard/static/glossary.js").read_text(encoding="utf-8")
        self.assertIn("OWN_COOLDOWN", g)
        self.assertIn("our own spacing rule", g)


class TestAStopFillIsNotAutomaticallyALoss(unittest.TestCase):
    def test_a_trailed_stop_in_profit_reads_as_a_win(self):
        self.assertEqual(exit_outcome("SL", 5.41, 0.61), "protected win (trailed stop)")

    def test_a_first_stop_hit_reads_as_a_full_loss(self):
        self.assertEqual(exit_outcome("SL", -18.96, -1.0), "full loss (first stop)")

    def test_a_partial_loss_is_distinguished(self):
        self.assertEqual(exit_outcome("SL", -9.0, -0.45), "partial loss")

    def test_the_target_is_named(self):
        self.assertEqual(exit_outcome("TP", 15.11, 1.5), "reached target")

    def test_nothing_is_claimed_without_a_result(self):
        self.assertEqual(exit_outcome("SL", None, None), "unknown")

    def test_losing_rows_still_select_on_money_not_on_the_reason(self):
        """This part was already right. The regression would be selecting on exit_reason instead."""
        trades = [{"net_pnl": 5.41, "r_multiple": 0.61, "exit_reason": "SL", "strategy_id": "a"},
                  {"net_pnl": -18.96, "r_multiple": -1.0, "exit_reason": "SL", "strategy_id": "a"}]
        rows = build_loss_rows(trades, {})
        self.assertEqual(len(rows), 1, "a profitable stop-fill is not a losing trade")
        self.assertEqual(rows[0]["net_pnl"], -18.96)

    def test_losses_are_grouped_by_what_happened_not_by_the_deal_reason(self):
        trades = [{"net_pnl": -18.96, "r_multiple": -1.0, "exit_reason": "SL", "strategy_id": "a"},
                  {"net_pnl": -4.0, "r_multiple": -0.4, "exit_reason": "SL", "strategy_id": "a"}]
        summary = loss_summary(build_loss_rows(trades, {}))
        self.assertIn("full loss (first stop)", summary["by_exit_reason"])
        self.assertIn("partial loss", summary["by_exit_reason"])

    def test_the_close_message_does_not_call_a_win_a_stop_out(self):
        from app.notifications.trade_alerts import format_close
        m = format_close({"strategy_id": "vwap_reversion_v1", "symbol": "GBPUSDm", "direction": "SELL",
                          "filled_volume": 0.03, "entry_price": 1.33362, "exit_price": 1.33306,
                          "net_pnl": 1.68, "r_multiple": 0.39, "exit_reason": "SL"})
        self.assertTrue(m.startswith("Made 1.68"), m.splitlines()[0])
        self.assertNotIn("The stop was hit.", m)
        self.assertIn("keeping the profit", m)


if __name__ == "__main__":
    unittest.main()
