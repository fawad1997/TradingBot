"""The four thresholds that were raw indicator levels can now track the market - bounded, and off by default.

39 of the 43 strategy thresholds were already live: a multiple of the CURRENT ATR, of a current standard
deviation, or of a rolling average. Four were raw levels that never moved - pullback's h1_adx_min (15) and
rsi_reclaim (50), mean-reversion's h1_adx_max (18) and rsi_extreme (30) - and those are the ones that change
meaning with the volatility regime.

The properties that matter more than the feature itself:
  * with the setting off, the result is the configured constant, exactly
  * every adaptive level is clamped by the configured number on one side
  * too little history falls back to the constant, because an adaptive threshold must never be LESS reliable
    than the one it replaces
  * a rolling MEAN, not a percentile: a percentile admits a fixed share of bars and so can never say
    "conditions are not there today"
"""
import unittest

import numpy as np

from app.config import ConfigManager
from app.features.adaptive_levels import (MIN_SAMPLES, adaptive_extreme, adaptive_level, adaptive_midline)


def flat(value, n=400):
    return np.full(n, float(value))


class TestOffByDefault(unittest.TestCase):
    def test_multiple_zero_returns_the_constant(self):
        self.assertEqual(adaptive_level(flat(25.0), 399, configured=15.0, multiple=0.0,
                                        lookback=500, low=15.0, high=40.0), 15.0)

    def test_disabled_midline_returns_the_constant(self):
        self.assertEqual(adaptive_midline(flat(70.0), 399, configured=50.0, enabled=False,
                                          lookback=500, low=40.0, high=60.0), 50.0)

    def test_sigma_zero_returns_the_constant(self):
        self.assertEqual(adaptive_extreme(flat(50.0), 399, configured=30.0, sigma=0.0,
                                          lookback=500, low=15.0, high=45.0), 30.0)

    def test_the_shipped_config_has_them_all_off(self):
        """They ship off so the change is measurable before it changes behaviour."""
        by = {s.id: s.param_values() for s in ConfigManager().strategies.strategies}
        self.assertEqual(by["pullback_v1"]["h1_adx_relative"], 0.0)
        self.assertFalse(by["pullback_v1"]["rsi_reclaim_adaptive"])
        self.assertEqual(by["mean_reversion_v1"]["h1_adx_relative"], 0.0)
        self.assertEqual(by["mean_reversion_v1"]["rsi_extreme_sigma"], 0.0)


class TestItTracksTheMarket(unittest.TestCase):
    def test_a_strong_regime_raises_the_bar(self):
        """ADX averaging 30 means 15 is nothing special there; the level rises to 30."""
        self.assertAlmostEqual(adaptive_level(flat(30.0), 399, configured=15.0, multiple=1.0,
                                              lookback=500, low=15.0, high=40.0), 30.0, places=6)

    def test_a_quiet_regime_cannot_drop_below_the_configured_floor(self):
        """ADX averaging 8 would give 8; the configured 15 is the floor, so it stays 15."""
        self.assertAlmostEqual(adaptive_level(flat(8.0), 399, configured=15.0, multiple=1.0,
                                              lookback=500, low=15.0, high=40.0), 15.0, places=6)

    def test_the_ceiling_bounds_a_runaway_regime(self):
        self.assertAlmostEqual(adaptive_level(flat(500.0), 399, configured=15.0, multiple=1.0,
                                              lookback=500, low=15.0, high=40.0), 40.0, places=6)

    def test_the_midline_follows_where_rsi_actually_sits(self):
        self.assertAlmostEqual(adaptive_midline(flat(57.0), 399, configured=50.0, enabled=True,
                                                lookback=500, low=40.0, high=60.0), 57.0, places=6)

    def test_the_midline_stays_a_midline(self):
        for v, want in ((90.0, 60.0), (10.0, 40.0)):
            self.assertAlmostEqual(adaptive_midline(flat(v), 399, configured=50.0, enabled=True,
                                                    lookback=500, low=40.0, high=60.0), want, places=6)

    def test_the_extreme_is_measured_in_the_markets_own_dispersion(self):
        rng = np.random.default_rng(7)
        series = 50.0 + rng.normal(0, 10.0, 600)
        level = adaptive_extreme(series, 599, configured=30.0, sigma=1.0, lookback=500, low=15.0, high=45.0)
        self.assertGreater(level, 35.0)      # centre 50, one sigma 10 -> about 40
        self.assertLess(level, 45.0)

    def test_a_flat_series_has_no_dispersion_and_falls_back(self):
        self.assertEqual(adaptive_extreme(flat(50.0), 399, configured=30.0, sigma=1.0,
                                          lookback=500, low=15.0, high=45.0), 30.0)


