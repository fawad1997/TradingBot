"""History for the research lab: read straight from MT5 (history only, no trading calls), cache as CSV."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.config import ROOT
from app.core.types import Bar
from app.features.frame import FeatureFrame
from app.research.data import load_bars, save_bars_csv

LAB_DATA = ROOT / "research" / "lab_data"


def despike(frame, max_sigma: float = 8.0, lookback: int = 50):
    """Flag bars whose close is an implausible jump from the recent run of closes.

    Chan ch.7: a bad print inflates MEAN-REVERSION backtests specifically, and does so invisibly. The
    strategy buys the fictitious low quote and sells on the next correct one, booking a profit that
    was never available. Momentum rules are far less affected, so a dataset that quietly flatters one
    family of strategies over another is worse than one that is simply noisy.

    Returns a boolean mask, True where the bar looks real. Judged on the median absolute deviation of
    recent log returns rather than the standard deviation, because one spike inflates a standard
    deviation enough to hide itself.

    8 sigma is deliberately permissive. Real markets gap, and a filter tight enough to catch every bad
    print would discard genuine moves - which would bias the research in the opposite direction.
    """
    import numpy as np
    c = np.asarray(frame.c, dtype=float)
    n = len(c)
    good = np.ones(n, dtype=bool)
    if n < max(10, lookback // 4):
        return good
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.diff(np.log(np.where(c > 0, c, np.nan)))
    lr = np.nan_to_num(lr, nan=0.0, posinf=0.0, neginf=0.0)
    for i in range(1, n):
        lo = max(0, i - lookback - 1)
        window = lr[lo:i - 1] if i - 1 > lo else lr[lo:i]
        if len(window) < 10:
            continue
        med = float(np.median(window))
        mad = float(np.median(np.abs(window - med)))
        scale = mad * 1.4826          # MAD -> sigma for a normal distribution
        if scale <= 0:
            continue
        if abs(lr[i - 1] - med) > max_sigma * scale:
            good[i] = False
    return good


def despike_report(frame, max_sigma: float = 8.0, lookback: int = 50) -> dict:
    """How many bars the filter considers bad, for the lab to record rather than silently drop."""
    import numpy as np
    good = despike(frame, max_sigma, lookback)
    bad = int((~good).sum())
    return {"bars": int(len(good)), "suspect_bars": bad,
            "suspect_fraction": round(bad / len(good), 6) if len(good) else 0.0,
            "max_sigma": max_sigma}


def to_utc_bars(raw: List[Bar], offset_hours: float) -> List[Bar]:
    seen, out = set(), []
    for x in raw[:-1]:                          # the last bar may still be forming
        t = int(x.time - offset_hours * 3600)
        if t not in seen:
            seen.add(t)
            out.append(Bar(t, x.open, x.high, x.low, x.close, x.tick_volume, x.spread, x.real_volume))
    return sorted(out, key=lambda b: b.time)


#: Days per request when MT5 refuses a long range in one go.
CHUNK_DAYS = 90


def fetch_range(adapter, symbol: str, tf: str, start: datetime, end: datetime,
                chunk_days: int = CHUNK_DAYS) -> List[Bar]:
    """History for [start, end], asked for in one request and, if MT5 refuses, in `chunk_days` pieces.

    MT5 answers a long copy_rates_range with "Invalid params" and nothing else (20 Oct 2026: EURUSDm M5 over 365
    days failed twice, the same request over 180 days returned 36,815 bars), and a timeframe it has not synced
    yet can fail once and then succeed (GBPJPYm H4). So: one try, a retry, then chunks. Overlapping bars at chunk
    edges are removed by `to_utc_bars`, which also drops the newest (possibly still forming) bar."""
    for _ in range(2):
        try:
            bars = adapter.rates_range(symbol, tf, start, end)
        except Exception:
            bars = None
        if bars:
            return list(bars)
    out: List[Bar] = []
    t = start
    while t < end:
        t2 = min(end, t + timedelta(days=chunk_days))
        try:
            out.extend(adapter.rates_range(symbol, tf, t, t2) or [])
        except Exception:
            pass
        t = t2
    return out


def _fetch(adapter, symbol: str, tf: str, days: int, offset_hours: float, cache: bool) -> FeatureFrame:
    end = datetime.now(timezone.utc)
    bars = to_utc_bars(fetch_range(adapter, symbol, tf, end - timedelta(days=days), end), offset_hours)
    if cache and bars:
        save_bars_csv(LAB_DATA / f"{symbol}_{tf}.csv", bars)
    return FeatureFrame.from_bars(bars, tf)


def fetch_frames(adapter, symbol: str, offset_hours: float, days_m1: int, days_m15: int,
                 cache: bool = True) -> Optional[Tuple[FeatureFrame, FeatureFrame, float]]:
    if not adapter.select_symbol(symbol):
        return None
    spec = adapter.symbol_spec(symbol)
    m1 = _fetch(adapter, symbol, "M1", days_m1, offset_hours, cache)
    m15 = _fetch(adapter, symbol, "M15", days_m15, offset_hours, cache)
    return m1, m15, spec.point


def fetch_long(adapter, symbol: str, offset_hours: float, days: Dict[str, int],
               cache: bool = True) -> Dict[str, FeatureFrame]:
    """Longer histories straight from MT5 ({"M5": 365, "H4": 3650}), so a rule is judged on a year or a decade
    instead of resampled M1 weeks. A timeframe that returns nothing is left out; the caller falls back."""
    out: Dict[str, FeatureFrame] = {}
    for tf, n in (days or {}).items():
        if not n or n <= 0:
            continue
        try:
            frame = _fetch(adapter, symbol, tf, int(n), offset_hours, cache)
        except Exception:
            continue
        if len(frame):
            out[tf] = frame
    return out


def spread_ratio(m1: FeatureFrame, point: float, tick_spread) -> float:
    """How much more the real (tick) spread is than MT5's per-bar spread, over the bars where both are known.

    A bar's recorded spread is a single number per bar, often the smallest seen, so it understates what an order
    pays. The long M5/M15/H4 histories have only bar spreads; scaling them by this ratio (never below 1) keeps a
    year-long backtest from being cheaper than the last 60 days of real ticks say trading is."""
    import numpy as np
    if tick_spread is None or m1 is None or not len(m1):
        return 1.0
    bar = np.asarray(m1.spread, float) * float(point)
    tick = np.asarray(tick_spread, float)
    ok = np.isfinite(tick) & (tick > 0) & np.isfinite(bar) & (bar > 0)
    if ok.sum() < 100:
        return 1.0
    return max(1.0, float(np.median(tick[ok] / bar[ok])))


def last_days(frame: FeatureFrame, days: int) -> FeatureFrame:
    """The most recent `days` of a frame (the funnel and threshold sweep stay on a short window: they replay every
    strategy bar by bar, and two years of M15 would take an hour a cycle)."""
    import numpy as np
    if frame is None or not len(frame) or not days:
        return frame
    cut = int(frame.t[-1]) - int(days) * 86400
    i = int(np.searchsorted(np.asarray(frame.t, np.int64), cut, side="left"))
    if i <= 0:
        return frame
    return FeatureFrame(frame.t[i:], frame.o[i:], frame.h[i:], frame.l[i:], frame.c[i:], frame.spread[i:],
                        frame.tick_volume[i:], frame.timeframe)


def load_cached(symbol: str, point: float) -> Optional[Tuple[FeatureFrame, FeatureFrame, float]]:
    a, b = LAB_DATA / f"{symbol}_M1.csv", LAB_DATA / f"{symbol}_M15.csv"
    if not (a.exists() and b.exists()):
        return None
    return load_bars(a, "M1"), load_bars(b, "M15"), point


# ---------------------------------------------------------------------------------------------------------------
# Real spreads from ticks. MT5's M1 bar "spread" is a single number per minute (often the smallest seen), so it
# understates what an order actually pays. Ticks give the spread at the moment each bar opened, which is when the
# lab's simulated orders fill.
def tick_open_spreads(adapter, symbol: str, offset_hours: float, days: int, bar_times) -> Optional["np.ndarray"]:
    """Spread (price units) of the first tick of each M1 bar in `bar_times` (UTC seconds), NaN where unknown.
    Read one day at a time so a busy crypto symbol never loads millions of ticks at once."""
    import numpy as np
    fetch = getattr(adapter, "ticks_array", None)
    if fetch is None or days <= 0 or len(bar_times) == 0:
        return None
    bar_times = np.asarray(bar_times, dtype=np.int64)
    out = np.full(len(bar_times), np.nan)
    end = datetime.now(timezone.utc)
    got = 0
    for d in range(days, 0, -1):
        a, b = end - timedelta(days=d), end - timedelta(days=d - 1)
        try:
            res = fetch(symbol, a, b)
        except Exception:
            res = None
        if res is None:
            continue
        tms, bid, ask = res
        if len(tms) == 0:
            continue
        minute = (np.asarray(tms, dtype=np.int64) // 1000 - int(offset_hours * 3600)) // 60 * 60
        first_minutes, first_idx = np.unique(minute, return_index=True)
        spreads = (np.asarray(ask, float) - np.asarray(bid, float))[first_idx]
        pos = np.searchsorted(bar_times, first_minutes)
        ok = (pos < len(bar_times)) & (bar_times[np.minimum(pos, len(bar_times) - 1)] == first_minutes)
        out[pos[ok]] = spreads[ok]
        got += int(ok.sum())
    return out if got else None


def resample(frame: FeatureFrame, seconds: int, spread_price=None):
    """Aggregate a frame to a slower timeframe. Returns (frame, spread price at each new bar's open)."""
    import numpy as np
    t = np.asarray(frame.t, dtype=np.int64)
    if len(t) == 0:
        return frame, None
    key = t // seconds * seconds
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    ends = np.r_[starts[1:], len(t)]
    o = frame.o[starts]
    c = frame.c[ends - 1]
    h = np.maximum.reduceat(frame.h, starts)
    l = np.minimum.reduceat(frame.l, starts)
    v = np.add.reduceat(frame.tick_volume, starts)
    sp_pts = frame.spread[starts]
    out = FeatureFrame(key[starts], o, h, l, c, sp_pts, v, f"{seconds // 60}m")
    sp = None if spread_price is None else np.asarray(spread_price, float)[starts]
    return out, sp
