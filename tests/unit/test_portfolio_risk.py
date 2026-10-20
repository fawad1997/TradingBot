"""Several positions counted as a portfolio, not a pile of independent bets.

Two defects this replaces:
  1. risk was `sum(exposure.values())` - two uncorrelated 1% positions consumed 2% of the budget, so
     the second strategy to signal was refused on budget the first was not really using
  2. the correlated-group check used `abs(correlation)`, so -0.9 (partly offsetting) was treated
     exactly like +0.9 (doubling up), and direction was never consulted at all

The property that matters most here is the SAFETY one: the new model must never be more permissive than
the old one in the case the old one was right about, and must never grant a diversification benefit the
data does not support.
"""
import math
import unittest

from app.config import RiskCfg
from app.risk.portfolio import (DEFAULT_STRESS_CORRELATION, Leg, admits, combine, marginal_risk)

ALL = {x: {y: 0.0 for y in "ABCDEF" if y != x} for x in "ABCDEF"}


def corr(a, b, rho):
    return {a: {b: rho}}


class TestItMatchesTheOldModelWhereTheOldModelWasRight(unittest.TestCase):
    def test_perfectly_correlated_same_direction_is_the_arithmetic_sum(self):
        c = combine([Leg("A", 1.0), Leg("B", 1.0)], corr("A", "B", 1.0))
        self.assertAlmostEqual(c.diversified, 2.0, places=6)
        self.assertAlmostEqual(c.gross, 2.0, places=6)
        self.assertEqual(c.diversification_benefit, 0.0)

    def test_a_single_position_is_just_itself(self):
        c = combine([Leg("A", 1.0)], {})
        self.assertAlmostEqual(c.diversified, 1.0)
        self.assertAlmostEqual(c.gross, 1.0)

    def test_no_legs_is_zero_not_an_error(self):
        c = combine([], {})
        self.assertEqual((c.gross, c.diversified, c.stressed), (0.0, 0.0, 0.0))

    def test_zero_and_negative_risk_legs_are_ignored(self):
        c = combine([Leg("A", 1.0), Leg("B", 0.0), Leg("C", -5.0)], ALL)
        self.assertAlmostEqual(c.gross, 1.0)

    def test_the_diversified_total_never_exceeds_the_gross_sum(self):
        """Correlation can only reduce combined risk, never inflate it past the money at stake."""
        for rho in (-1.0, -0.5, 0.0, 0.5, 1.0):
            with self.subTest(rho=rho):
                c = combine([Leg("A", 1.3), Leg("B", 0.7)], corr("A", "B", rho))
                self.assertLessEqual(c.diversified, c.gross + 1e-9)


class TestDiversificationCredit(unittest.TestCase):
    def test_two_uncorrelated_positions(self):
        c = combine([Leg("A", 1.0), Leg("B", 1.0)], corr("A", "B", 0.0))
        self.assertAlmostEqual(c.diversified, math.sqrt(2), places=6)

    def test_four_uncorrelated_positions(self):
        c = combine([Leg(x, 1.0) for x in "ABCD"], ALL)
        self.assertAlmostEqual(c.diversified, 2.0, places=6)
        self.assertAlmostEqual(c.diversification_benefit, 0.5, places=6)

    def test_credit_grows_as_correlation_falls(self):
        got = [combine([Leg("A", 1.0), Leg("B", 1.0)], corr("A", "B", r)).diversified
               for r in (1.0, 0.75, 0.5, 0.25, 0.0)]
        self.assertEqual(got, sorted(got, reverse=True))


class TestDirectionCounts(unittest.TestCase):
    """Defect 2: abs(correlation) could not tell doubling up from offsetting."""

    def test_opposite_directions_get_more_credit_than_the_same_direction(self):
        same = combine([Leg("A", 1.0, 1), Leg("B", 1.0, 1)], corr("A", "B", 0.9)).diversified
        opp = combine([Leg("A", 1.0, 1), Leg("B", 1.0, -1)], corr("A", "B", 0.9)).diversified
        self.assertLess(opp, same)

    def test_a_negative_correlation_held_the_same_way_behaves_like_an_offset(self):
        a = combine([Leg("A", 1.0, 1), Leg("B", 1.0, 1)], corr("A", "B", -0.9)).diversified
        b = combine([Leg("A", 1.0, 1), Leg("B", 1.0, -1)], corr("A", "B", 0.9)).diversified
        self.assertAlmostEqual(a, b, places=6)