class TestItNeverBecomesLessReliableThanTheConstant(unittest.TestCase):
    def test_too_little_history_falls_back(self):
        short = flat(30.0, MIN_SAMPLES - 1)
        self.assertEqual(adaptive_level(short, len(short) - 1, configured=15.0, multiple=1.0,
                                        lookback=500, low=15.0, high=40.0), 15.0)

    def test_an_all_nan_window_falls_back(self):
        nans = np.full(400, np.nan)
        self.assertEqual(adaptive_level(nans, 399, configured=15.0, multiple=1.0,
                                        lookback=500, low=15.0, high=40.0), 15.0)

    def test_nans_are_ignored_rather_than_poisoning_the_mean(self):
        series = flat(30.0)
        series[:100] = np.nan
        self.assertAlmostEqual(adaptive_level(series, 399, configured=15.0, multiple=1.0,
                                              lookback=500, low=15.0, high=40.0), 30.0, places=6)

    def test_an_empty_or_missing_series_falls_back(self):
        for s in (None, np.array([])):
            self.assertEqual(adaptive_level(s, 10, configured=15.0, multiple=1.0,
                                            lookback=500, low=15.0, high=40.0), 15.0)

    def test_a_negative_index_falls_back(self):
        self.assertEqual(adaptive_level(flat(30.0), -1, configured=15.0, multiple=1.0,
                                        lookback=500, low=15.0, high=40.0), 15.0)

    def test_the_level_only_ever_looks_backwards(self):
        """A threshold that sees future bars would make every backtest meaningless."""
        series = flat(10.0, 600)
        series[500:] = 90.0                       # a regime change AFTER the bar being judged
        at_499 = adaptive_level(series, 499, configured=5.0, multiple=1.0, lookback=400, low=0.0, high=100.0)
        self.assertAlmostEqual(at_499, 10.0, places=6)


class TestStrategiesStillBuildAndRun(unittest.TestCase):
    def test_both_strategies_evaluate_with_the_settings_on(self):
        from app.research.lab.data import resample
        from app.features.frame import FeatureFrame
        from app.strategies.base import StrategyView
        from app.strategies.registry import build
        rng = np.random.default_rng(3)
        n = 40000
        t = 1_700_000_000 // 3600 * 3600 + np.arange(n) * 60
        c = 2000 + np.cumsum(rng.normal(0, 0.5, n))
        o = np.r_[c[0], c[:-1]]
        m1 = FeatureFrame(t, o, np.maximum(o, c) + 0.3, np.minimum(o, c) - 0.3, c, np.full(n, 20.0),
                          rng.integers(50, 200, n).astype(float), "M1")
        m15, h1 = resample(m1, 900)[0], resample(m1, 3600)[0]
        cfgm = ConfigManager()
        idx = np.searchsorted(np.asarray(h1.t, np.int64) + 3600,
                              np.asarray(m15.t, np.int64) + 900, side="right") - 1
        for sid, ov in (("pullback_v1", {"h1_adx_relative": 1.0, "rsi_reclaim_adaptive": True}),
                        ("mean_reversion_v1", {"h1_adx_relative": 1.0, "rsi_extreme_sigma": 1.0})):
            s = build(cfgm.strategy(sid), param_overrides=ov)
            for i in range(700, 900):
                k = int(idx[i])
                if k < 60:
                    continue
                mid = float(m15.c[i])
                res = s.evaluate(StrategyView(m15=m15, i=i, h1=h1, k=k, symbol="XAUUSDm", point=0.01,
                                              spread_price=0.2, bid=mid - 0.1, ask=mid + 0.1,
                                              spread_safe=True, session="LONDON", regime_trend="UNKNOWN"))
                self.assertTrue(res.plan is not None or res.reasons, sid)


if __name__ == "__main__":
    unittest.main()
