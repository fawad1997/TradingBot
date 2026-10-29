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

Two more come from the published evidence rather than from this codebase, and are scored by their own paths
because neither is a stop/target grid rule:
  trend_daily        daily time-series momentum (Clenow, "Following the Trend"): a close at a 50-day high with
                     EMA50 above EMA100, short in reverse; 3 x ATR(100) stop, trailed. app/research/lab/trend.py
  intraday_momentum  the last 30 minutes before the New York close follow the move since the previous close
                     (Gao, Han, Li & Zhou 2018; Baltussen, Da, Lammers & Martens 2021). Time exit at the close.
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


# ------------------------------------------------------------------ 6. daily trend following
def daily_bars(f: FeatureFrame, until: Optional[int] = None) -> FeatureFrame:
    """UTC-day bars from an intraday frame, keeping only days that had ENDED by `until` (epoch seconds).

    Default: every day except the newest, which may still be trading. Live passes the close time of the current
    M15 bar, so a day becomes visible the moment it is over and never before - the same day boundaries the lab
    uses, which is what keeps the live rule and its backtest on identical bars."""
    t = np.asarray(f.t, dtype=np.int64)
    if len(t) == 0:
        return FeatureFrame([], [], [], [], [], timeframe="D1")
    day = t // 86400
    last_open_day = (int(until) // 86400) if until is not None else int(day[-1])
    keep = day < last_open_day
    if not keep.any():
        return FeatureFrame([], [], [], [], [], timeframe="D1")
    t, day = t[keep], day[keep]
    o, h, l, c = (np.asarray(x, float)[keep] for x in (f.o, f.h, f.l, f.c))
    sp, tv = np.asarray(f.spread, float)[keep], np.asarray(f.tick_volume, float)[keep]
    starts = np.flatnonzero(np.r_[True, day[1:] != day[:-1]])
    ends = np.r_[starts[1:], len(day)]
    return FeatureFrame(day[starts] * 86400, o[starts], np.maximum.reduceat(h, starts),
                        np.minimum.reduceat(l, starts), c[ends - 1],
                        np.array([float(np.median(sp[a:b])) for a, b in zip(starts, ends)]),
                        np.add.reduceat(tv, starts), "D1")


def session_close_ny(f: FeatureFrame) -> Optional[tuple]:
    """(hour, minute) in New York time at which this symbol's sessions usually END: the most common close time of
    each day's last bar. A trading schedule is instrument metadata (MT5 publishes it), not price information, so
    reading it off the history does not leak future prices into a signal."""
    from collections import Counter
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    t = np.asarray(f.t, dtype=np.int64)
    if len(t) < 50:
        return None
    bar = int(np.median(np.diff(t)))
    ny = ZoneInfo("America/New_York")
    day = t // 86400
    last = t[np.r_[day[1:] != day[:-1], True]]
    closes = Counter(_dt.fromtimestamp(int(x) + bar, tz=ny).strftime("%H:%M") for x in last)
    if not closes:
        return None
    hh, mm = closes.most_common(1)[0][0].split(":")
    return int(hh), int(mm)


TREND_DAILY_DEFAULTS = {"breakout_days": 50, "ema_fast": 50, "ema_slow": 100, "atr_days": 100, "stop_atr": 3.0}
#: The broker needs a take-profit on every position, so the live strategy sets one this far away; the replay uses
#: the same, so a 25R trend is booked at 20R in both.
FAR_TARGET_R = 20.0


def trend_daily(f: FeatureFrame, htf: Optional[FeatureFrame] = None, params: Optional[dict] = None) -> dict:
    """On DAILY bars: long when today's close is the highest close of the last `breakout_days` (today included)
    and EMA(fast) > EMA(slow); short in reverse. `dist` is the stop distance, `stop_atr` x ATR(atr_days), which
    is also the trailing distance from the best price. Parameters are Clenow's, fixed in advance - no search."""
    p = {**TREND_DAILY_DEFAULTS, **(params or {})}
    n = len(f)
    atr = _atr(f, int(p["atr_days"]))
    out = _empty(f, atr)
    out["dist"] = float(p["stop_atr"]) * atr
    b = int(p["breakout_days"])
    if n < max(b, int(p["ema_slow"]), int(p["atr_days"])) + 1:
        return out
    c = np.asarray(f.c, float)
    ef, es = np.asarray(f.ema(int(p["ema_fast"])), float), np.asarray(f.ema(int(p["ema_slow"])), float)
    w = np.lib.stride_tricks.sliding_window_view(c, b)
    hi, lo = np.full(n, np.nan), np.full(n, np.nan)
    hi[b - 1:], lo[b - 1:] = w.max(axis=1), w.min(axis=1)
    ok = np.isfinite(atr) & (atr > 0) & np.isfinite(ef) & np.isfinite(es) & np.isfinite(hi)
    out["long"] = ok & (c >= hi) & (ef > es)
    out["short"] = ok & (c <= lo) & (ef < es)
    return out


# ------------------------------------------------------------------ 6b. multi-speed time-series momentum
#: Lookbacks in CALENDAR days - 1, 3, 6 and 12 months - so a rule on crypto (7 bars a week) and on FX (5-6) looks
#: back over the same stretch of time. The published speeds, fixed in advance: nothing here is searched.
TREND_ENSEMBLE_DEFAULTS = {"lookbacks": (30, 91, 182, 365), "atr_days": 100, "stop_atr": 3.0}


def trend_ensemble(f: FeatureFrame, htf: Optional[FeatureFrame] = None, params: Optional[dict] = None) -> dict:
    """On DAILY bars: long while the return over EVERY lookback is positive, short while every one is negative.

    Time-series momentum per speed (Moskowitz, Ooi & Pedersen 2012), blended across speeds as trend funds do
    (Hurst, Ooi & Pedersen, "A Century of Evidence on Trend-Following"). Requiring all four to agree is the trade
    form of the blended position: on 2018-2026 daily data for gold, three FX pairs, BTC and ETH, net of the
    account's spread and swap, the blend made a Sharpe of 0.52 against 0.46 for the 50-day breakout, and +0.29
    against -0.14 over the second half (screen of 23 Oct 2026). `score` is the average sign across the speeds;
    `dist` = stop_atr x ATR(atr_days) is the stop and the trailing distance, as in trend_daily."""
    p = {**TREND_ENSEMBLE_DEFAULTS, **(params or {})}
    n = len(f)
    atr = _atr(f, int(p["atr_days"]))
    out = _empty(f, atr)
    out["dist"] = float(p["stop_atr"]) * atr
    out["score"] = np.zeros(n)
    if n < 2:
        return out
    t = np.asarray(f.t, dtype=np.int64)
    c = np.asarray(f.c, float)
    lookbacks = [int(x) for x in p["lookbacks"]]
    ok = np.isfinite(atr) & (atr > 0)
    score = np.zeros(n)
    for days in lookbacks:
        # the last bar at or before `days` calendar days ago: only past timestamps, so causal
        j = np.searchsorted(t, t - days * 86400, side="right") - 1
        have = j >= 0
        r = np.full(n, np.nan)
        r[have] = c[have] / c[j[have]] - 1.0
        ok &= np.isfinite(r)
        score += np.sign(np.nan_to_num(r))
    score /= len(lookbacks)
    out["score"] = score
    out["long"] = ok & (score >= 1.0)
    out["short"] = ok & (score <= -1.0)
    return out


# ------------------------------------------------------------------ 6c. turn of the month (equity indices)
TURN_OF_MONTH_DEFAULTS = {"atr_days": 20, "stop_atr": 3.0, "hold_days": 4}


def turn_of_month(f: FeatureFrame, htf: Optional[FeatureFrame] = None, params: Optional[dict] = None) -> dict:
    """On DAILY bars of an equity index: long on the close of the second-to-last weekday of the month, so the
    position is held through the last trading day and the first three of the next (days -1 to +3).

    McConnell & Xu ("Equity Returns at the Turn of the Month", 2008): over 1926-2005 the whole US equity excess
    return fell in those four days, and 30 of 34 other countries show it. Long only - the effect is the equity
    premium arriving at month ends. Weekdays stand in for exchange days (a month-end holiday shifts it by a day).
    The trade is meant to end after `hold_days` daily bars; `dist` = stop_atr x ATR(atr_days) is a protective stop
    a normal four days should not reach."""
    p = {**TURN_OF_MONTH_DEFAULTS, **(params or {})}
    n = len(f)
    atr = _atr(f, int(p["atr_days"]))
    out = _empty(f, atr)
    out["dist"] = float(p["stop_atr"]) * atr
    if n < 2:
        return out
    import datetime as _dt
    days = (np.asarray(f.t, dtype=np.int64) // 86400).tolist()
    for j, d in enumerate(days):
        day = _dt.date(1970, 1, 1) + _dt.timedelta(days=int(d))
        if day.weekday() > 4 or not _ok(atr, j):          # a Sunday-evening stub bar is not a trading day
            continue
        nxt, left = day + _dt.timedelta(days=1), 0
        while nxt.month == day.month:                     # weekdays still to come this month
            left += nxt.weekday() < 5
            nxt += _dt.timedelta(days=1)
        out["long"][j] = left == 1
    return out


# ------------------------------------------------------------------ 7. intraday momentum into the close
def intraday_momentum(f: FeatureFrame, htf: Optional[FeatureFrame] = None, params: Optional[dict] = None) -> dict:
    """US cash session only: on the bar that closes `minutes_before_close` minutes before the session close (New
    York time, `close_hour`:`close_minute`), signal in the direction of the move since the previous session's close;
    the fill is the next bar's open and the trade is meant to be closed at the session close (`hold` bars).
    Daylight saving comes from zoneinfo, so the UTC times move with New York's clock. A day with no bar closing
    exactly at the entry time (a half day, a gap) has no signal, and only a bar that closed exactly at the session
    close sets the reference close for the next day.

    The papers use the exchange close, 16:00. Exness stops its US stock CFDs at 15:45 New York (20 Oct 2026, from
    the bars themselves), so the lab passes the session close it finds in each symbol's own history."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    p = {"minutes_before_close": 30, "close_hour": 16, "close_minute": 0, **(params or {})}
    out = _empty(f)
    t = np.asarray(f.t, dtype=np.int64)
    n = len(t)
    out["hold"] = 0
    if n < 50:
        return out
    bar = int(np.median(np.diff(t)))
    if bar <= 0:
        return out
    hold = int(p["minutes_before_close"]) * 60 // bar
    out["hold"] = hold
    if hold < 1:
        return out
    ny = ZoneInfo("America/New_York")
    dates = sorted({_dt.fromtimestamp(int(x), tz=ny).date() for x in t[::max(1, 3600 // bar)]})
    atr, c = out["atr"], np.asarray(f.c, float)
    prev_close = None
    for d in dates:
        close_ts = int(_dt(d.year, d.month, d.day, int(p["close_hour"]), int(p["close_minute"]),
                           tzinfo=ny).timestamp())
        entry_ts = close_ts - hold * bar
        # causal: bar j has CLOSED at the entry time; nothing after it is looked at to decide the signal
        j = int(np.searchsorted(t, entry_ts - bar))
        if j < n and t[j] == entry_ts - bar and prev_close is not None and _ok(atr, j) and c[j] != prev_close:
            (out["long"] if c[j] > prev_close else out["short"])[j] = True
        last = int(np.searchsorted(t, close_ts - bar))
        if last < n and t[last] == close_ts - bar:
            prev_close = c[last]                     # used only from the NEXT day on
    return out


HYPOTHESES: Dict[str, Callable[..., dict]] = {
    "breakout_retest": breakout_retest,
    "momentum_trend": momentum_trend,
    "session_breakout": session_breakout,
    "vwap_reversion": vwap_reversion,
    "htf_continuation": htf_continuation,
}
