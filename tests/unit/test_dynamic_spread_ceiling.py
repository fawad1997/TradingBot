"""The spread ceiling is learned from the symbol's own live quotes, not from a hand-set number.

The hand-set per-symbol point limits were a guess made once; a symbol whose normal spread is wider than the
guess never trades at all, and a symbol whose normal spread is much tighter is allowed to trade at a cost the
guess happens to permit. These tests pin the learned behaviour: enough live samples -> the limit follows the
rolling median; too few samples -> the configured number; and the configured number still caps how far the
learned limit may expand.

These tests isolate the LEARNED ceiling, so they switch the hourly adaptive layer off. Since 16 Oct the
shipped config has `adaptive.enabled: true`, and the adaptive cap tightens whatever the learned ceiling
arrives at - without pinning it off here, every number below would silently become the adaptive cap and
these tests would be measuring a different feature. The interaction itself is pinned separately, at the
bottom of this file.
"""
import unittest

from tests.helpers import started_core

NO_ADAPTIVE = {"adaptive.enabled": False}


def seed(core, points, session="LONDON", n=400):
    for _ in range(n):
        core.spread.by_session.setdefault(session, core.spread.overall.__class__(20000)).add(points)
        core.spread.overall.add(points)


class TestDynamicSpreadCeiling(unittest.TestCase):
    def test_falls_back_to_the_configured_limit_until_enough_samples(self):
        core, *_ = started_core(overrides={"spread_gate.dynamic_ceiling": True,
                                           "spread_gate.dynamic_min_samples": 200,
                                           "spread_gate.max_spread_points": 300.0, **NO_ADAPTIVE})
        core.spread.by_session.clear()
        core.spread.overall.window.clear()
        core.spread.overall.sorted.clear()
        seed(core, 40.0, n=10)
        limit, basis = core.spread_limit_points("LONDON")
        self.assertEqual(limit, 300.0)
        self.assertEqual(basis, "configured")

    def test_learns_a_tighter_ceiling_when_the_symbol_actually_trades_tight(self):
        core, *_ = started_core(overrides={"spread_gate.dynamic_ceiling": True,
                                           "spread_gate.dynamic_multiple": 1.5,
                                           "spread_gate.dynamic_min_samples": 200,
                                           "spread_gate.max_spread_points": 300.0, **NO_ADAPTIVE})
        seed(core, 8.0)
        limit, basis = core.spread_limit_points("LONDON")
        self.assertAlmostEqual(limit, 12.0, places=6)      # 8 x 1.5, far below the 300 guess
        self.assertIn("live median", basis)

    def test_expands_past_a_configured_limit_that_is_too_tight_but_only_to_the_bound(self):
        core, *_ = started_core(overrides={"spread_gate.dynamic_ceiling": True,
                                           "spread_gate.dynamic_multiple": 1.5,
                                           "spread_gate.dynamic_max_expansion": 3.0,
                                           "spread_gate.dynamic_min_samples": 200,
                                           "spread_gate.max_spread_points": 300.0, **NO_ADAPTIVE})
        seed(core, 640.0)                                   # normal spread is wider than the configured ceiling
        limit, _ = core.spread_limit_points("LONDON")
        self.assertEqual(limit, 900.0)                      # 640 x 1.5 = 960, bounded by 300 x 3
        self.assertGreater(limit, 300.0)                    # the symbol is no longer blocked by construction

    def test_disabled_keeps_the_hand_set_number(self):
        core, *_ = started_core(overrides={"spread_gate.dynamic_ceiling": False,
                                           "spread_gate.max_spread_points": 300.0, **NO_ADAPTIVE})
        seed(core, 8.0)
        limit, basis = core.spread_limit_points("LONDON")
        self.assertEqual((limit, basis), (300.0, "configured"))

    def test_a_normal_spread_passes_the_gate_it_used_to_fail(self):
        core, *_ = started_core(overrides={"spread_gate.dynamic_ceiling": True,
                                           "spread_gate.dynamic_min_samples": 200,
                                           "spread_gate.max_spread_points": 300.0,
                                           "cost.max_spread_fraction_of_stop": 0.0})
        tick = core.last_tick
        sp = tick.spread / core.spec.point
        seed(core, sp)                                      # today's spread IS this symbol's normal spread
        ok, why = core._spread_ok(tick, "LONDON")
        self.assertTrue(ok, why)

    def test_an_outlier_spread_is_still_rejected(self):
        core, *_ = started_core(overrides={"spread_gate.dynamic_ceiling": True,
                                           "spread_gate.dynamic_multiple": 1.5,
                                           "spread_gate.dynamic_min_samples": 200,
                                           "cost.max_spread_fraction_of_stop": 0.0})
        tick = core.last_tick
        sp = tick.spread / core.spec.point
        seed(core, sp / 10.0)                               # normal is a tenth of what is quoted right now
        ok, why = core._spread_ok(tick, "LONDON")
        self.assertFalse(ok)
        self.assertIn("SPREAD", why)


if __name__ == "__main__":
    unittest.main()


class TestAdaptiveOnlyEverTightens(unittest.TestCase):
    """The hourly adaptive cap may lower the ceiling; it must never raise it.

    This matters because the adaptive layer was switched on for all 13 symbols on 16 Oct. A cap that could
    RAISE the ceiling would quietly let trades through on a spread the learned limit had already judged too
    wide - the exact cost that makes this system lose money - so the one-way rule is load-bearing.
    """

    def test_a_tighter_adaptive_cap_wins(self):
        core, *_ = started_core(overrides={"spread_gate.dynamic_ceiling": True,
                                           "spread_gate.dynamic_multiple": 1.5,
                                           "spread_gate.dynamic_min_samples": 200,
                                           "spread_gate.max_spread_points": 300.0,
                                           "adaptive.enabled": True})
        seed(core, 100.0)                       # learned ceiling would be 150
        core.adaptive.max_spread_points = 40.0
        limit, basis = core.spread_limit_points("LONDON")
        self.assertEqual(limit, 40.0)
        # The basis now carries the cap's age too, because an adaptive cap is only valid while it is fresh
        # (see AdaptiveState.effective_max_spread_points). A rejection has to say which threshold refused it
        # and how old that threshold is, or a stale ceiling is indistinguishable from a live one.
        self.assertTrue(basis.startswith("hourly adaptive cap"), basis)

    def test_a_looser_adaptive_cap_is_ignored(self):
        core, *_ = started_core(overrides={"spread_gate.dynamic_ceiling": True,
                                           "spread_gate.dynamic_multiple": 1.5,
                                           "spread_gate.dynamic_min_samples": 200,
                                           "spread_gate.max_spread_points": 300.0,
                                           "adaptive.enabled": True})
        seed(core, 8.0)                         # learned ceiling is 12
        core.adaptive.max_spread_points = 250.0
        limit, basis = core.spread_limit_points("LONDON")
        self.assertAlmostEqual(limit, 12.0, places=6)
        self.assertIn("live median", basis)

    def test_switched_off_the_adaptive_cap_does_nothing(self):
        core, *_ = started_core(overrides={"spread_gate.dynamic_ceiling": False,
                                           "spread_gate.max_spread_points": 300.0,
                                           "adaptive.enabled": False})
        core.adaptive.max_spread_points = 5.0
        self.assertEqual(core.spread_limit_points("LONDON"), (300.0, "configured"))
