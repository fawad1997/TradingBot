"""Candlestick/momentum scalper with spread and broker-protection gates.

The trigger timeframe is a parameter (trigger_timeframe): "M1" (default) confirms with the last closed M15 candle,
"M15" confirms with the last closed H1 candle, "H1" with the last closed H4 candle. A slower trigger has a wider ATR
stop, so the spread is a smaller share of it: without an edge a trade costs about (spread / stop) R.
"""
from __future__ import annotations

from typing import Dict

import numpy as np

from app.core.types import Side, TradePlan
from app.features.frame import FeatureFrame
from app.strategies.base import EvalResult, Strategy, StrategyView
from app.strategies.entry_generator import EntryCandidate, confirm_relative_volume, detect_candlestick_trigger
from app.strategies.macro_filter import evaluate_macro_channel


class MicroScalper(Strategy):
    def lookbacks(self) -> Dict[str, int]:
        return {"H4": 40, "M1": 40, "M15": 20, "H1": 20}

    def _frames(self, v: StrategyView):
        """(trigger frame, trigger index, confirm frame, confirm index, confirm label)."""
        tf = str(self.params.get("trigger_timeframe", "M1")).upper()
        if tf == "M15":
            return v.m15, v.i, v.h1, v.k, "H1"
        if tf == "H1":
            return v.h1, v.k, v.h4, v.q, "H4"
        return v.m1, v.j, v.m15, v.i, "M15"

    def evaluate(self, v: StrategyView) -> EvalResult:
        m1, j, confirm, ci, confirm_tf = self._frames(v)
        if m1 is None or j is None or j < 20 or j >= len(m1):
            return EvalResult(None, ["NO_TRIGGER_TIMEFRAME_DATA"])
        if not v.spread_safe:
            return EvalResult(None, ["SPREAD_NOT_WITHIN_SAFE_RANGE" + (f" [{v.spread_why}]" if v.spread_why else "")])

        atr = m1.atr(14)
        atr_val = float(atr[j])
        if not np.isfinite(atr_val) or atr_val <= 0:
            return EvalResult(None, ["ATR_NOT_READY"])

        momentum = float(m1.c[j] - m1.c[j - 1])
        if not self.params.get("candlestick_triggers_enabled", True):
            return EvalResult(None, ["CANDLESTICK_TRIGGERS_DISABLED"])
        trigger = detect_candlestick_trigger(m1, j)
        if trigger is None:
            return EvalResult(None, ["NO_CANDLESTICK_TRIGGER"])
        side, trigger_pattern = trigger

        if (confirm is None or ci is None or ci < 0 or ci >= len(confirm)
                or not np.isfinite(confirm.o[ci]) or not np.isfinite(confirm.c[ci])):
            return EvalResult(None, [f"{confirm_tf}_CONFIRMATION_UNAVAILABLE"])
        m15_open, m15_close = float(confirm.o[ci]), float(confirm.c[ci])
        if ((side is Side.BUY and m15_close < m15_open)
                or (side is Side.SELL and m15_close > m15_open)):
            return EvalResult(None, [f"{confirm_tf}_DIRECTION_NOT_CONFIRMED"])

        relative_volume = confirm_relative_volume(
            m1, j, int(self.params.get("volume_lookback", 20)),
            float(self.params.get("minimum_relative_volume", 1.2)))
        if relative_volume is None:
            return EvalResult(None, ["LOW_OR_UNAVAILABLE_TICK_VOLUME"])
        macro = None
        if self.params.get("macro_filter_enabled", True):
            macro = evaluate_macro_channel(
                v.h4, v.q, float(m1.c[j]), side,
                int(self.params.get("macro_channel_window", 40)),
                float(self.params.get("macro_extreme_fraction", 0.2)))
            if macro is None:
                return EvalResult(None, ["H4_MACRO_CHANNEL_UNAVAILABLE"])
            if not macro.is_at_channel_extreme:
                return EvalResult(None, ["MACRO_CHANNEL_MIDDLE_OR_COUNTERTREND"])

        entry = v.ask if side is Side.BUY else v.bid
        candidate = EntryCandidate(v.symbol, side, float(entry), trigger_pattern, relative_volume)
        side, trigger_pattern, relative_volume = (
            candidate.direction, candidate.pattern, candidate.relative_volume)
        minimum_distance = max(0.0, v.spread_price) + max(0.0, v.min_stop_distance)
        # The spread is paid out of the stop: expectancy without an edge is about -(spread / stop) R per trade. The
        # stop is therefore floored at a multiple of the CURRENT spread, not only at an ATR multiple.
        spread_floor = float(self.params.get("min_stop_spread_multiple", 10.0)) * max(0.0, v.spread_price)
        stop_dist = max(float(self.params.get("stop_atr_multiple", 1.0)) * atr_val, minimum_distance, spread_floor)
        tp_dist = max(float(self.params.get("target_atr_multiple", 1.0)) * atr_val, minimum_distance,
                      stop_dist * float(self.params.get("min_reward_risk", 1.0)))
        sl = entry - stop_dist if side is Side.BUY else entry + stop_dist
        tp = entry + tp_dist if side is Side.BUY else entry - tp_dist
        key = f"{self.id}:{side}:{int(m1.t[j])}"
        trigger_move = max(abs(momentum), abs(float(m1.c[j] - m1.o[j])))

        plan = TradePlan(
            direction=side,
            entry=float(entry),
            sl=float(sl),
            tp=float(tp),
            exit_model=dict(self.exit_model),
            setup_key=key,
            quality=min(1.0, trigger_move / atr_val),
            ttl_seconds=int(self.params.get("signal_ttl_sec", 60)),
            features={
                "trigger_timeframe": str(self.params.get("trigger_timeframe", "M1")).upper(),
                "c": float(m1.c[j]),
                "atr_val": atr_val,
                "momentum": momentum,
                "momentum_atr": abs(momentum) / atr_val,
                "trigger_pattern": trigger_pattern,
                "entry_candidate": candidate.as_dict(),
                "relative_tick_volume": relative_volume,
                "h4_channel_slope": macro.slope if macro else None,
                "h4_channel_lower": macro.lower if macro else None,
                "h4_channel_upper": macro.upper if macro else None,
                "h4_channel_position": macro.position if macro else None,
                "stop_distance": stop_dist,
                "target_distance": tp_dist,
                "stop_atr": stop_dist / atr_val,
                "target_atr": tp_dist / atr_val,
                "minimum_distance": minimum_distance,
                "spread_price": float(v.spread_price),
                "spread_fraction_of_stop": (float(v.spread_price) / stop_dist) if stop_dist > 0 else None,
            },
        )
        return EvalResult(plan, [])
