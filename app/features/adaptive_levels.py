"""Thresholds that track an indicator's own recent behaviour instead of standing still.

Most of this system's thresholds are already live: `stop_atr: 2.0` means two CURRENT ATRs, `bb_k: 2.0` means two
current standard deviations, `minimum_relative_volume: 1.0` means the last 20 bars' own average. Those cannot go
stale, because the thing they multiply IS the market condition.

Four were raw indicator levels that never moved: pullback's `h1_adx_min` (15) and `rsi_reclaim` (50), and
mean-reversion's `h1_adx_max` (18) and `rsi_extreme` (30). A raw ADX level means different things in different
volatility regimes - in a quiet year 15 may sit at the 80th percentile of its own distribution and gate almost
everything out; in a wild year at the 25th and let almost everything through. Same number, opposite behaviour.

WHY A ROLLING MEAN RATHER THAN A PERCENTILE. A percentile threshold ("top 40% of ADX") always admits the same
SHARE of bars, so it can never say "conditions are not there today" - it manufactures signals in regimes where
no level has an edge. A multiple of the indicator's own rolling centre keeps that ability: when the whole
distribution is low, the adaptive level is low in absolute terms but a bar still has to beat it. This also
matches how the rest of the codebase already expresses live thresholds.

EVERY ADAPTIVE LEVEL IS BOUNDED. The configured number survives as a floor and a ceiling, so an adaptive level
can shift with the regime but can never wander somewhere absurd on a thin or broken window. With the multiple
set to 0 the functions return the configured constant and behaviour is bit-for-bit what it was before.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

MIN_SAMPLES = 50          # below this a rolling statistic is noise; fall back to the configured constant


def _window(series, i: int, lookback: int) -> Optional[np.ndarray]:
    """The finite values of `series` over the `lookback` bars ending at i, or None if too few to trust."""
    if series is None or i is None or i < 0:
        return None
    arr = np.asarray(series, dtype=float)
    if not len(arr):
        return None
    lo = max(0, int(i) - int(lookback) + 1)
    w = arr[lo:int(i) + 1]
    w = w[np.isfinite(w)]
    return w if len(w) >= MIN_SAMPLES else None


def adaptive_level(series, i: int, *, configured: float, multiple: float, lookback: int,
                   low: float, high: float) -> float:
    """`multiple` x the indicator's own rolling mean, clamped to [low, high].

    multiple <= 0 returns `configured` unchanged, which is the off switch and the default.
    Too little history, or a window that is all NaN, also returns `configured` - an adaptive threshold must
    never be LESS reliable than the constant it replaces.
    """
    if multiple is None or multiple <= 0:
        return float(configured)
    w = _window(series, i, lookback)
    if w is None:
        return float(configured)
    centre = float(np.mean(w))
    if not np.isfinite(centre):
        return float(configured)
    return float(min(max(multiple * centre, low), high))


def adaptive_midline(series, i: int, *, configured: float, enabled: bool, lookback: int,
                     low: float, high: float) -> float:
    """The indicator's own rolling MEDIAN, for a threshold that is a centre rather than a strength.

    RSI's 50 is the definitional midline between upward and downward momentum, so a multiple of the mean is the
    wrong shape for it. What does drift is where RSI actually sits: in a persistently trending market its own
    centre moves away from 50, and "price reclaimed the midline" should mean the centre that market is
    currently trading around.
    """
    if not enabled:
        return float(configured)
    w = _window(series, i, lookback)
    if w is None:
        return float(configured)
    centre = float(np.median(w))
    if not np.isfinite(centre):
        return float(configured)
    return float(min(max(centre, low), high))


def adaptive_extreme(series, i: int, *, configured: float, sigma: float, lookback: int,
                     low: float, high: float) -> float:
    """A level `sigma` standard deviations BELOW the indicator's own rolling centre.

    For "oversold" the fixed 30 is a distance from the midline, so the live form is a distance measured in that
    indicator's own recent dispersion: in a calm market RSI rarely reaches 30 and the rule never fires, while in
    a violent one it reaches 30 constantly and the rule fires on noise. Returns the LOW side; callers mirror it
    for the high side exactly as they already mirror the constant (100 - rsi_extreme).
    """
    if sigma is None or sigma <= 0:
        return float(configured)
    w = _window(series, i, lookback)
    if w is None:
        return float(configured)
    centre, spread = float(np.median(w)), float(np.std(w))
    if not np.isfinite(centre) or not np.isfinite(spread) or spread <= 0:
        return float(configured)
    return float(min(max(centre - sigma * spread, low), high))
