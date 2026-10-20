"""Causal price-action context from confirmed pivots and closed bars."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.features.frame import FeatureFrame


@dataclass(frozen=True)
class MarketContext:
    trend: str
    sweep: str
    sweep_level: float | None
    fvg: str
    fvg_zone: tuple[float, float] | None
    activity_spike: bool
    score: int
    score_parts: dict
    reasons: tuple[str, ...]


def _confirmed_pivots(frame: FeatureFrame, index: int, pivot_n: int) -> tuple[list[int], list[int]]:
    if index < pivot_n * 2:
        return [], []
    highs, lows = frame.pivots(pivot_n)
    end = max(0, index - pivot_n + 1)
    return (np.flatnonzero(highs[:end]).tolist(),
            np.flatnonzero(lows[:end]).tolist())


def _structure_trend(frame: FeatureFrame, index: int, pivot_n: int) -> str:
    highs, lows = _confirmed_pivots(frame, index, pivot_n)
    if len(highs) >= 2 and len(lows) >= 2:
        higher_highs = frame.h[highs[-1]] > frame.h[highs[-2]]
        higher_lows = frame.l[lows[-1]] > frame.l[lows[-2]]
        lower_highs = frame.h[highs[-1]] < frame.h[highs[-2]]
        lower_lows = frame.l[lows[-1]] < frame.l[lows[-2]]
        if higher_highs and higher_lows:
            return "BULLISH"
        if lower_highs and lower_lows:
            return "BEARISH"
    if highs and frame.c[index] > frame.h[highs[-1]]:
        return "BULLISH"
    if lows and frame.c[index] < frame.l[lows[-1]]:
        return "BEARISH"
    return "RANGING"


def _liquidity_sweep(m15: FeatureFrame, m15_index: int, m1: FeatureFrame, m1_index: int,
                     pivot_n: int, lookback: int, wick_ratio: float) -> tuple[str, float | None]:
    highs, lows = _confirmed_pivots(m15, m15_index, pivot_n)
    if not highs or not lows or m1_index < 1:
        return "NONE", None
    start = max(0, m1_index - lookback + 1)
    if m1.t[m1_index] - m1.t[start] > lookback * 60:
        start = int(np.searchsorted(m1.t, m1.t[m1_index] - lookback * 60, side="left"))
    candidates = []
    low_level = float(m15.l[lows[-1]])
    high_level = float(m15.h[highs[-1]])
    for j in range(start, m1_index + 1):
        body = abs(float(m1.c[j] - m1.o[j]))
        lower_wick = min(m1.o[j], m1.c[j]) - m1.l[j]
        upper_wick = m1.h[j] - max(m1.o[j], m1.c[j])
        if m1.l[j] < low_level and m1.c[j] > low_level and lower_wick >= max(body * wick_ratio, 0.0):
            candidates.append((int(m1.t[j]), "BULLISH", low_level))
        if m1.h[j] > high_level and m1.c[j] < high_level and upper_wick >= max(body * wick_ratio, 0.0):
            candidates.append((int(m1.t[j]), "BEARISH", high_level))
    if not candidates:
        return "NONE", None
    _, direction, level = max(candidates)
    return direction, level


def _fvg_retest(frame: FeatureFrame, index: int, lookback: int, min_width: float) -> tuple[
        str, tuple[float, float] | None]:
    if index < 3:
        return "NONE", None
    start = max(2, index - lookback)
    for formed in range(index - 1, start - 1, -1):
        bull_low = float(frame.h[formed - 2])
        bull_high = float(frame.l[formed])
        if bull_high - bull_low >= min_width:
            midpoint = (bull_low + bull_high) / 2.0
            if (frame.l[index] <= bull_high and frame.h[index] >= bull_low
                    and frame.c[index] >= midpoint):
                return "BULLISH", (bull_low, bull_high)
        bear_low = float(frame.h[formed])
        bear_high = float(frame.l[formed - 2])
        if bear_high - bear_low >= min_width:
            midpoint = (bear_low + bear_high) / 2.0
            if (frame.h[index] >= bear_low and frame.l[index] <= bear_high
                    and frame.c[index] <= midpoint):
                return "BEARISH", (bear_low, bear_high)
    return "NONE", None


def analyze_market_context(m15: FeatureFrame, m15_index: int, h1: FeatureFrame, h1_index: int,
                           m1: FeatureFrame, m1_index: int, *, spread_safe: bool,
                           score_threshold: int = 85, pivot_n: int = 2, sweep_lookback: int = 15,
                           fvg_lookback: int = 15, min_wick_ratio: float = 1.2,
                           activity_atr_multiple: float = 1.2, activity_volume_ratio: float = 1.2,
                           minimum_fvg_atr: float = 0.05) -> MarketContext:
    trend = _structure_trend(h1, h1_index, pivot_n)
    sweep, sweep_level = _liquidity_sweep(m15, m15_index, m1, m1_index, pivot_n,
                                          sweep_lookback, min_wick_ratio)
    atr = m1.atr(14)
    atr_now = float(atr[m1_index]) if m1_index < len(atr) else float("nan")
    min_fvg_width = minimum_fvg_atr * atr_now if np.isfinite(atr_now) else float("inf")
    fvg, zone = _fvg_retest(m1, m1_index, fvg_lookback, min_fvg_width)
    previous_volumes = m1.tick_volume[max(0, m1_index - 20):m1_index]
    median_volume = float(np.median(previous_volumes)) if len(previous_volumes) else 0.0
    current_range = float(m1.h[m1_index] - m1.l[m1_index])
    activity_spike = bool(np.isfinite(atr_now) and atr_now > 0 and
                          current_range >= activity_atr_multiple * atr_now and
                          median_volume > 0 and m1.tick_volume[m1_index] >= activity_volume_ratio * median_volume)

    score_parts = {
        "higher_timeframe_structure": 30 if trend in ("BULLISH", "BEARISH") else 0,
        "sweep_and_fvg_retest": 25 if sweep != "NONE" and sweep == fvg else 0,
        "range_and_tick_volume_expansion": 20 if activity_spike else 0,
        "spread_within_safe_range": 25 if spread_safe else 0,
    }
    score = sum(score_parts.values())
    reasons = []
    if trend == "RANGING":
        reasons.append("NO_CLEAR_HIGHER_TIMEFRAME_STRUCTURE")
    if not spread_safe:
        reasons.append("SPREAD_NOT_WITHIN_SAFE_RANGE")
    if score < score_threshold:
        reasons.append(f"CONFLUENCE_SCORE_{score}_BELOW_{score_threshold}")
    return MarketContext(trend, sweep, sweep_level, fvg, zone, activity_spike, score, score_parts, tuple(reasons))
