"""Turn of the month on an equity index, as a live strategy (see app/research/lab/hypotheses.py, turn_of_month).

The rule is read on the same UTC daily bars TrendDaily builds, so the signal appears once the second-to-last
weekday of the month has ENDED and the trade opens on the last one. What differs is the exit: a fixed protective
stop (3 x ATR(20)), and a time exit after `hold_days` trading days - the last of the month and the first three of
the next. The trade manager counts M15 bars, so the hold is converted to bars from this symbol's own recent days
(an index CFD trades about 23 hours, a stock CFD about 9); breakeven, the profit lock and trailing are off, since
the thesis is the calendar, not the path.

Long only: the effect is the equity premium arriving at month ends (McConnell & Xu 2008).
"""
from __future__ import annotations

import numpy as np

from app.features.frame import FeatureFrame
from app.research.lab.hypotheses import TURN_OF_MONTH_DEFAULTS, turn_of_month
from app.strategies.base import StrategyView
from app.strategies.trend_daily import TrendDaily

#: M15 bars in a trading day when the frame is too short to measure it (an index CFD's ~23 h session)
FALLBACK_BARS_PER_DAY = 92


def hold_exit(max_bars: int, far_target_r: float, base: dict = None) -> dict:
    """A fixed stop, no trailing and no profit taking before an unconditional exit after `max_bars` bars."""
    model = dict(base or {})
    model.update({"type": "fixed", "target_r": float(far_target_r),
                  "time_stop": {"enabled": True, "max_bars": int(max_bars), "min_r": 1e9},
                  "trail": {}, "partial": {"enabled": False},
                  "breakeven": {"enabled": False}, "profit_lock": {"enabled": False},
                  "thesis_invalidation": False})
    return model


def bars_per_day(m15: FeatureFrame, upto: int, days: int = 20) -> int:
    """Median M15 bars in this symbol's recent full days (a part day at either end is not counted)."""
    t = np.asarray(m15.t[max(0, upto - days * 100):upto + 1], dtype=np.int64)
    if len(t) == 0:
        return FALLBACK_BARS_PER_DAY
    _, counts = np.unique(t // 86400, return_counts=True)
    full = counts[1:-1] if len(counts) > 2 else counts
    full = full[full >= 8]
    return int(np.median(full)) if len(full) else FALLBACK_BARS_PER_DAY


class TurnOfMonth(TrendDaily):
    rule = staticmethod(turn_of_month)
    defaults = TURN_OF_MONTH_DEFAULTS
    name = "turn_of_month"

    def min_days(self, p: dict) -> int:
        return int(p["atr_days"]) + 2

    def day_features(self, daily: FeatureFrame, sig: dict, j: int, p: dict) -> dict:
        return {"hold_days": int(p["hold_days"]), "trail_distance": None}

    def exit_for(self, v: StrategyView, dist: float, far: float) -> dict:
        hold = int(self.rule_params()["hold_days"]) * bars_per_day(v.m15, v.i)
        return hold_exit(hold, far, self.exit_model)
