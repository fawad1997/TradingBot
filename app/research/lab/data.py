"""History for the research lab: read straight from MT5 (history only, no trading calls), cache as CSV."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

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


def fetch_frames(adapter, symbol: str, offset_hours: float, days_m1: int, days_m15: int,
                 cache: bool = True) -> Optional[Tuple[FeatureFrame, FeatureFrame, float]]:
    if not adapter.select_symbol(symbol):
        return None
    spec = adapter.symbol_spec(symbol)
    end = datetime.now(timezone.utc)
    frames = []
    for tf, days in (("M1", days_m1), ("M15", days_m15)):
        raw = adapter.rates_range(symbol, tf, end - timedelta(days=days), end)
        bars = to_utc_bars(raw, offset_hours)
        if cache and bars:
            save_bars_csv(LAB_DATA / f"{symbol}_{tf}.csv", bars)
        frames.append(FeatureFrame.from_bars(bars, tf))
    return frames[0], frames[1], spec.point


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
