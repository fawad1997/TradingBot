"""The bot's REAL micro_scalper entry logic, replayed over history so VectorBT, Backtrader and the fast simulator test
the same entries.

Uses the production functions (candlestick trigger, higher-timeframe direction confirmation, relative tick volume).
The H4 macro channel is NOT replayed (history for it is not available offline), so these
results describe the raw entry + exit behaviour, which is exactly what exit/stop research needs.

`trigger_candidates` returns every candle trigger with its relative volume BEFORE the volume threshold is applied, so
a parameter search can vary the threshold without re-running the slow candle scan.
"""
from __future__ import annotations

import numpy as np

from app.core.types import Side
from app.features.frame import FeatureFrame
from app.strategies.entry_generator import confirm_relative_volume, detect_candlestick_trigger


def trigger_candidates(trig: FeatureFrame, confirm: FeatureFrame, confirm_seconds: int, trig_seconds: int,
                       lookback: int = 20) -> dict:
    """Candle triggers on `trig` that the last CLOSED `confirm` candle agrees with.
    Returns long/short masks (before the volume filter), ATR and relative tick volume per bar."""
    n = len(trig)
    longs = np.zeros(n, dtype=bool)
    shorts = np.zeros(n, dtype=bool)
    rv = np.full(n, np.nan)
    atr = np.asarray(trig.atr(14), dtype=float)
    # last confirm candle CLOSED at the moment each trigger candle closes
    ic = np.searchsorted(confirm.t + confirm_seconds, trig.t + trig_seconds, side="right") - 1
    for j in range(max(20, lookback), n):
        t = detect_candlestick_trigger(trig, j)
        if t is None:
            continue
        side, _ = t
        i = int(ic[j])
        if i < 0 or not (np.isfinite(confirm.o[i]) and np.isfinite(confirm.c[i])):
            continue
        if (side is Side.BUY and confirm.c[i] < confirm.o[i]) or (side is Side.SELL and confirm.c[i] > confirm.o[i]):
            continue
        if not np.isfinite(atr[j]) or atr[j] <= 0:
            continue
        r = confirm_relative_volume(trig, j, lookback, 1e-12)
        if r is None:
            continue
        rv[j] = r
        (longs if side is Side.BUY else shorts)[j] = True
    return {"long": longs, "short": shorts, "atr": atr, "rv": rv}


def apply_volume(cands: dict, min_rv: float) -> dict:
    ok = np.nan_to_num(cands["rv"], nan=-1.0) >= float(min_rv)
    return {"long": cands["long"] & ok, "short": cands["short"] & ok, "atr": cands["atr"]}


def micro_signals(m1: FeatureFrame, m15: FeatureFrame, params: dict) -> dict:
    """micro_scalper on M1 with M15 confirmation, at the live volume threshold."""
    cands = trigger_candidates(m1, m15, 900, 60, int(params.get("volume_lookback", 20)))
    return apply_volume(cands, float(params.get("minimum_relative_volume", 1.2)))
