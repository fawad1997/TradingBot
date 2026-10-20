import unittest
from unittest.mock import patch

import numpy as np

from tests import helpers  # noqa: F401
from app.features.frame import FeatureFrame
from app.market.structure import (
    _fvg_retest,
    _liquidity_sweep,
    _structure_trend,
    analyze_market_context,
)


def frame(high, low, close=None, *, start=1_700_000_000, seconds=60, volume=None):
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close if close is not None else (high + low) / 2, dtype=float)
    open_ = close.copy()
    n = len(high)
    return FeatureFrame(
        start + np.arange(n) * seconds, open_, high, low, close,
        tick_volume=volume if volume is not None else np.full(n, 100),
    )


class TestMarketStructure(unittest.TestCase):
    def test_structure_uses_only_confirmed_pivots(self):
        high = [2, 3, 2.4, 2.8, 4, 2.7, 3.2, 4.5, 3.1, 3.4]
        low = [1.5, 2, 1.2, 2.1, 2.0, 2.5, 2.2, 3, 2.8, 2.7]
        full = frame(high, low)
        partial = frame(high[:7], low[:7])
        self.assertEqual(_structure_trend(full, 6, 1), _structure_trend(partial, 6, 1))
        self.assertEqual(_structure_trend(full, 6, 1), "BULLISH")

    def test_liquidity_sweep_requires_wick_rejection(self):
        m15 = frame([11, 10, 12, 10, 11, 13], [10, 9, 11, 8, 10, 12], seconds=900)
        m1 = frame([9, 9, 9], [8.5, 8.5, 7.8], [8.8, 8.8, 8.6])
        direction, level = _liquidity_sweep(m15, 5, m1, 2, 1, 15, 1.2)
        self.assertEqual(direction, "BULLISH")
        self.assertEqual(level, 8)

    def test_fvg_is_recognized_only_when_retested(self):
        high = [101, 101, 101, 100, 101, 104, 103]
        low = [99, 99, 99, 99, 101, 102, 101.5]
        close = [100, 100, 100, 99.5, 102, 103, 102.5]
        bars = frame(high, low, close)
        direction, zone = _fvg_retest(bars, 6, 5, 1)
        self.assertEqual(direction, "BULLISH")
        self.assertEqual(zone, (100, 102))
        direction, _ = _fvg_retest(bars, 5, 5, 1)
        self.assertEqual(direction, "NONE")

    def test_trend_and_safe_spread_allow_modest_confluence_setup(self):
        bars = frame(np.full(40, 101), np.full(40, 99), np.full(40, 100))
        with patch("app.market.structure._structure_trend", return_value="BULLISH"), \
                patch("app.market.structure._liquidity_sweep", return_value=("NONE", None)), \
                patch("app.market.structure._fvg_retest", return_value=("NONE", None)):
            context = analyze_market_context(bars, 39, bars, 39, bars, 39, spread_safe=True,
                                             score_threshold=55)
        self.assertEqual(context.score, 55)
        self.assertEqual(context.reasons, ())
        self.assertFalse(context.activity_spike)

    def test_unsafe_spread_still_blocks_modest_setup(self):
        bars = frame(np.full(40, 101), np.full(40, 99), np.full(40, 100))
        with patch("app.market.structure._structure_trend", return_value="BULLISH"), \
                patch("app.market.structure._liquidity_sweep", return_value=("NONE", None)), \
                patch("app.market.structure._fvg_retest", return_value=("NONE", None)):
            context = analyze_market_context(bars, 39, bars, 39, bars, 39, spread_safe=False,
                                             score_threshold=55)
        self.assertLess(context.score, 55)
        self.assertIn("SPREAD_NOT_WITHIN_SAFE_RANGE", context.reasons)


if __name__ == "__main__":
    unittest.main()
