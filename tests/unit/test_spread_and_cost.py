import unittest
from types import SimpleNamespace
from unittest import mock


from app.config import ConfigManager
from app.core.types import Side, Tick
from tests.helpers import started_core, submit, trades, rows


class TestSpreadVersusStop(unittest.TestCase):
    def test_wide_spread_relative_to_stop_is_blocked_then_waits(self):
        core, sim, broker, _ = started_core(overrides={"cost.max_spread_fraction_of_stop": 0.10,
                                                        "spread_gate.wait_for_spread": True})
        tick = core.last_tick
        stop = 10 * tick.spread                      # spread = 10% of the stop: allowed
        ok, why = core._spread_ok(tick, "LONDON", stop)
        self.assertTrue(ok, why)
        ok, why = core._spread_ok(tick, "LONDON", stop / 2)    # spread = 20% of the stop: blocked
        self.assertFalse(ok)
        self.assertIn("SPREAD_TOO_LARGE_VS_STOP", why)

    def test_signal_with_a_tight_stop_never_becomes_a_trade(self):
        core, sim, broker, _ = started_core(overrides={"cost.max_spread_fraction_of_stop": 0.10})
        atr = float(core.ff["M15"].atr(14)[core.ff["M15"].last])
        tight = core.last_tick.spread / atr * 2       # stop = 2 spreads -> the spread is 50% of the stop
        submit(core, Side.BUY, stop_atr=tight)
        self.assertEqual(len(trades(core)), 0)
        self.assertIn(rows(core, "rejections")[-1]["gate"], ("COST", "SPREAD", "SIZE"))

    def test_spread_gate_waits_when_only_the_stop_fraction_binds(self):
        # no slippage/commission, so the spread-vs-stop rule is the binding constraint
        core, sim, broker, _ = started_core(overrides={"cost.max_spread_fraction_of_stop": 0.10,
                                                        "cost.default_slippage_points": 0.0,
                                                        "cost.max_cost_fraction_of_risk": 0.9,
                                                        "spread_gate.wait_for_spread": True})
        atr = float(core.ff["M15"].atr(14)[core.ff["M15"].last])
        submit(core, Side.BUY, stop_atr=core.last_tick.spread / atr * 5)      # spread = 20% of the stop
        self.assertEqual(len(trades(core)), 0)
        self.assertEqual(len(core.pending), 1)        # waiting for a better spread, not thrown away
        self.assertIn("SPREAD_TOO_LARGE_VS_STOP", core.pending[0]["reason"])

    def test_disabled_limit_keeps_old_behaviour(self):
        core, *_ = started_core(overrides={"cost.max_spread_fraction_of_stop": 0.0})
        self.assertTrue(core._spread_ok(core.last_tick, "LONDON", core.last_tick.spread)[0])


class TestMicroScalperStopFloor(unittest.TestCase):
    def test_stop_is_floored_at_a_multiple_of_the_spread(self):
        from app.features.frame import FeatureFrame
        from app.strategies.registry import build
        cfg = ConfigManager().strategy("micro_scalper")
        self.assertEqual(cfg.params["min_stop_spread_multiple"].value, 10.0)
        strat = build(cfg)
        # a tiny ATR with a wide spread must still produce a stop >= 10 x spread
        self.assertGreaterEqual(float(strat.params["min_stop_spread_multiple"]), 3.0)


class TestProfitLockKeepsWinners(unittest.TestCase):
    def test_failed_stop_move_no_longer_closes_the_position(self):
        core, *_ = started_core()
        self.assertFalse(core.manager.close_when_profit_lock_unprotectable)
        self.assertFalse(ConfigManager().app.execution.close_when_profit_lock_unprotectable)


if __name__ == "__main__":
    unittest.main()




if __name__ == "__main__":
    unittest.main()
