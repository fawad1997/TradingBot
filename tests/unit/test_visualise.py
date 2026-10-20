"""The signal visualiser must show what the bot would REALLY have traded, not a flattering version.

Its whole value is that you can trust what you are looking at. Three things have to hold:

  * stops and targets are sized exactly as the live CandidateStrategy sizes them (ATR, the ten-spread floor)
  * outcomes are simulated pessimistically - when one bar touches both the stop and the target, the stop wins -
    and costs are charged, so a losing rule cannot look like a winning one
  * the rule is the SAME function the lab scores and the live strategy calls, so a chart can never disagree
    with the research that follows it

The arithmetic is pinned here because a chart that quietly flatters a rule is worse than no chart: it would
make a losing strategy look worth promoting.
"""
import unittest

import numpy as np

from app.features.frame import FeatureFrame
from app.research.lab.hypotheses import HYPOTHESES
from app.research.visualise import build_view, outcome


def frame(closes, spread=0.1):
    """A frame whose highs and lows straddle each close, so a walk-forward can reach either side."""
    c = np.asarray(closes, float)
    o = np.r_[c[0], c[:-1]]
    t = np.arange(len(c), dtype=np.int64) * 900 + 1_700_000_000
    return FeatureFrame(t, o, np.maximum(o, c) + 0.5, np.minimum(o, c) - 0.5, c,
                        np.full(len(c), spread), np.full(len(c), 100.0), "M15")


