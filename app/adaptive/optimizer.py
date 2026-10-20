"""Hourly adaptive parameters (bounded, smoothed, tighten-only for risk).

What adapts, per symbol, from the symbol's own recent M15 bars (no extra broker calls):
  * vol_scale        - short-term vs long-term ATR ratio. Scales the news-straddle distances (trigger / SL / TP)
                       so they widen when volatility has expanded and narrow when it has contracted.
  * max_spread_points - a per-symbol spread ceiling from the recent spread distribution. It can only TIGHTEN the
                       global spread_gate.max_spread_points, never loosen it. "Tighten only" means tighter than
                       the global ceiling ON THIS CYCLE - it is NOT a ratchet. Every cycle recomputes the cap from
                       scratch off the recent spread distribution and assigns it, so when spreads widen the cap
                       widens with them and reverts to None (the global ceiling) once it is no longer tighter.
                       What it cannot do by itself is expire: see `effective_max_spread_points`.

Deliberately NOT adapted: lot size, risk fractions, loss limits, stop-loss caps of the strategies, kill switch.
Why not a parameter grid search on the last hours of bars: with tens of bars a "best of N combinations" is mostly
noise (curve fitting). Volatility scaling is a single, low-variance, monotone adjustment, clamped to hard bounds and
moved at most `max_step` per cycle, so a bad hour cannot swing the system.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence


@dataclass
class AdaptiveResult:
    vol_scale: float = 1.0
    max_spread_points: Optional[float] = None     # None = use the global ceiling
    updated: bool = False
    reason: str = ""
    metrics: dict = field(default_factory=dict)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def compute(prev_scale: float, atr_series: Sequence[float], spreads_points: Sequence[float],
            global_max_spread: float, cfg) -> AdaptiveResult:
    """cfg is AdaptiveCfg. Returns the new parameters (or the previous ones with updated=False)."""
    finite = [float(a) for a in atr_series if a is not None and math.isfinite(a) and a > 0]
    res = AdaptiveResult(vol_scale=prev_scale)
    if len(finite) < cfg.atr_long_bars + 1:
        res.reason = f"INSUFFICIENT_ATR_HISTORY ({len(finite)})"
        return res
    short = statistics.fmean(finite[-cfg.atr_short_bars:])
    long_ = statistics.median(finite[-cfg.atr_long_bars:])
    ratio = short / long_
    target = _clamp(ratio, cfg.scale_min, cfg.scale_max)
    step = _clamp(target - prev_scale, -cfg.max_step, cfg.max_step)
    res.vol_scale = round(_clamp(prev_scale + step, cfg.scale_min, cfg.scale_max), 4)
    res.metrics = {"atr_short": short, "atr_long_median": long_, "atr_ratio": round(ratio, 4),
                   "target_scale": round(target, 4)}
    sp = sorted(float(s) for s in spreads_points if s is not None and math.isfinite(s) and s > 0)
    if len(sp) >= cfg.spread_min_samples:
        p90 = sp[int(0.9 * (len(sp) - 1))]
        med = statistics.median(sp)
        cap = max(p90 * cfg.spread_p90_mult, med * 2.0)
        res.metrics.update({"spread_median": med, "spread_p90": p90, "spread_samples": len(sp)})
        res.max_spread_points = round(cap, 1) if cap < global_max_spread * 0.999 else None   # tighten only
    res.updated = True
    res.reason = "OK"
    return res


class AdaptiveState:
    """Per-symbol live parameters; read by the trading loop on every evaluation (hot reload, no restart)."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.vol_scale = 1.0
        self.max_spread_points: Optional[float] = None
        self.updated_at: Optional[str] = None
        self.metrics: dict = {}
        self.last_reason = "NOT_RUN"

    def apply(self, res: AdaptiveResult) -> None:
        self.last_reason = res.reason
        if not res.updated:
            return
        self.vol_scale = res.vol_scale
        self.max_spread_points = res.max_spread_points
        self.metrics = res.metrics
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def cap_age_minutes(self, now: Optional[datetime] = None) -> Optional[float]:
        """Age of the stored spread cap, or None if there is no cap or no usable timestamp."""
        if self.max_spread_points is None or not self.updated_at:
            return None
        try:
            ts = datetime.fromisoformat(str(self.updated_at))
        except ValueError:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ((now or datetime.now(timezone.utc)) - ts).total_seconds() / 60.0

    def effective_max_spread_points(self, max_age_minutes: float,
                                    now: Optional[datetime] = None) -> Optional[float]:
        """The spread cap, but only while it is fresh.

        `apply()` keeps the previous value whenever a refresh cannot run, and `restore()` reloads it after a
        restart, so the stored cap can outlive the market conditions that produced it. An expired cap is
        discarded here rather than quietly tightening the spread gate forever."""
        if self.max_spread_points is None:
            return None
        age = self.cap_age_minutes(now)
        if age is None:                      # a cap with no timestamp cannot be shown to be fresh
            return None
        return None if age > max_age_minutes else self.max_spread_points

    def to_dict(self) -> dict:
        return {"vol_scale": self.vol_scale, "max_spread_points": self.max_spread_points,
                "updated_at": self.updated_at, "metrics": self.metrics, "last_reason": self.last_reason}

    def restore(self, d: Optional[dict]) -> None:
        if isinstance(d, dict):
            self.vol_scale = float(d.get("vol_scale", 1.0))
            self.max_spread_points = d.get("max_spread_points")
            self.updated_at = d.get("updated_at")
            self.metrics = d.get("metrics") or {}
