"""Candidate entry rules, as RESEARCH ONLY: nothing here can place an order.

Each function turns price history into long/short entry masks, exactly as `signals.py` does for the live
micro_scalper, so the same machinery scores them: the fast simulator's stop/target grid, the real spread and
commission, the live minimum-stop rule, and Optuna walk-forward. A rule only becomes a live strategy after it
passes (see the roadmap's gate C), and a rule that is written here has still proved nothing.

Causality: a signal on bar j uses only bars up to and including j, which has CLOSED; the simulator fills it at the
open of bar j+1. Every lookback is therefore shifted by one bar.

The five rules:
  breakout_retest   break of a prior range, then a retest that holds
  momentum_trend    trend by two EMAs + volatility-adjusted momentum, entered on a pullback through the fast EMA
  session_breakout  break of one session's range during the next session (one trade per side per day)
  vwap_reversion    a stretch away from the session VWAP that starts to come back
  htf_continuation  higher-timeframe trend, entered on a pullback to the trigger timeframe's EMA
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

import numpy as np

from app.features.frame import FeatureFrame


# ------------------------------------------------------------------ helpers (all causal)
def _prior_window(x: np.ndarray, n: int, fn) -> np.ndarray:
    """fn over the n bars BEFORE each bar (never includes the bar itself)."""
    out = np.full(len(x), np.nan)
    if len(x) > n >= 1:
        w = np.lib.stride_tricks.sliding_window_view(np.asarray(x, float), n)
        out[n:] = fn(w[:-1], axis=1)
    return out


def prior_high(f: FeatureFrame, n: int) -> np.ndarray:
    return _prior_window(f.h, n, np.max)


def prior_low(f: FeatureFrame, n: int) -> np.ndarray:
    return _prior_window(f.l, n, np.min)


def _atr(f: FeatureFrame, n: int = 14) -> np.ndarray:
    return np.asarray(f.atr(n), dtype=float)


def _day(f: FeatureFrame) -> np.ndarray:
    return np.asarray(f.t, dtype=np.int64) // 86400


def _hour(f: FeatureFrame) -> np.ndarray:
    return (np.asarray(f.t, dtype=np.int64) % 86400) / 3600.0


def _empty(f: FeatureFrame, atr: Optional[np.ndarray] = None) -> dict:
    n = len(f)
    return {"long": np.zeros(n, bool), "short": np.zeros(n, bool),
            "atr": _atr(f) if atr is None else atr}


def _ok(atr: np.ndarray, j: int) -> bool:
    return bool(np.isfinite(atr[j]) and atr[j] > 0)


# ------------------------------------------------------------------ 1. breakout + retest
def breakout_retest(f: FeatureFrame, htf: Optional[FeatureFrame] = None, params: Optional[dict] = None) -> dict:
    """Close beyond the prior `lookback`-bar range, then a pullback to within `tol` ATR of the broken level that
    closes back in the breakout's direction, within `wait` bars."""
    p = {"lookback": 40, "wait": 12, "tol_atr": 0.25, "min_break_atr": 0.1, **(params or {})}
    out = _empty(f)
    atr, c, o, h, l = out["atr"], f.c, f.o, f.h, f.l
    res, sup = prior_high(f, int(p["lookback"])), prior_low(f, int(p["lookback"]))
    side, level, expires = 0, 0.0, -1
    for j in range(int(p["lookback"]) + 1, len(f)):
        if not _ok(atr, j):
            continue
        tol = p["tol_atr"] * atr[j]
        if side != 0 and j <= expires:
            if side > 0 and l[j] <= level + tol and c[j] > o[j] and c[j] > level:
                out["long"][j] = True
                side = 0
            elif side < 0 and h[j] >= level - tol and c[j] < o[j] and c[j] < level:
                out["short"][j] = True
                side = 0
            elif (side > 0 and c[j] < level - tol) or (side < 0 and c[j] > level + tol):
                side = 0                                   # the break failed; stop waiting for its retest
        elif j > expires:
            side = 0
        if side == 0:
            edge = p["min_break_atr"] * atr[j]
            if np.isfinite(res[j]) and c[j] > res[j] + edge:
                side, level, expires = 1, res[j], j + int(p["wait"])
            elif np.isfinite(sup[j]) and c[j] < sup[j] - edge:
                side, level, expires = -1, sup[j], j + int(p["wait"])
    return out