def market(n=4000, seed=5):
    rng = np.random.default_rng(seed)
    c = 2000 + np.cumsum(rng.normal(0, 0.5, n))
    o = np.r_[c[0], c[:-1]]
    t = np.arange(n, dtype=np.int64) * 900 + 1_700_000_000
    m15 = FeatureFrame(t, o, np.maximum(o, c) + 0.3, np.minimum(o, c) - 0.3, c,
                       np.full(n, 6.0), rng.integers(50, 200, n).astype(float), "M15")
    h1t = np.arange(n // 4, dtype=np.int64) * 3600 + 1_700_000_000
    hc = c[::4][:len(h1t)]
    ho = np.r_[hc[0], hc[:-1]]
    h1 = FeatureFrame(h1t, ho, np.maximum(ho, hc) + 0.6, np.minimum(ho, hc) - 0.6, hc,
                      np.full(len(h1t), 6.0), np.full(len(h1t), 400.0), "H1")
    return m15, h1


class TestOutcomeIsHonest(unittest.TestCase):
    def test_a_target_hit_pays_the_reward_minus_cost(self):
        f = frame([100.0] + [100.0] * 3 + [130.0] * 5)
        r = outcome(f, 0, 1, entry=100.0, stop_dist=10.0, rr=1.5, spread=0.1, slippage=0.0)
        self.assertEqual(r["exit"], "target")
        self.assertAlmostEqual(r["r"], 1.5 - 0.1 / 10.0, places=6)

    def test_a_stop_hit_costs_more_than_one_r(self):
        """-1R plus slippage plus cost: a loser must never be recorded as exactly -1."""
        f = frame([100.0] + [80.0] * 5)
        r = outcome(f, 0, 1, entry=100.0, stop_dist=10.0, rr=1.5, spread=0.1, slippage=0.5)
        self.assertEqual(r["exit"], "stop")
        self.assertLess(r["r"], -1.0)
        self.assertAlmostEqual(r["r"], -1.0 - 0.5 / 10.0 - 0.1 / 10.0, places=6)

    def test_when_one_bar_touches_both_the_stop_wins(self):
        """The pessimistic rule. Without it every ambiguous bar would be scored as a win."""
        c = np.array([100.0, 100.0])
        o = np.array([100.0, 100.0])
        t = np.array([0, 900], dtype=np.int64)
        both = FeatureFrame(t, o, np.array([100.0, 200.0]), np.array([100.0, 50.0]), c,
                            np.full(2, 0.1), np.full(2, 1.0), "M15")
        r = outcome(both, 0, 1, entry=100.0, stop_dist=10.0, rr=1.5, spread=0.0, slippage=0.0)
        self.assertEqual(r["exit"], "stop")

    def test_an_unresolved_signal_closes_at_market_not_at_the_target(self):
        f = frame([100.0] * 40)
        r = outcome(f, 0, 1, entry=100.0, stop_dist=10.0, rr=1.5, spread=0.0, slippage=0.0, max_hold=5)
        self.assertEqual(r["exit"], "timeout")
        self.assertAlmostEqual(r["r"], 0.0, places=6)

    def test_the_short_side_mirrors_the_long_side(self):
        up = frame([100.0] + [130.0] * 5)
        down = frame([100.0] + [70.0] * 5)
        long_win = outcome(up, 0, 1, 100.0, 10.0, 1.5, 0.0, 0.0)
        short_win = outcome(down, 0, -1, 100.0, 10.0, 1.5, 0.0, 0.0)
        self.assertEqual(long_win["exit"], "target")
        self.assertEqual(short_win["exit"], "target")
        self.assertAlmostEqual(long_win["r"], short_win["r"], places=6)

    def test_mfe_is_recorded_even_for_a_loser(self):
        """The column that showed your real losers reach only +0.04R before dying."""
        f = frame([100.0, 105.0, 80.0, 80.0])
        r = outcome(f, 0, 1, entry=100.0, stop_dist=10.0, rr=5.0, spread=0.0, slippage=0.0)
        self.assertEqual(r["exit"], "stop")
        self.assertGreater(r["mfe_r"], 0.0)


class TestViewMatchesTheLiveStrategy(unittest.TestCase):
    def test_the_ten_spread_floor_is_applied_exactly_as_live(self):
        m15, h1 = market()
        wide = build_view("vwap_reversion", m15, h1, {"min_dev_atr": 2.0, "warmup_bars": 20},
                          spread=5.0, min_stop_spread_multiple=10.0, stop_atr=2.0)
        for mk in wide["markers"]:
            self.assertGreaterEqual(abs(mk["entry"] - mk["stop"]), 10.0 * 5.0 - 1e-6)

    def test_target_distance_is_reward_risk_times_the_stop(self):
        m15, h1 = market()
        v = build_view("vwap_reversion", m15, h1, {"min_dev_atr": 2.0, "warmup_bars": 20},
                       spread=0.06, reward_risk=1.5)
        self.assertTrue(v["markers"])
        for mk in v["markers"]:
            stop = abs(mk["entry"] - mk["stop"])
            self.assertAlmostEqual(abs(mk["target"] - mk["entry"]) / stop, 1.5, places=5)

    def test_every_rule_can_be_charted(self):
        m15, h1 = market()
        for rule in HYPOTHESES:
            v = build_view(rule, m15, h1, {}, spread=0.06)
            self.assertEqual(v["stats"]["rule"], rule)
            self.assertEqual(len(v["bars"]), len(m15))
            for key in ("signals", "per_week", "total_r", "avg_r", "win_rate"):
                self.assertIn(key, v["stats"])

    def test_an_unknown_rule_is_refused_by_name(self):
        m15, h1 = market()
        with self.assertRaises(ValueError) as e:
            build_view("not_a_rule", m15, h1, {}, spread=0.06)
        self.assertIn("not_a_rule", str(e.exception))

    def test_markers_sit_on_real_bars_and_carry_their_outcome(self):
        m15, h1 = market()
        v = build_view("vwap_reversion", m15, h1, {"min_dev_atr": 2.0, "warmup_bars": 20}, spread=0.06)
        times = {b["time"] for b in v["bars"]}
        for mk in v["markers"]:
            self.assertIn(mk["time"], times)
            self.assertIn(mk["side"], ("long", "short"))
            self.assertIn(mk["exit"], ("stop", "target", "timeout"))
            self.assertEqual(mk["won"], mk["r"] > 0)

    def test_the_headline_numbers_match_the_markers(self):
        """The stats panel must be derived from the same signals drawn on the chart."""
        m15, h1 = market()
        v = build_view("momentum_trend", m15, h1, {}, spread=0.06)
        rs = [m["r"] for m in v["markers"]]
        self.assertEqual(v["stats"]["signals"], len(rs))
        if rs:
            self.assertAlmostEqual(v["stats"]["total_r"], round(sum(rs), 2), places=2)
            self.assertAlmostEqual(v["stats"]["win_rate"], round(sum(r > 0 for r in rs) / len(rs), 3), places=3)

    def test_a_wider_spread_can_only_make_a_rule_look_worse(self):
        """A chart that flatters a rule as costs rise would be worse than no chart."""
        m15, h1 = market()
        p = {"min_dev_atr": 2.0, "warmup_bars": 20}
        cheap = build_view("vwap_reversion", m15, h1, p, spread=0.02, min_stop_spread_multiple=0.0)
        dear = build_view("vwap_reversion", m15, h1, p, spread=2.00, min_stop_spread_multiple=0.0)
        self.assertLessEqual(dear["stats"]["avg_r"], cheap["stats"]["avg_r"] + 1e-9)


class TestRenderedPage(unittest.TestCase):
    def test_the_page_is_self_contained_and_pins_its_chart_library(self):
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "visrule", Path(__file__).resolve().parents[2] / "tools" / "visualise_rule.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        m15, h1 = market()
        view = build_view("vwap_reversion", m15, h1, {"min_dev_atr": 2.0, "warmup_bars": 20}, spread=0.06)
        page = mod.render(view, "t", "s", "f")
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertNotRegex(page, r"__[A-Z]+__")                 # no unreplaced placeholders
        self.assertIn("lightweight-charts@4.1.3", page)          # pinned, not floating
        self.assertIn("const DATA =", page)


if __name__ == "__main__":
    unittest.main()
