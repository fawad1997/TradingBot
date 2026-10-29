"""The profit floor must trail the peak in BOTH tiers, not only above 0.8R.

Observed 15 Oct on XAUUSDm: 0.07 lots, entry 4095.17, SL 4092.46, so 1R = $18.97 and the target was also $18.97.
Profit ran to about $10 and gave it all back.

  small_win_activation_r 0.5 -> arms at  $9.48
  ambition_activation_r  0.8 -> arms at $15.18

A $10 peak crosses the small-win rung and nothing else, and that rung locked a FLAT small_win_floor_r 0.1 =
$1.90. Between $9.48 and $15.18 of profit - a $5.70 band - the trade protected a tenth of its risk. The ambition
tier already trailed at profit_giveback_fraction 0.3; the small-win tier did not. Now both do.
"""
import unittest

RISK = 18.97          # the gold trade above: (4095.17 - 4092.46) * 0.07 * 100
GIVEBACK = 0.3        # config execution.profit_giveback_fraction -> protect 70% of the peak


def _level(money: float, r: float, risk: float = RISK) -> float:
    """Mirror of TradeManager._level: max(configured money, configured R x this trade's initial risk)."""
    return max(money, r * risk) if r > 0 and risk > 0 else money


def ladder(peak: float, giveback: float = GIVEBACK) -> float:
    """The lock target TradeManager.on_tick sets, from the shared ladder it calls (app/alpha/exits.py)."""
    from app.alpha.exits import profit_lock_target
    _, target = profit_lock_target(peak, _level(0.20, 0.5), _level(0.05, 0.1), _level(0.90, 0.8),
                                   _level(0.50, 0.4), giveback, base_floor=0.05)
    return target


class TestProfitTrail(unittest.TestCase):
    def test_the_reported_trade_now_protects_seven_dollars(self):
        """The whole point: a $10 peak on this trade locked $1.90 and now locks $7.00."""
        self.assertAlmostEqual(ladder(10.0), 7.00, places=2)

    def test_the_old_dead_band_is_gone(self):
        """$9.48 to $15.18 used to lock a flat $1.90 regardless of how high the peak went."""
        for peak in (9.5, 10.0, 12.0, 14.0):
            self.assertGreater(ladder(peak), 1.90 + 1e-9, f"peak {peak} still locks the flat small floor")
            self.assertAlmostEqual(ladder(peak), peak * 0.7, places=2)

    def test_the_floor_never_drops_below_the_configured_minimum(self):
        small_floor = _level(0.05, 0.1)
        for peak in (9.49, 9.6, 10.0, 15.0):   # 9.49 is the first peak at/above the 0.5R rung
            self.assertGreaterEqual(ladder(peak), small_floor - 1e-9)

    def test_it_is_monotonic_in_the_peak(self):
        """A higher peak must never lock less - otherwise the floor could move down."""
        last = -1.0
        p = 0.0
        while p <= 25.0:
            cur = ladder(p)
            self.assertGreaterEqual(cur + 1e-9, last, f"lock target fell at peak {p}")
            last, p = cur, p + 0.25

    def test_below_activation_nothing_is_locked(self):
        """Arming earlier than 0.5R would put a near-breakeven stop into the noise; that is unchanged."""
        for peak in (0.0, 3.0, 5.0, 8.0, 9.0):
            self.assertAlmostEqual(ladder(peak), 0.05, places=2)

    def test_the_ambition_tier_is_untouched(self):
        for peak in (15.2, 17.0, 19.0):
            self.assertAlmostEqual(ladder(peak), max(_level(0.50, 0.4), peak * 0.7), places=2)

    def test_giveback_zero_restores_the_old_flat_behaviour(self):
        """Operators who set profit_giveback_fraction to 0 must get exactly what they had before."""
        self.assertAlmostEqual(ladder(10.0, giveback=0.0), _level(0.05, 0.1), places=2)

    def test_the_live_code_matches_this_ladder(self):
        """The live manager must use the SAME ladder the backtester and counterfactuals use, not a copy of it."""
        import inspect
        from app.alpha import exits
        from app.execution.trade_manager import TradeManager
        src = inspect.getsource(TradeManager.on_tick)
        self.assertIn("profit_lock_target(", src)
        self.assertIn("max(small_floor, peak * (1.0 - giveback))", inspect.getsource(exits.profit_lock_target))


if __name__ == "__main__":
    unittest.main()
