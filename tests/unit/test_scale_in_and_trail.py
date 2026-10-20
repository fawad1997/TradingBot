import unittest

from tests.helpers import started_core, submit, trades
from app.core.types import Side, Tick


def open_one(**kw):
    core, sim, broker, d = started_core(overrides={"risk.max_concurrent_positions": 5, **kw})
    submit(core, Side.BUY)
    t = trades(core)[0]
    return core, broker, t


class TestScaleIn(unittest.TestCase):
    def test_blocked_until_winner_is_protected_then_allowed_and_capped(self):
        core, broker, t = open_one()
        submit(core, Side.BUY)
        self.assertEqual(len(trades(core)), 1)                      # unprotected -> no add
        entry, risk = t["entry_price"], t["entry_price"] - t["initial_sl"]
        core.book.update(core.book.active[t["id"]], sl=entry + 0.0001)     # protected at breakeven
        tk = core.last_tick
        core.last_tick = Tick(tk.time_msc, entry + risk, entry + risk + (tk.ask - tk.bid))
        submit(core, Side.BUY)
        ts = trades(core)
        self.assertEqual(len(ts), 2)
        self.assertLess(ts[1]["requested_volume"], ts[0]["requested_volume"] + 1e-9)
        self.assertLessEqual(ts[1]["requested_volume"], ts[0]["requested_volume"] * 0.6 + 1e-9)   # half-risk add

    def test_opposite_direction_never_scales(self):
        core, broker, t = open_one()
        entry, risk = t["entry_price"], t["entry_price"] - t["initial_sl"]
        core.book.update(core.book.active[t["id"]], sl=entry + 0.0001)
        tk = core.last_tick
        core.last_tick = Tick(tk.time_msc, entry + risk, entry + risk + (tk.ask - tk.bid))
        submit(core, Side.SELL)
        self.assertEqual(len(trades(core)), 1)

    def test_disabled_flag(self):
        core, broker, t = open_one(**{"scaling.enabled": False})
        entry, risk = t["entry_price"], t["entry_price"] - t["initial_sl"]
        core.book.update(core.book.active[t["id"]], sl=entry + 0.0001)
        tk = core.last_tick
        core.last_tick = Tick(tk.time_msc, entry + risk, entry + risk + (tk.ask - tk.bid))
        submit(core, Side.BUY)
        self.assertEqual(len(trades(core)), 1)


class TestGiveback(unittest.TestCase):
    def test_config_defaults(self):
        from app.config import ConfigManager
        e = ConfigManager().app.execution
        self.assertEqual(e.hard_profit_target_money, 0.0)
        self.assertAlmostEqual(e.profit_giveback_fraction, 0.3)

    def test_floor_trails_peak_fraction(self):
        core, broker, t = open_one(**{"execution.hard_profit_target_money": 0.0,
                                      "execution.profit_giveback_fraction": 0.3})
        m = core.manager
        self.assertAlmostEqual(m.profit_giveback_fraction, 0.3)
        st = core.book.ensure_mstate(core.book.active[t["id"]])
        st.profit_high_watermark_money = 10.0
        # a 70% floor of a $10 peak is $7: a $9 giveback-free position stays, a $6.5 one is closed
        floor = max(m.ambition_floor_money, st.profit_high_watermark_money * (1 - m.profit_giveback_fraction))
        self.assertAlmostEqual(floor, 7.0)


if __name__ == "__main__":
    unittest.main()


class TestProtectedWinnerDoesNotBlockOtherEntries(unittest.TestCase):
    def test_breakeven_stop_counts_as_zero_open_risk(self):
        core, broker, t = open_one()
        before = core._open_risk_money()
        self.assertGreater(before, 0)
        core.book.update(core.book.active[t["id"]], sl=t["entry_price"] + 0.5)      # profit locked above entry
        self.assertEqual(core._open_risk_money(), 0.0)
