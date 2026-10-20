"""Causal technical indicators (value at index i only uses data <= i).

All functions take/return numpy float arrays of the same length; positions
without enough history are NaN.
"""
from __future__ import annotations

import numpy as np


def _arr(x) -> np.ndarray:
    return np.asarray(x, dtype=float)


def sma(x, n: int) -> np.ndarray:
    x = _arr(x)
    out = np.full(len(x), np.nan)
    if n <= 0 or len(x) < n:
        return out
    c = np.cumsum(np.insert(x, 0, 0.0))
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def rolling_std(x, n: int) -> np.ndarray:
    x = _arr(x)
    out = np.full(len(x), np.nan)
    if n <= 1 or len(x) < n:
        return out
    c1 = np.cumsum(np.insert(x, 0, 0.0))
    c2 = np.cumsum(np.insert(x * x, 0, 0.0))
    s1 = c1[n:] - c1[:-n]
    s2 = c2[n:] - c2[:-n]
    var = np.maximum(s2 / n - (s1 / n) ** 2, 0.0)
    out[n - 1:] = np.sqrt(var)
    return out


def ema(x, n: int) -> np.ndarray:
    x = _arr(x)
    out = np.full(len(x), np.nan)
    if n <= 0:
        return out
    valid = np.where(~np.isnan(x))[0]
    if len(valid) == 0:
        return out
    f = valid[0]
    if len(x) - f < n:
        return out
    a = 2.0 / (n + 1.0)
    out[f + n - 1] = np.mean(x[f:f + n])
    prev = out[f + n - 1]
    for i in range(f + n, len(x)):
        xi = x[i]
        prev = a * xi + (1.0 - a) * prev if not np.isnan(xi) else prev
        out[i] = prev
    return out


def wilder(x, n: int) -> np.ndarray:
    """Wilder's smoothing (RMA), seeded with a simple mean, skipping leading NaNs."""
    x = _arr(x)
    out = np.full(len(x), np.nan)
    valid = np.where(~np.isnan(x))[0]
    if len(valid) == 0:
        return out
    f = valid[0]
    if len(x) - f < n:
        return out
    out[f + n - 1] = np.mean(x[f:f + n])
    prev = out[f + n - 1]
    for i in range(f + n, len(x)):
        prev = (prev * (n - 1) + x[i]) / n
        out[i] = prev
    return out


def true_range(h, l, c) -> np.ndarray:
    h, l, c = _arr(h), _arr(l), _arr(c)
    if len(c) == 0:
        return np.array([])
    prev = np.concatenate([[c[0]], c[:-1]])
    return np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))


def atr(h, l, c, n: int = 14) -> np.ndarray:
    return wilder(true_range(h, l, c), n)


def rsi(c, n: int = 14) -> np.ndarray:
    c = _arr(c)
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return out
    d = np.diff(c)
    gain = np.maximum(d, 0.0)
    loss = np.maximum(-d, 0.0)
    ag = wilder(gain, n)
    al = wilder(loss, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ag / al
        r = 100.0 - 100.0 / (1.0 + rs)
    r = np.where((al == 0) & (ag > 0), 100.0, r)
    r = np.where((al == 0) & (ag == 0), 50.0, r)
    out[1:] = r
    return out


def adx(h, l, c, n: int = 14):
    """Returns (adx, +DI, -DI)."""
    h, l, c = _arr(h), _arr(l), _arr(c)
    N = len(c)
    nanarr = np.full(N, np.nan)
    if N <= 2 * n + 1:
        return nanarr, nanarr.copy(), nanarr.copy()
    up = h[1:] - h[:-1]
    dn = l[:-1] - l[1:]
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = true_range(h, l, c)[1:]
    atr_ = wilder(tr, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100.0 * wilder(pdm, n) / atr_
        mdi = 100.0 * wilder(mdm, n) / atr_
        dx = 100.0 * np.abs(pdi - mdi) / (pdi + mdi)
    dx = np.where(np.isfinite(dx), dx, np.nan)
    adx_ = wilder(dx, n)
    out_adx = np.concatenate([[np.nan], adx_])
    out_pdi = np.concatenate([[np.nan], pdi])
    out_mdi = np.concatenate([[np.nan], mdi])
    return out_adx, out_pdi, out_mdi


def normalized_slope(series, atr_arr, k: int) -> np.ndarray:
    """(series[i] - series[i-k]) / (k * atr[i]) : slope per bar in ATR units."""
    s = _arr(series)
    a = _arr(atr_arr)
    out = np.full(len(s), np.nan)
    if k <= 0 or len(s) <= k:
        return out
    with np.errstate(divide="ignore", invalid="ignore"):
        out[k:] = (s[k:] - s[:-k]) / (k * a[k:])
    out[~np.isfinite(out)] = np.nan
    return out


def rolling_percentile_rank(x, window: int, chunk: int = 4096) -> np.ndarray:
    """Fraction of the trailing `window` values (inclusive) that are <= x[i]."""
    x = _arr(x)
    N = len(x)
    out = np.full(N, np.nan)
    if N < window or window < 2:
        return out
    from numpy.lib.stride_tricks import sliding_window_view
    win = sliding_window_view(x, window)          # shape (N-window+1, window), view (no copy)
    for start in range(0, win.shape[0], chunk):
        w = win[start:start + chunk]
        cur = w[:, -1][:, None]
        valid = ~np.isnan(w)
        cnt = valid.sum(axis=1)
        le = ((w <= cur) & valid).sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            r = le / cnt
        r[np.isnan(w[:, -1])] = np.nan
        out[window - 1 + start: window - 1 + start + len(w)] = r
    return out


def bollinger(c, n: int = 20, k: float = 2.0):
    mid = sma(c, n)
    sd = rolling_std(c, n)
    return mid, mid + k * sd, mid - k * sd


def pivots(h, l, n: int = 3):
    """Fractal pivots. pivot_high[j] is True if high[j] is the strict max of [j-n, j+n].

    A pivot at j is only *confirmed* at index j+n. Callers must only use pivots
    with j <= i - n when evaluating at index i (see `last_confirmed_pivot`).
    """
    h, l = _arr(h), _arr(l)
    N = len(h)
    ph = np.zeros(N, dtype=bool)
    pl = np.zeros(N, dtype=bool)
    if N < 2 * n + 1:
        return ph, pl
    from numpy.lib.stride_tricks import sliding_window_view
    wh = sliding_window_view(h, 2 * n + 1)
    wl = sliding_window_view(l, 2 * n + 1)
    centre_h = wh[:, n]
    centre_l = wl[:, n]
    others_h = np.delete(wh, n, axis=1)
    others_l = np.delete(wl, n, axis=1)
    ph[n:N - n] = centre_h > others_h.max(axis=1)
    pl[n:N - n] = centre_l < others_l.min(axis=1)
    return ph, pl


def last_confirmed_pivot(flags: np.ndarray, i: int, n: int, after: int = -1) -> int:
    """Index of the latest pivot confirmed by bar i (pivot index <= i-n), strictly after `after`."""
    hi = i - n
    if hi < 0:
        return -1
    idx = np.flatnonzero(flags[max(after + 1, 0):hi + 1])
    return int(idx[-1] + max(after + 1, 0)) if len(idx) else -1
