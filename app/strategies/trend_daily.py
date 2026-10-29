"""Daily trend following as a live strategy (Clenow's rule; see app/research/lab/trend.py for the why).

The entry logic is NOT duplicated here: `evaluate` builds the same UTC daily bars from the H4 store and calls the
same `trend_daily` function the research lab replays over a decade of history, so the live strategy and its
backtest cannot drift apart. A day becomes visible once it has ENDED (the close time of the current M15 bar is
past midnight UTC), which is also the boundary the lab uses.

The exit is what makes this a trend strategy rather than another scalper:
  * the stop is 3 x the 100-day ATR, and it TRAILS at that same fixed distance from the best price since entry
    (a chandelier exit; `trail.distance_price` is set per trade, because the trade manager only ever hands the
    exit an M15 frame, whose ATR would turn a daily trail into an intraday one);
  * no breakeven and no profit lock - both would bank a fraction of a small move and end the trend early;
  * the broker requires a take-profit on every position, so the target is `far_target_r` (20R), far enough to
    almost never bind. The lab simulates the same target.

One open position per symbol, as in the simulator: `allow_scale_in` is false, so the engine never adds to it.
"""
from __future__ import annotations

from typing import Dict

import numpy as np

from app.core.types import Side, TradePlan
from app.features.frame import FeatureFrame
from app.research.lab.hypotheses import FAR_TARGET_R, TREND_DAILY_DEFAULTS, daily_bars, trend_daily
from app.strategies.base import EvalResult, Strategy, StrategyView, fin

#: H4 bars handed to the rule. FX has 6 a day, so this is about 500 trading days - enough to warm a 100-day EMA
#: that started from a different bar than the lab's to within a fraction of a point.
H4_WINDOW = 3000


def trend_exit(distance: float, far_target_r: float, base: dict = None) -> dict:
    """The exit every TrendDaily trade carries (the lab's `trend.trend_exit_model`, plus the far target)."""
    model = dict(base or {})
    model.update({"type": "chandelier", "target_r": float(far_target_r),
                  "trail": {"activate_r": 0.0, "distance_price": float(distance)},
                  "breakeven": {"enabled": False}, "profit_lock": {"enabled": False},
                  "time_stop": {"enabled": False}, "thesis_invalidation": False})
    return model


class TrendDaily(Strategy):
    # The daily rule traded, and its defaults. Subclasses (trend_ensemble.py, turn_of_month.py) swap these and
    # inherit the rest: the same day boundaries, the memo, and the rule the lab replays.
    rule = staticmethod(trend_daily)
    defaults = TREND_DAILY_DEFAULTS
    name = "trend_daily"

    def rule_params(self) -> dict:
        return {k: self.params.get(k, v) for k, v in self.defaults.items()}

    def min_days(self, p: dict) -> int:
        """Completed days needed before the rule can be read at all."""
        return max(int(p["ema_slow"]), int(p["atr_days"]), int(p["breakout_days"])) + 1

    def day_features(self, daily: FeatureFrame, sig: dict, j: int, p: dict) -> dict:
        """What the signal day looked like, stored on the signal."""
        return {"ema_fast": float(np.asarray(daily.ema(int(p["ema_fast"])), float)[j]),
                "ema_slow": float(np.asarray(daily.ema(int(p["ema_slow"])), float)[j])}

    def exit_for(self, v: StrategyView, dist: float, far: float) -> dict:
        return trend_exit(dist, far, self.exit_model)

    def lookbacks(self) -> Dict[str, int]:
        # Hydration fetches this x ema_warmup_multiple (5) H4 bars - about 3,900, enough to warm a daily EMA100 -
        # and `h4_lookback` stays below what the thinnest symbol has (about 980 for the Exness stock CFDs, 20 Oct
        # 2026).
        return {"M15": 60, "H4": int(self.params.get("h4_lookback", 600))}

    def min_history(self) -> Dict[str, int]:
        # Nothing on H4 is REQUIRED to start: with too little history evaluate() answers INSUFFICIENT_HISTORY, and
        # the other strategies on the symbol keep trading.
        return {"M15": 60}

    def _daily_decision(self, v: StrategyView, p: dict):
        """(reason or None, side, distance, signal day, daily close, day features) for the last COMPLETED day.

        It only changes when a day ends or a new H4 bar closes, but evaluate() runs on every M15 bar (and every few
        seconds live), so the answer is cached on exactly those inputs. Rebuilding it per call made the two-year
        backtest of this strategy take five minutes a symbol."""
        lo = max(0, int(v.q) - H4_WINDOW + 1)
        hi = int(v.q) + 1
        key = (int(v.h4.t[lo]), int(v.h4.t[hi - 1]), float(v.h4.c[hi - 1]), int(v.close_time()) // 86400, hi - lo)
        if getattr(self, "_memo_key", None) == key:
            return self._memo_val
        h4 = FeatureFrame(v.h4.t[lo:hi], v.h4.o[lo:hi], v.h4.h[lo:hi], v.h4.l[lo:hi], v.h4.c[lo:hi],
                          v.h4.spread[lo:hi], v.h4.tick_volume[lo:hi], "H4")
        daily = daily_bars(h4, until=v.close_time())
        if len(daily) < self.min_days(p):
            val = ("INSUFFICIENT_HISTORY", None, None, None, None, {})
        else:
            sig = self.rule(daily, None, p)
            j = len(daily) - 1
            dist = float(np.asarray(sig["dist"], float)[j])
            side = Side.BUY if bool(sig["long"][j]) else (Side.SELL if bool(sig["short"][j]) else None)
            reason = ("ATR_NOT_READY" if not fin(dist) or dist <= 0 else (None if side is not None else "NO_SETUP"))
            val = (reason, side, dist, int(daily.t[j]), float(daily.c[j]), self.day_features(daily, sig, j, p))
        self._memo_key, self._memo_val = key, val
        return val

    def evaluate(self, v: StrategyView) -> EvalResult:
        p = self.rule_params()
        if v.h4 is None or v.q is None or v.q < 0:
            return EvalResult(None, ["H4_HISTORY_UNAVAILABLE"])
        reason, side, dist, day, daily_close, day_features = self._daily_decision(v, p)
        if reason is not None:
            return EvalResult(None, [reason])
        entry = v.ask if side is Side.BUY else v.bid
        if not fin(entry) or entry <= 0:
            return EvalResult(None, ["NO_PRICE"])
        dist = max(dist, max(0.0, v.spread_price) + max(0.0, v.min_stop_distance))
        far = float(self.params.get("far_target_r", FAR_TARGET_R))
        sl = entry - side.sign * dist
        tp = entry + side.sign * far * dist
        if (side is Side.BUY and sl >= entry) or (side is Side.SELL and sl <= entry):
            return EvalResult(None, ["INVALID_STOP_SIDE"])
        plan = TradePlan(
            direction=side, entry=float(entry), sl=float(sl), tp=float(tp),
            exit_model=self.exit_for(v, dist, far),
            setup_key=f"{self.id}:{side}:{day}", quality=0.5,
            ttl_seconds=int(self.params.get("signal_ttl_sec", 900)),
            features={"rule": self.name, "signal_day": day, "daily_close": daily_close,
                      "atr_daily": float(dist / float(p["stop_atr"])), "stop_distance": float(dist),
                      "trail_distance": float(dist), "far_target_r": far,
                      **day_features,
                      "spread_price": float(v.spread_price),
                      "spread_fraction_of_stop": float(v.spread_price) / dist if dist > 0 else None})
        return EvalResult(plan, [])