# ------------------------------------------------------------------ 2. trend / momentum
def momentum_trend(f: FeatureFrame, htf: Optional[FeatureFrame] = None, params: Optional[dict] = None) -> dict:
    """Fast EMA above slow EMA, momentum over `mom_bars` worth at least `min_mom` ATR, entered when price closes
    back through the fast EMA after dipping below it."""
    p = {"fast": 20, "slow": 50, "mom_bars": 20, "min_mom_atr": 1.0, **(params or {})}
    out = _empty(f)
    atr, c = out["atr"], np.asarray(f.c, float)
    ef, es = np.asarray(f.ema(int(p["fast"])), float), np.asarray(f.ema(int(p["slow"])), float)
    start = int(p["slow"]) + int(p["mom_bars"]) + 1
    if len(f) <= start:
        return out
    mom = np.full(len(f), np.nan)
    k = int(p["mom_bars"])
    mom[k:] = c[k:] - c[:-k]
    for j in range(start, len(f)):
        if not _ok(atr, j) or not np.isfinite(mom[j]) or not np.isfinite(es[j]):
            continue
        need = p["min_mom_atr"] * atr[j]
        up, down = ef[j] > es[j] and mom[j] >= need, ef[j] < es[j] and mom[j] <= -need
        if up and c[j - 1] < ef[j - 1] and c[j] > ef[j]:
            out["long"][j] = True
        elif down and c[j - 1] > ef[j - 1] and c[j] < ef[j]:
            out["short"][j] = True
    return out


# ------------------------------------------------------------------ 3. session volatility breakout
def session_breakout(f: FeatureFrame, htf: Optional[FeatureFrame] = None, params: Optional[dict] = None) -> dict:
    """Range built over [range_from, range_to) UTC, traded on the first close beyond it inside
    [trade_from, trade_to) UTC. One signal per side per day.

    Hours are BROKER-SERVER hours converted to UTC upstream (the lab shifts every bar by
    broker.broker_utc_offset_hours), so no daylight-saving handling is needed here."""
    p = {"range_from": 0.0, "range_to": 7.0, "trade_from": 7.0, "trade_to": 13.0, "min_range_atr": 0.5,
         "buffer_atr": 0.1, **(params or {})}
    out = _empty(f)
    atr, c, hour, day = out["atr"], np.asarray(f.c, float), _hour(f), _day(f)
    in_range = (hour >= p["range_from"]) & (hour < p["range_to"])
    in_trade = (hour >= p["trade_from"]) & (hour < p["trade_to"])
    cur, hi, lo, done_up, done_dn = None, -np.inf, np.inf, False, False
    for j in range(len(f)):
        d = day[j]
        if d != cur:
            cur, hi, lo, done_up, done_dn = d, -np.inf, np.inf, False, False
        if in_range[j]:
            hi, lo = max(hi, f.h[j]), min(lo, f.l[j])
            continue
        if not in_trade[j] or not np.isfinite(hi) or not np.isfinite(lo) or hi <= lo or not _ok(atr, j):
            continue
        if hi - lo < p["min_range_atr"] * atr[j]:          # a range too tight to mean anything
            continue
        buf = p["buffer_atr"] * atr[j]
        if not done_up and c[j] > hi + buf:
            out["long"][j], done_up = True, True
        elif not done_dn and c[j] < lo - buf:
            out["short"][j], done_dn = True, True
    return out