class TestTheFloorStopsOverCrediting(unittest.TestCase):
    """A hedge that holds through the event you need it for is a hope, not a measurement."""

    def test_an_offsetting_pair_is_never_credited_to_zero(self):
        c = combine([Leg("A", 1.0, 1), Leg("B", 1.0, -1)], corr("A", "B", 1.0))
        self.assertAlmostEqual(c.diversified, math.sqrt(2), places=6)
        self.assertGreater(c.diversified, 0.0)

    def test_the_default_floor_gives_no_hedge_credit_beyond_uncorrelated(self):
        uncorrelated = combine([Leg("A", 1.0), Leg("B", 1.0)], corr("A", "B", 0.0)).diversified
        hedged = combine([Leg("A", 1.0, 1), Leg("B", 1.0, -1)], corr("A", "B", 1.0)).diversified
        self.assertAlmostEqual(uncorrelated, hedged, places=6)

    def test_lowering_the_floor_deliberately_does_allow_hedge_credit(self):
        c = combine([Leg("A", 1.0, 1), Leg("B", 1.0, -1)], corr("A", "B", 1.0),
                    diversification_floor=-1.0)
        self.assertAlmostEqual(c.diversified, 0.0, places=6)


class TestUnknownCorrelationIsTreatedAsWorstCase(unittest.TestCase):
    def test_a_pair_with_no_history_gets_no_benefit(self):
        c = combine([Leg("A", 1.0), Leg("B", 1.0)], {})
        self.assertAlmostEqual(c.diversified, 2.0, places=6)
        self.assertEqual(c.pairs_missing_correlation, [("A", "B")])

    def test_each_missing_pair_is_reported_once_not_twice(self):
        c = combine([Leg("A", 1.0), Leg("B", 1.0), Leg("C", 1.0)], {})
        self.assertEqual(len(c.pairs_missing_correlation), 3)

    def test_a_one_sided_correlation_map_is_read_both_ways(self):
        c = combine([Leg("A", 1.0), Leg("B", 1.0)], {"A": {"B": 0.0}})
        self.assertEqual(c.pairs_missing_correlation, [])
        self.assertAlmostEqual(c.diversified, math.sqrt(2), places=6)


class TestTheThreeCeilings(unittest.TestCase):
    def test_a_well_diversified_book_is_admitted(self):
        ok, reason, _ = admits(Leg("D", 1.0), [Leg(x, 1.0) for x in "ABC"], ALL,
                               budget=2.5, gross_budget=10.0, stress_budget=5.0)
        self.assertTrue(ok, reason)

    def test_the_gross_ceiling_bounds_it_however_diversified(self):
        """The backstop for the correlation model simply being wrong."""
        ok, reason, _ = admits(Leg("F", 1.0), [Leg(x, 1.0) for x in "ABCDE"], ALL,
                               budget=99.0, gross_budget=5.0, stress_budget=99.0)
        self.assertFalse(ok)
        self.assertIn("PORTFOLIO_GROSS_RISK_LIMIT", reason)

    def test_the_stress_test_refuses_a_book_that_only_fits_while_calm(self):
        ok, reason, _ = admits(Leg("D", 1.0), [Leg(x, 1.0) for x in "ABC"], ALL,
                               budget=2.5, gross_budget=99.0, stress_budget=2.6)
        self.assertFalse(ok)
        self.assertIn("PORTFOLIO_STRESSED_RISK_LIMIT", reason)

    def test_the_ordinary_budget_still_refuses_a_correlated_book(self):
        ok, reason, _ = admits(Leg("B", 1.0), [Leg("A", 1.0)], corr("A", "B", 1.0),
                               budget=1.5, gross_budget=99.0, stress_budget=99.0)
        self.assertFalse(ok)
        self.assertIn("PORTFOLIO_OPEN_RISK_LIMIT", reason)

    def test_every_refusal_states_both_numbers(self):
        for kwargs in ({"gross_budget": 0.5}, {"budget": 0.5}, {"stress_budget": 0.5}):
            base = {"budget": 99.0, "gross_budget": 99.0, "stress_budget": 99.0, **kwargs}
            ok, reason, nums = admits(Leg("B", 1.0), [Leg("A", 1.0)], ALL, **base)
            with self.subTest(**kwargs):
                self.assertFalse(ok)
                self.assertIn(">", reason, "a refusal must show the figure and the limit")
                self.assertIn("gross", nums)

    def test_stress_uses_the_documented_default(self):
        self.assertEqual(DEFAULT_STRESS_CORRELATION, 0.8)


