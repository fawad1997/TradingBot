"""Range Mean Reversion v1 (RESEARCH_ONLY by default - tracked in shadow).

H1 directional strength weak (ADX <= h1_adx_max). On M15, price closes back
inside the Bollinger band after closing outside it, with RSI exhaustion.
Target = band middle; stop beyond the excursion extreme.
"""
from __future__ import annotations

from typing import Dict

import numpy as np

from app.core.types import Side, TradePlan
from app.features.adaptive_levels import adaptive_extreme, adaptive_level
from app.strategies.base import EvalResult, Strategy, StrategyView, fin


class MeanReversionV1(Strategy):
    def lookbacks(self) -> Dict[str, int]:
        p = self.params
        return {"H1": 14 * 4, "M15": int(max(p["bb_len"], p["rsi_len"] * 4, p["atr_len"] * 3) + 10)}

    def evaluate(self, v: StrategyView) -> EvalResult:
        p = self.params
        m, i, h, k = v.m15, v.i, v.h1, v.k
        if i < 30 or k < 1:
            return EvalResult(None, ["INSUFFICIENT_HISTORY"])
        adx_series = h.adx(14)[0]
        adx = adx_series[k]
        if not fin(adx):
            return EvalResult(None, ["H1_FEATURES_NAN"])
        # "Ranging" is relative too. A fixed ADX ceiling of 18 is met constantly in a calm regime and almost
        # never in a trending one, so the strategy's trade rate swings with the regime rather than with the
        # setup. A multiple of ADX's own rolling mean holds the meaning steady. The configured 18 survives as
        # the CEILING, so this can only ever demand a quieter market, never a noisier one. 0 restores the
        # fixed level exactly.
        adx_max = adaptive_level(adx_series, k, configured=float(p["h1_adx_max"]),
                                 multiple=float(p.get("h1_adx_relative", 0.0)),
                                 lookback=int(p.get("adaptive_lookback", 500)),
                                 low=float(p.get("h1_adx_relative_min", 8.0)), high=float(p["h1_adx_max"]))
        if adx > adx_max:
            return EvalResult(None, ["H1_NOT_RANGE"])
        mid, up, lo = m.bollinger(int(p["bb_len"]), float(p["bb_k"]))
        rsi = m.rsi(int(p["rsi_len"]))
        atr = m.atr(int(p["atr_len"]))[i]
        if not fin(mid[i], up[i], lo[i], lo[i - 1], up[i - 1], rsi[i - 1], rsi[i - 2], atr) or atr <= 0:
            return EvalResult(None, ["M15_FEATURES_NAN"])
        # "Oversold" is a distance from the centre, so the live form measures it in RSI's OWN recent
        # dispersion: a calm market may never reach a fixed 30 (the rule never fires) while a violent one
        # reaches it constantly (the rule fires on noise). Bounded to 15-45 so it stays an extreme.
        rsi_low = adaptive_extreme(rsi, i, configured=float(p["rsi_extreme"]),
                                   sigma=float(p.get("rsi_extreme_sigma", 0.0)),
                                   lookback=int(p.get("adaptive_lookback", 500)), low=15.0, high=45.0)
        c = m.c
        if c[i - 1] < lo[i - 1] and c[i] > lo[i] and min(rsi[i - 1], rsi[i - 2]) <= rsi_low:
            side = Side.BUY
            entry = v.ask
            sl = float(np.min(m.l[i - 2:i + 1])) - p["sl_buffer_atr"] * atr
            tp = float(mid[i])
        elif c[i - 1] > up[i - 1] and c[i] < up[i] and max(rsi[i - 1], rsi[i - 2]) >= 100 - rsi_low:
            side = Side.SELL
            entry = v.bid
            sl = float(np.max(m.h[i - 2:i + 1])) + p["sl_buffer_atr"] * atr + v.spread_price
            tp = float(mid[i])
        else:
            return EvalResult(None, ["NO_BAND_REENTRY"])
        stop = abs(entry - sl)
        reward = (tp - entry) * side.sign
        if stop <= 0 or reward <= 0 or reward / stop < p["min_reward_r"]:
            return EvalResult(None, ["REWARD_TOO_SMALL"])
        key = f"{self.id}:{side}:{int(m.t[i - 1])}"
        q = float(min(1.0, (reward / stop) / 3.0))
        feats = {"h1_adx": float(adx), "rsi_prev": float(rsi[i - 1]), "atr": float(atr),
                 "band_width_atr": float((up[i] - lo[i]) / atr), "reward_r": float(reward / stop)}
        plan = TradePlan(direction=side, entry=float(entry), sl=float(sl), tp=tp, exit_model=dict(self.exit_model),
                         setup_key=key, quality=round(q, 3), ttl_seconds=int(p["signal_ttl_sec"]), features=feats)
        return EvalResult(plan, [])