# ------------------------------------------------------------------ 4. VWAP deviation reversion
def vwap_reversion(f: FeatureFrame, htf: Optional[FeatureFrame] = None, params: Optional[dict] = None) -> dict:
    """Price at least `min_dev` ATR away from the session VWAP, with the bar closing back toward it.

    The VWAP uses MT5 TICK volume, which counts quote updates, not contracts traded: it is a proxy for activity,
    not the centralized volume a futures VWAP is built on."""
    p = {"min_dev_atr": 2.0, "warmup_bars": 20, **(params or {})}
    out = _empty(f)
    atr, c, o = out["atr"], np.asarray(f.c, float), np.asarray(f.o, float)
    typical = (np.asarray(f.h, float) + np.asarray(f.l, float) + c) / 3.0
    vol = np.asarray(f.tick_volume, float)
    vol = np.where(np.isfinite(vol) & (vol > 0), vol, 1.0)
    day = _day(f)
    cur, pv, pvol, bars = None, 0.0, 0.0, 0
    for j in range(len(f)):
        if day[j] != cur:
            cur, pv, pvol, bars = day[j], 0.0, 0.0, 0
        pv += typical[j] * vol[j]
        pvol += vol[j]
        bars += 1
        if bars <= int(p["warmup_bars"]) or pvol <= 0 or not _ok(atr, j):
            continue
        vwap = pv / pvol
        dev = (c[j] - vwap) / atr[j]
        if dev <= -p["min_dev_atr"] and c[j] > o[j]:
            out["long"][j] = True
        elif dev >= p["min_dev_atr"] and c[j] < o[j]:
            out["short"][j] = True
    return out


# ------------------------------------------------------------------ 5. higher-timeframe continuation
def htf_continuation(f: FeatureFrame, htf: Optional[FeatureFrame] = None, params: Optional[dict] = None) -> dict:
    """Direction from the higher timeframe's EMA slope; entry when the trigger timeframe pulls back to its own EMA
    and closes back in the trend's direction."""
    p = {"htf_ema": 20, "htf_slope_bars": 3, "ema": 20, "min_slope_atr": 0.2, **(params or {})}
    out = _empty(f)
    if htf is None or len(htf) < int(p["htf_ema"]) + int(p["htf_slope_bars"]) + 2:
        return out
    atr, c = out["atr"], np.asarray(f.c, float)
    ema = np.asarray(f.ema(int(p["ema"])), float)
    he = np.asarray(htf.ema(int(p["htf_ema"])), float)
    hatr = _atr(htf)
    k = int(p["htf_slope_bars"])
    slope = np.full(len(htf), np.nan)
    slope[k:] = he[k:] - he[:-k]
    # the higher-timeframe bar that had CLOSED when each trigger bar closed
    step = int(np.median(np.diff(np.asarray(htf.t, np.int64)))) if len(htf) > 2 else 3600
    tstep = int(np.median(np.diff(np.asarray(f.t, np.int64)))) if len(f) > 2 else 60
    idx = np.searchsorted(np.asarray(htf.t, np.int64) + step, np.asarray(f.t, np.int64) + tstep, side="right") - 1
    for j in range(int(p["ema"]) + 1, len(f)):
        i = int(idx[j])
        if i < 0 or not _ok(atr, j) or not np.isfinite(slope[i]) or not np.isfinite(hatr[i]) or hatr[i] <= 0:
            continue
        need = p["min_slope_atr"] * hatr[i]
        if slope[i] >= need and f.l[j] <= ema[j] and c[j] > ema[j]:
            out["long"][j] = True
        elif slope[i] <= -need and f.h[j] >= ema[j] and c[j] < ema[j]:
            out["short"][j] = True
    return out


HYPOTHESES: Dict[str, Callable[..., dict]] = {
    "breakout_retest": breakout_retest,
    "momentum_trend": momentum_trend,
    "session_breakout": session_breakout,
    "vwap_reversion": vwap_reversion,
    "htf_continuation": htf_continuation,
}
