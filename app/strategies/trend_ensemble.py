"""Multi-speed time-series momentum as a live strategy (see app/research/lab/hypotheses.py, trend_ensemble).

Everything but the entry rule is TrendDaily's: the same UTC daily bars built from the H4 store, the same memo, and
the same exit - a 3 x ATR(100) stop trailed at that distance from the best price, no breakeven, no profit lock, a
far target only because the broker wants one. So the two trend strategies differ in one thing, which is what a
side-by-side test of them should isolate: TrendDaily waits for a 50-day breakout, this one trades while the 1-,
3-, 6- and 12-month returns all point the same way.

The lab replays the same function over the H4 history (app/research/lab/trend.py, research_trend with
rule=trend_ensemble), so the live strategy and its backtest cannot drift apart.
"""
from __future__ import annotations

import numpy as np

from app.features.frame import FeatureFrame
from app.research.lab.hypotheses import TREND_ENSEMBLE_DEFAULTS, trend_ensemble
from app.strategies.trend_daily import TrendDaily


class TrendEnsemble(TrendDaily):
    rule = staticmethod(trend_ensemble)
    defaults = TREND_ENSEMBLE_DEFAULTS
    name = "trend_ensemble"

    def rule_params(self) -> dict:
        p = super().rule_params()
        p["lookbacks"] = tuple(int(x) for x in p["lookbacks"])
        return p

    def min_days(self, p: dict) -> int:
        # a year of a five-day-a-week market; the rule itself waits for each lookback's own history
        return int(max(p["lookbacks"]) * 5 / 7) + 1

    def day_features(self, daily: FeatureFrame, sig: dict, j: int, p: dict) -> dict:
        return {"score": float(np.asarray(sig["score"], float)[j]), "lookbacks_days": list(p["lookbacks"])}
