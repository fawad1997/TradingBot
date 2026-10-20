"""The five candidate entry rules as real strategies.

They run in the live engine on every bar, produce signals, and have their candidates, rejection reasons and
"would have been" results recorded exactly like the older strategies - but they ship with status SHADOW, so the
router marks them research-only and they cannot send an order. That is deliberate: a rule earns the right to
trade by its recorded results, not by being written.

The entry logic is NOT duplicated here. Each class calls the same function the research lab scores
(`app/research/lab/hypotheses.py`) over a trailing window and asks whether it fired on the last CLOSED bar, so the
live strategy and its backtest can never drift apart. The window is a few hundred bars: long enough for the
rule's own lookbacks, short enough to recompute every bar.

Promotion: when a rule's walk-forward (research lab) and its shadow results both hold up, change its `status` to
DEMO in config/strategies.json. Nothing else needs to change.
"""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np

from app.core.types import Side, TradePlan
from app.features.frame import FeatureFrame
from app.research.lab.hypotheses import (breakout_retest, htf_continuation, momentum_trend, session_breakout,
                                         vwap_reversion)
from app.strategies.base import EvalResult, Strategy, StrategyView, fin


def _tail(f: FeatureFrame, upto: int, bars: int) -> FeatureFrame:
    """The `bars` closed bars ending at `upto`, as a frame the rule functions can read."""
    lo = max(0, upto - bars + 1)
    return FeatureFrame(f.t[lo:upto + 1], f.o[lo:upto + 1], f.h[lo:upto + 1], f.l[lo:upto + 1], f.c[lo:upto + 1],
                        f.spread[lo:upto + 1], f.tick_volume[lo:upto + 1], f.timeframe)


class CandidateStrategy(Strategy):
    """Shared plumbing: slice, run the research rule, size the stop, build the plan."""

    rule = staticmethod(lambda frame, htf, params: {"long": np.zeros(len(frame), bool),
                                                    "short": np.zeros(len(frame), bool), "atr": frame.atr(14)})
    rule_params: tuple = ()
    needs_htf = False
    window = 400
    htf_window = 200

    def lookbacks(self) -> Dict[str, int]:
        return {"M15": int(self.window), "H1": int(self.htf_window) if self.needs_htf else 30}

    def _rule_params(self) -> dict:
        return {k: self.params[k] for k in self.rule_params if k in self.params}

    def evaluate(self, v: StrategyView) -> EvalResult:
        p = self.params
        m, i = v.m15, v.i
        if m is None or i is None or i < 60:
            return EvalResult(None, ["INSUFFICIENT_HISTORY"])
        frame = _tail(m, i, int(self.window))
        if len(frame) < 60:
            return EvalResult(None, ["INSUFFICIENT_HISTORY"])
        htf = None
        if self.needs_htf:
            if v.h1 is None or v.k is None or v.k < 30:
                return EvalResult(None, ["H1_HISTORY_UNAVAILABLE"])
            htf = _tail(v.h1, v.k, int(self.htf_window))
        try:
            sig = self.rule(frame, htf, self._rule_params())
        except Exception as exc:                       # a rule that raises must not stop the symbol's loop
            return EvalResult(None, [f"RULE_ERROR: {exc}"])
        j = len(frame) - 1
        atr_val = float(np.asarray(sig["atr"], float)[j])
        if not fin(atr_val) or atr_val <= 0:
            return EvalResult(None, ["ATR_NOT_READY"])
        if bool(sig["long"][j]):
            side = Side.BUY
        elif bool(sig["short"][j]):
            side = Side.SELL
        else:
            return EvalResult(None, ["NO_SETUP"])

        entry = v.ask if side is Side.BUY else v.bid
        spread_floor = float(p.get("min_stop_spread_multiple", 10.0)) * max(0.0, v.spread_price)
        stop_dist = max(float(p.get("stop_atr", 2.0)) * atr_val, spread_floor,
                        max(0.0, v.spread_price) + max(0.0, v.min_stop_distance))
        tp_dist = stop_dist * float(p.get("reward_risk", 1.5))
        sl = entry - side.sign * stop_dist
        tp = entry + side.sign * tp_dist
        if (side is Side.BUY and sl >= entry) or (side is Side.SELL and sl <= entry):
            return EvalResult(None, ["INVALID_STOP_SIDE"])
        plan = TradePlan(
            direction=side, entry=float(entry), sl=float(sl), tp=float(tp), exit_model=dict(self.exit_model),
            setup_key=f"{self.id}:{side}:{int(frame.t[j])}", quality=round(float(self.quality(sig, j, atr_val)), 3),
            ttl_seconds=int(p.get("signal_ttl_sec", 600)),
            features={"rule": self.id, "trigger_bar": int(frame.t[j]), "atr": atr_val,
                      "stop_distance": float(stop_dist), "stop_atr": float(stop_dist / atr_val),
                      "reward_risk": float(p.get("reward_risk", 1.5)),
                      "spread_price": float(v.spread_price),
                      "spread_fraction_of_stop": (float(v.spread_price) / stop_dist) if stop_dist > 0 else None,
                      **self.extra_features(sig, j, frame, htf)})
        return EvalResult(plan, [])

    # ------------------------------------------------------------------ hooks
    def quality(self, sig: dict, j: int, atr_val: float) -> float:
        return 0.5

    def extra_features(self, sig: dict, j: int, frame: FeatureFrame, htf: Optional[FeatureFrame]) -> dict:
        return {}


