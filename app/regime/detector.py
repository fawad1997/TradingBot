"""Deterministic regime engine (H1).

Output has two dimensions plus a stress flag:
  trend : TREND_UP / TREND_DOWN / RANGE / TRANSITION / UNKNOWN
  vol   : HIGH_VOLATILITY / LOW_VOLATILITY / NORMAL / VOLATILITY_EXPANSION / VOLATILITY_CONTRACTION
  stress: spread shock or extreme volatility
Hysteresis: a new trend label must persist `confirm_bars` H1 bars before it replaces the old one.
Statistical / ML regime models are research candidates and are not used live in v0.1.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from app.core.types import TrendRegime, VolRegime
from app.features.frame import FeatureFrame


@dataclass
class RegimeParams:
    ema_fast: int = 50
    ema_slow: int = 200
    adx_len: int = 14
    adx_trend: float = 22.0
    adx_range: float = 18.0
    slope_len: int = 10
    slope_trend: float = 0.02
    atr_len: int = 14
    atr_pct_window: int = 250
    high_vol_pct: float = 0.85
    low_vol_pct: float = 0.15
    stress_vol_pct: float = 0.98
    expansion_ratio: float = 1.3     # ATR(now) / ATR(5 bars ago)
    confirm_bars: int = 2


@dataclass
class RegimeState:
    trend: TrendRegime = TrendRegime.UNKNOWN
    vol: VolRegime = VolRegime.UNKNOWN
    stress: bool = False
    label: str = "UNKNOWN"
    bar_time: int = 0
    diagnostics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"trend": str(self.trend), "vol": str(self.vol), "stress": self.stress, "label": self.label,
                "bar_time": self.bar_time, "diagnostics": self.diagnostics}


def _f(x) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


class RegimeDetector:
    def __init__(self, params: Optional[RegimeParams] = None):
        self.p = params or RegimeParams()
        self.state = RegimeState()
        self._candidate: Optional[TrendRegime] = None
        self._candidate_count = 0

    def lookback(self) -> int:
        return max(self.p.ema_slow, self.p.adx_len * 3, self.p.atr_pct_window + self.p.atr_len)

    def raw_trend(self, h1: FeatureFrame, k: int):
        p = self.p
        ef, es = _f(h1.ema(p.ema_fast)[k]), _f(h1.ema(p.ema_slow)[k])
        adx, pdi, mdi = (_f(a[k]) for a in h1.adx(p.adx_len))
        slope = _f(h1.ema_slope(p.ema_fast, p.slope_len, p.atr_len)[k])
        diag = {"ema_fast": ef, "ema_slow": es, "adx": adx, "pdi": pdi, "mdi": mdi, "slope": slope,
                "close": float(h1.c[k])}
        if None in (ef, es, adx, slope, pdi, mdi):
            return TrendRegime.UNKNOWN, diag
        c = h1.c[k]
        if adx >= p.adx_trend and ef > es and c > ef and slope > p.slope_trend and pdi > mdi:
            return TrendRegime.TREND_UP, diag
        if adx >= p.adx_trend and ef < es and c < ef and slope < -p.slope_trend and mdi > pdi:
            return TrendRegime.TREND_DOWN, diag
        if adx <= p.adx_range and abs(slope) <= p.slope_trend * 1.5:
            return TrendRegime.RANGE, diag
        return TrendRegime.TRANSITION, diag

    def raw_vol(self, h1: FeatureFrame, k: int):
        p = self.p
        pct = _f(h1.atr_pct(p.atr_len, p.atr_pct_window)[k])
        a = h1.atr(p.atr_len)
        now, prev = _f(a[k]), _f(a[k - 5]) if k >= 5 else None
        ratio = (now / prev) if (now and prev) else None
        diag = {"atr": now, "atr_pct": pct, "atr_ratio_5": ratio}
        if pct is None:
            return VolRegime.UNKNOWN, False, diag
        stress = pct >= p.stress_vol_pct
        if ratio is not None and ratio >= p.expansion_ratio:
            return VolRegime.VOLATILITY_EXPANSION, stress, diag
        if ratio is not None and ratio <= 1.0 / p.expansion_ratio:
            return VolRegime.VOLATILITY_CONTRACTION, stress, diag
        if pct >= p.high_vol_pct:
            return VolRegime.HIGH_VOLATILITY, stress, diag
        if pct <= p.low_vol_pct:
            return VolRegime.LOW_VOLATILITY, stress, diag
        return VolRegime.NORMAL, stress, diag

    def update(self, h1: FeatureFrame, k: int, spread_shock: bool = False) -> RegimeState:
        """Call once per newly closed H1 bar (index k)."""
        trend, d1 = self.raw_trend(h1, k)
        vol, stress, d2 = self.raw_vol(h1, k)
        cur = self.state.trend
        if cur == TrendRegime.UNKNOWN or trend == TrendRegime.UNKNOWN:
            new_trend = trend
            self._candidate, self._candidate_count = None, 0
        elif trend == cur:
            new_trend = cur
            self._candidate, self._candidate_count = None, 0
        else:
            if trend == self._candidate:
                self._candidate_count += 1
            else:
                self._candidate, self._candidate_count = trend, 1
            new_trend = trend if self._candidate_count >= self.p.confirm_bars else cur
            if new_trend == trend:
                self._candidate, self._candidate_count = None, 0
        stress = stress or spread_shock
        label = "STRESS" if stress else str(new_trend)
        self.state = RegimeState(new_trend, vol, stress, label, int(h1.t[k]),
                                 {**d1, **d2, "raw_trend": str(trend), "candidate": str(self._candidate)})
        return self.state

    def with_fast_stress(self, spread_shock: bool) -> RegimeState:
        """Fast layer can flag stress between H1 bars."""
        if spread_shock and not self.state.stress:
            s = self.state
            return RegimeState(s.trend, s.vol, True, "STRESS", s.bar_time, {**s.diagnostics, "fast_stress": True})
        return self.state
