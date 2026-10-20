"""FeatureFrame: one timeframe's OHLC arrays plus lazily cached causal indicators.

The same object is used live (rebuilt from the closed-bar store on every bar
close) and in the backtester (built once over the whole history, evaluated at a
moving index). Because every indicator is causal, both paths see identical
values at a given bar -> backtest and live logic cannot drift apart.
"""
from __future__ import annotations

from typing import Dict, Iterable, Tuple

import numpy as np

from app.core.types import Bar
from app.features import indicators as ind


class FeatureFrame:
    def __init__(self, t, o, h, l, c, spread=None, tick_volume=None, timeframe: str = ""):
        self.timeframe = timeframe
        self.t = np.asarray(t, dtype=np.int64)
        self.o = np.asarray(o, dtype=float)
        self.h = np.asarray(h, dtype=float)
        self.l = np.asarray(l, dtype=float)
        self.c = np.asarray(c, dtype=float)
        n = len(self.t)
        self.spread = np.asarray(spread if spread is not None else np.zeros(n), dtype=float)
        self.tick_volume = np.asarray(tick_volume if tick_volume is not None else np.zeros(n), dtype=float)
        self._cache: Dict[Tuple, object] = {}

    @classmethod
    def from_bars(cls, bars: Iterable[Bar], timeframe: str = "") -> "FeatureFrame":
        bars = list(bars)
        return cls([b.time for b in bars], [b.open for b in bars], [b.high for b in bars],
                   [b.low for b in bars], [b.close for b in bars], [b.spread for b in bars],
                   [b.tick_volume for b in bars], timeframe)

    def __len__(self) -> int:
        return len(self.t)

    @property
    def last(self) -> int:
        return len(self.t) - 1

    def _memo(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def ema(self, n: int) -> np.ndarray:
        return self._memo(("ema", n), lambda: ind.ema(self.c, n))

    def sma(self, n: int) -> np.ndarray:
        return self._memo(("sma", n), lambda: ind.sma(self.c, n))

    def atr(self, n: int = 14) -> np.ndarray:
        return self._memo(("atr", n), lambda: ind.atr(self.h, self.l, self.c, n))

    def rsi(self, n: int = 14) -> np.ndarray:
        return self._memo(("rsi", n), lambda: ind.rsi(self.c, n))

    def adx(self, n: int = 14):
        return self._memo(("adx", n), lambda: ind.adx(self.h, self.l, self.c, n))

    def ema_slope(self, ema_n: int, k: int, atr_n: int = 14) -> np.ndarray:
        return self._memo(("slope", ema_n, k, atr_n),
                          lambda: ind.normalized_slope(self.ema(ema_n), self.atr(atr_n), k))

    def atr_pct(self, atr_n: int = 14, window: int = 500) -> np.ndarray:
        w = min(window, max(len(self) - atr_n, 20))
        return self._memo(("atr_pct", atr_n, w), lambda: ind.rolling_percentile_rank(self.atr(atr_n), w))

    def bollinger(self, n: int = 20, k: float = 2.0):
        return self._memo(("bb", n, k), lambda: ind.bollinger(self.c, n, k))

    def pivots(self, n: int = 3):
        return self._memo(("piv", n), lambda: ind.pivots(self.h, self.l, n))

    def required_history(self) -> int:
        return len(self)


def resample(frame: FeatureFrame, seconds: int, timeframe: str = "") -> FeatureFrame:
    """Resample to a higher timeframe keeping only COMPLETE buckets (causal)."""
    if len(frame) == 0:
        return FeatureFrame([], [], [], [], [], timeframe=timeframe)
    bucket = (frame.t // seconds) * seconds
    keys, start_idx = np.unique(bucket, return_index=True)
    end_idx = np.append(start_idx[1:], len(bucket))
    t, o, h, l, c, sp, tv = [], [], [], [], [], [], []
    base = int(np.median(np.diff(frame.t))) if len(frame) > 1 else seconds
    expected = max(1, seconds // max(base, 1))
    for k_i, (s, e) in enumerate(zip(start_idx, end_idx)):
        last_bucket = k_i == len(keys) - 1
        # the last bucket is incomplete unless it has every sub-bar
        if last_bucket and (e - s) < expected:
            continue
        t.append(keys[k_i])
        o.append(frame.o[s])
        h.append(frame.h[s:e].max())
        l.append(frame.l[s:e].min())
        c.append(frame.c[e - 1])
        sp.append(float(np.median(frame.spread[s:e])))
        tv.append(float(frame.tick_volume[s:e].sum()))
    return FeatureFrame(t, o, h, l, c, sp, tv, timeframe)