class BreakoutRetest(CandidateStrategy):
    rule = staticmethod(breakout_retest)
    rule_params = ("lookback", "wait", "tol_atr", "min_break_atr")
    window = 400

    def extra_features(self, sig, j, frame, htf):
        n = int(self.params.get("lookback", 40))
        lo = max(0, j - n)
        return {"range_high": float(np.max(frame.h[lo:j])), "range_low": float(np.min(frame.l[lo:j]))}


class MomentumTrend(CandidateStrategy):
    rule = staticmethod(momentum_trend)
    rule_params = ("fast", "slow", "mom_bars", "min_mom_atr")
    window = 400

    def extra_features(self, sig, j, frame, htf):
        ef = np.asarray(frame.ema(int(self.params.get("fast", 20))), float)
        es = np.asarray(frame.ema(int(self.params.get("slow", 50))), float)
        return {"ema_fast": float(ef[j]), "ema_slow": float(es[j]),
                "ema_gap_atr": float((ef[j] - es[j]) / np.asarray(sig["atr"], float)[j])}


class SessionBreakout(CandidateStrategy):
    rule = staticmethod(session_breakout)
    rule_params = ("range_from", "range_to", "trade_from", "trade_to", "min_range_atr", "buffer_atr")
    window = 300                                      # a day of M15 is 96 bars; this covers the session and spare


class VwapReversion(CandidateStrategy):
    rule = staticmethod(vwap_reversion)
    rule_params = ("min_dev_atr", "warmup_bars")
    window = 300


class HtfContinuation(CandidateStrategy):
    rule = staticmethod(htf_continuation)
    rule_params = ("htf_ema", "htf_slope_bars", "ema", "min_slope_atr")
    needs_htf = True
    window = 300
    htf_window = 200

    def extra_features(self, sig, j, frame, htf):
        if htf is None:
            return {}
        he = np.asarray(htf.ema(int(self.params.get("htf_ema", 20))), float)
        k = int(self.params.get("htf_slope_bars", 3))
        return {"h1_ema": float(he[-1]), "h1_slope": float(he[-1] - he[-1 - k]) if len(he) > k else None}


CANDIDATE_CLASSES = {"BreakoutRetest": BreakoutRetest, "MomentumTrend": MomentumTrend,
                     "SessionBreakout": SessionBreakout, "VwapReversion": VwapReversion,
                     "HtfContinuation": HtfContinuation}