class TestMarginalRisk(unittest.TestCase):
    def test_adding_to_an_empty_book_charges_the_full_amount(self):
        self.assertAlmostEqual(marginal_risk(Leg("A", 1.0), [], {}), 1.0, places=6)

    def test_an_uncorrelated_addition_costs_less_than_its_own_risk(self):
        m = marginal_risk(Leg("B", 1.0), [Leg("A", 1.0)], corr("A", "B", 0.0))
        self.assertLess(m, 1.0)
        self.assertGreater(m, 0.0)

    def test_a_perfectly_correlated_addition_costs_its_full_risk(self):
        m = marginal_risk(Leg("B", 1.0), [Leg("A", 1.0)], corr("A", "B", 1.0))
        self.assertAlmostEqual(m, 1.0, places=6)

    def test_it_is_never_negative(self):
        m = marginal_risk(Leg("B", 1.0, -1), [Leg("A", 1.0, 1)], corr("A", "B", 1.0))
        self.assertGreaterEqual(m, 0.0)


class TestGrossModeIsUnchanged(unittest.TestCase):
    """The reservation check gained a branch. The old branch must behave exactly as it did.

    This is the test that matters most in this file: everything else adds capability, this one proves
    the capability is not switched on behind the operator's back."""

    def _reason(self, **kw):
        from app.storage.repo import _reservation_block_reason
        base = dict(trades=[], reservations=[], signal_id="s1", symbol="EURUSDm",
                    risk_money=1.0, max_total_risk=10.0, max_correlated_risk=5.0,
                    correlation_threshold=0.7, correlations={}, external_risk_by_symbol={})
        base.update(kw)
        return _reservation_block_reason(**base)

    def test_no_portfolio_cfg_behaves_as_before(self):
        self.assertIsNone(self._reason())

    def test_explicit_gross_mode_behaves_as_before(self):
        self.assertIsNone(self._reason(portfolio_cfg={"mode": "gross"}))

    def test_the_total_limit_still_refuses(self):
        self.assertEqual(self._reason(risk_money=20.0), "PORTFOLIO_OPEN_RISK_LIMIT")

    def test_missing_correlation_history_still_refuses_in_gross_mode(self):
        r = self._reason(external_risk_by_symbol={"XAUUSDm": 1.0})
        self.assertEqual(r, "CORRELATION_HISTORY_INSUFFICIENT:XAUUSDm")

    def test_the_correlated_group_limit_still_refuses(self):
        r = self._reason(external_risk_by_symbol={"XAUUSDm": 4.5},
                         correlations={"XAUUSDm": 0.95}, max_correlated_risk=5.0)
        self.assertEqual(r, "CORRELATED_OPEN_RISK_LIMIT")

    def test_diversified_mode_admits_what_gross_mode_refuses(self):
        """Two uncorrelated positions: gross charges both in full, diversified does not."""
        args = dict(external_risk_by_symbol={"XAUUSDm": 6.0}, correlations={"XAUUSDm": 0.0},
                    risk_money=6.0, max_total_risk=10.0)
        self.assertEqual(self._reason(**args), "PORTFOLIO_OPEN_RISK_LIMIT")
        self.assertIsNone(self._reason(**args, portfolio_cfg={
            "mode": "diversified", "gross_budget": 20.0, "stressed_risk_tolerance": 2.0}))

    def test_diversified_mode_still_refuses_a_correlated_book(self):
        args = dict(external_risk_by_symbol={"XAUUSDm": 6.0}, correlations={"XAUUSDm": 1.0},
                    risk_money=6.0, max_total_risk=10.0)
        r = self._reason(**args, portfolio_cfg={"mode": "diversified", "gross_budget": 20.0,
                                                "stressed_risk_tolerance": 2.0})
        self.assertIsNotNone(r)
        self.assertIn("PORTFOLIO_OPEN_RISK_LIMIT", r)


class TestConfigDefaultsAreSafe(unittest.TestCase):
    def test_the_new_mode_is_off_by_default(self):
        """It changes live trade behaviour and restarts the record, so it must be opt-in."""
        self.assertEqual(RiskCfg().portfolio_risk_mode, "gross")

    def test_the_gross_ceiling_sits_above_the_ordinary_budget(self):
        r = RiskCfg()
        self.assertGreaterEqual(r.max_gross_open_risk_fraction, r.max_total_open_risk_fraction)

    def test_a_gross_ceiling_below_the_budget_is_rejected(self):
        with self.assertRaises(Exception):
            RiskCfg(max_total_open_risk_fraction=0.10, max_gross_open_risk_fraction=0.05)

    def test_unknown_pairs_default_to_fully_correlated(self):
        self.assertEqual(RiskCfg().missing_correlation_value, 1.0)

    def test_the_floor_defaults_to_no_hedge_credit(self):
        self.assertEqual(RiskCfg().diversification_floor, 0.0)


if __name__ == "__main__":
    unittest.main()
