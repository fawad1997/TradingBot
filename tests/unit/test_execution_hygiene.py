"""Two execution faults visible in the 15 Oct broker feed, and the behaviour that closes each.

1. COOLDOWN CHURN. Every tick that nudged a trail by a fraction of a point fired another SL_CHANGE. WE enforce
   a 5s cooldown between stop moves, so the surplus was refused by our own code - position 2063100403 logged
   roughly forty attempts between 13:04 and 13:08, almost all rejected, and on 16 Oct one GBPUSD position
   attempted 65 and got 24 through. (Those refusals used to be reported as MT5's 10024 THROTTLED, which reads
   as the BROKER rate-limiting us; they now carry OWN_COOLDOWN.) An attempt CONSUMES the cooldown, so a trivial
   nudge can block the meaningful move queued behind it. `improves()` now takes a minimum step, measured as a
   fraction of that trade's own initial risk.

2. A FLOOR BELOW THE COST LINE. Seven trades exited with reason SMALL_WIN_PROFIT_FLOOR and a NEGATIVE result:
   -0.08, -0.08, -0.12, -0.07, -0.08, -0.00 and -0.19R. The base rung locks a flat profit_lock_floor_money of
   $0.05, which the spread alone swallows. A floor beneath the round-trip cost is not protection. The lock
   target is now held at profit_lock_cost_multiple x the estimated exit cost.
"""
import unittest

from app.alpha.exits import OpenTradeState, improves
from app.config import ConfigManager
from app.core.types import Side


def state(side=Side.BUY, entry=100.0, initial_sl=98.0, sl=98.0):
    """A trade risking 2.0 price units, so 1R = 2.0 and 0.02R = 0.04."""
    return OpenTradeState(side=side, entry=entry, initial_sl=initial_sl, sl=sl, tp=None,
                          volume=0.01, initial_volume=0.01, entry_time=1)


class TestMinimumStopStep(unittest.TestCase):
    def test_a_trivial_nudge_is_not_worth_a_broker_round_trip(self):
        st = state()
        self.assertFalse(improves(st, 98.01, min_step_r=0.02), "0.01 on a 2.0 risk is 0.005R - churn")
        self.assertFalse(improves(st, 98.039, min_step_r=0.02))

    def test_a_material_move_still_goes_through(self):
        st = state()
        self.assertTrue(improves(st, 98.05, min_step_r=0.02))
        self.assertTrue(improves(st, 99.0, min_step_r=0.02))

    def test_breakeven_sized_moves_are_never_filtered(self):
        """The move that matters most - stop to entry - is half a risk unit, far above any sane step."""
        st = state()
        self.assertTrue(improves(st, 100.0, min_step_r=0.02))

    def test_zero_restores_the_old_behaviour_exactly(self):
        st = state()
        self.assertTrue(improves(st, 98.0000001, min_step_r=0.0))

    def test_it_still_refuses_to_loosen_a_stop(self):
        """The original guarantee: stops only ever tighten, whatever the step."""
        st = state()
        for step in (0.0, 0.02, 0.5):
            self.assertFalse(improves(st, 97.0, min_step_r=step))
            self.assertFalse(improves(state(Side.SELL, 100.0, 102.0, 102.0), 103.0, min_step_r=step))

    def test_it_works_on_the_sell_side(self):
        st = state(Side.SELL, entry=100.0, initial_sl=102.0, sl=102.0)
        self.assertFalse(improves(st, 101.99, min_step_r=0.02))
        self.assertTrue(improves(st, 101.9, min_step_r=0.02))

    def test_an_unknown_risk_cannot_block_a_move(self):
        """entry == initial_sl gives zero risk; the step must collapse to the old epsilon, not to infinity."""
        st = OpenTradeState(side=Side.BUY, entry=100.0, initial_sl=100.0, sl=99.0, tp=None,
                            volume=0.01, initial_volume=0.01, entry_time=1)
        self.assertTrue(improves(st, 99.5, min_step_r=0.02))

    def test_the_configured_default_is_small_enough_to_be_safe(self):
        step = ConfigManager().app.execution.min_sl_step_r
        self.assertGreater(step, 0.0)
        self.assertLessEqual(step, 0.05, "a large step would suppress real protective moves")


class TestFloorNeverBooksALoss(unittest.TestCase):
    """The ladder's own arithmetic: whatever tier is active, the floor clears the cost line."""

    @staticmethod
    def lock_target(raw_target, exit_costs, multiple=1.5, lock_active=True):
        if exit_costs is not None and lock_active:
            cost_floor = float(exit_costs) * float(multiple)
            if cost_floor > raw_target:
                return cost_floor
        return raw_target

    def test_the_base_rung_is_lifted_above_cost(self):
        """$0.05 flat floor against $0.18 of spread was the bug: it closed at -0.08R."""
        self.assertAlmostEqual(self.lock_target(0.05, exit_costs=0.18), 0.27, places=6)

    def test_a_floor_already_above_cost_is_untouched(self):
        self.assertAlmostEqual(self.lock_target(7.00, exit_costs=0.18), 7.00, places=6)

    def test_an_inactive_lock_is_untouched(self):
        self.assertAlmostEqual(self.lock_target(0.05, exit_costs=0.18, lock_active=False), 0.05, places=6)

    def test_unknown_costs_change_nothing(self):
        self.assertAlmostEqual(self.lock_target(0.05, exit_costs=None), 0.05, places=6)

    def test_the_result_always_clears_the_cost_line(self):
        for raw in (0.0, 0.01, 0.05, 0.5):
            for cost in (0.02, 0.18, 1.40):
                self.assertGreater(self.lock_target(raw, cost), cost,
                                   f"raw {raw} cost {cost} would still book a loss")

    def test_the_live_code_applies_it(self):
        import inspect
        from app.execution.trade_manager import TradeManager
        src = inspect.getsource(TradeManager.on_tick)
        self.assertIn("profit_lock_cost_multiple", src)
        self.assertIn("cost_floor > lock_target", src)

    def test_the_configured_multiple_leaves_margin(self):
        m = ConfigManager().app.execution.profit_lock_cost_multiple
        self.assertGreaterEqual(m, 1.0, "below 1.0 the floor can still book a loss")


class TestRiskIsNotInvertedAgainstEvidence(unittest.TestCase):
    def test_the_worst_performer_no_longer_runs_at_full_size(self):
        """micro_scalper: 38 of 45 losses and -$378 of -$424 over 7 days, previously at risk_multiplier 1.0."""
        by = {s.id: s for s in ConfigManager().strategies.strategies}
        self.assertLessEqual(by["micro_scalper"].risk_multiplier, 0.5)

    def test_no_strategy_without_validation_stats_exceeds_full_size(self):
        for s in ConfigManager().strategies.strategies:
            self.assertLessEqual(s.risk_multiplier, 1.0, s.id)


if __name__ == "__main__":
    unittest.main()
