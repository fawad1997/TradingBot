"""XAUUSD Pullback v1 - mechanical definition of the section-9 hypothesis.

Hypothesis (NOT a proven edge): a directional H1 market followed by an M15
pullback with renewed momentum may have positive expectancy under specific
session / volatility conditions.

TREND VALID       H1: EMA_fast vs EMA_slow aligned, close beyond EMA_fast,
                  ATR-normalised EMA slope >= h1_slope_min, ADX >= h1_adx_min, DI aligned.
PULLBACK VALID    M15: an impulse >= impulse_min_atr * ATR inside the lookback, then a
                  retracement of [min_depth, max_depth] of that impulse lasting
                  <= pullback_max_bars that reaches the M15 EMA zone.
MOMENTUM          last closed M15 bar closes in trend direction, beyond the previous
                  bar's extreme and beyond the M15 EMA; RSI reclaims rsi_reclaim and rises.
INVALIDATION      stop beyond the pullback extreme + sl_buffer_atr * ATR (+ spread for sells).
Session, spread, cost/edge and risk are separate gates in the pipeline.
"""
from __future__ import annotations

from typing import Dict

import numpy as np

from app.core.types import Side, TradePlan
from app.features.adaptive_levels import adaptive_level, adaptive_midline
from app.strategies.base import EvalResult, Strategy, StrategyView, fin


class PullbackV1(Strategy):
    def lookbacks(self) -> Dict[str, int]:
        p = self.params
        h1 = int(max(p["h1_ema_slow"], p["h1_ema_fast"] + p["h1_slope_len"], 14 * 4))
        m15 = int(p["impulse_lookback"] + max(p["m15_ema"] * 3, p["atr_len"] * 3, p["rsi_len"] * 4) + 10)
        return {"H1": h1, "M15": m15}

    def evaluate(self, v: StrategyView) -> EvalResult:
        p = self.params
        m, i, h, k = v.m15, v.i, v.h1, v.k
        L = int(p["impulse_lookback"])
        if i < L + 5 or k < 1:
            return EvalResult(None, ["INSUFFICIENT_HISTORY"])

        # ---------------- H1 trend
        ef = h.ema(int(p["h1_ema_fast"]))[k]
        es = h.ema(int(p["h1_ema_slow"]))[k]
        adx_a, pdi_a, mdi_a = h.adx(14)
        adx, pdi, mdi = adx_a[k], pdi_a[k], mdi_a[k]
        slope = h.ema_slope(int(p["h1_ema_fast"]), int(p["h1_slope_len"]), int(p["atr_len"]))[k]
        hc = h.c[k]
        if not fin(ef, es, adx, pdi, mdi, slope):
            return EvalResult(None, ["H1_FEATURES_NAN"])
        # "Trending" is relative to how this symbol has been behaving. A raw ADX level gates almost everything
        # out in a quiet regime and almost nothing out in a violent one; a multiple of ADX's own rolling mean
        # keeps the same meaning in both. The configured 15 survives as the floor, so this can only ever ask
        # for MORE strength than before, never less. adx_relative = 0 restores the fixed level exactly.
        adx_min = adaptive_level(adx_a, k, configured=float(p["h1_adx_min"]),
                                 multiple=float(p.get("h1_adx_relative", 0.0)),
                                 lookback=int(p.get("adaptive_lookback", 500)),
                                 low=float(p["h1_adx_min"]), high=float(p.get("h1_adx_relative_max", 40.0)))
        if ef > es and hc > ef and slope >= p["h1_slope_min"] and adx >= adx_min and pdi > mdi:
            side = Side.BUY
        elif ef < es and hc < ef and slope <= -p["h1_slope_min"] and adx >= adx_min and mdi > pdi:
            side = Side.SELL
        else:
            return EvalResult(None, ["H1_TREND_INVALID"])

        # ---------------- M15 structure
        atr = m.atr(int(p["atr_len"]))[i]
        ema = m.ema(int(p["m15_ema"]))
        rsi = m.rsi(int(p["rsi_len"]))
        if not fin(atr, ema[i], rsi[i], rsi[i - 1]) or atr <= 0:
            return EvalResult(None, ["M15_FEATURES_NAN"])
        # 50 is RSI's definitional midline, so the live form is not a multiple but the centre this market is
        # actually trading around: in a persistent trend RSI's own median drifts away from 50, and "momentum
        # reclaimed the midline" should mean THAT centre. Bounded to 40-60 so it stays a midline.
        rsi_mid = adaptive_midline(rsi, i, configured=float(p["rsi_reclaim"]),
                                   enabled=bool(p.get("rsi_reclaim_adaptive", False)),
                                   lookback=int(p.get("adaptive_lookback", 500)), low=40.0, high=60.0)
        lo = i - L
        if side is Side.BUY:
            jh = lo + int(np.argmax(m.h[lo:i]))
            ext = m.h[jh]
            j0 = lo + int(np.argmin(m.l[lo:jh + 1]))
            origin = m.l[j0]
            if jh >= i:
                return EvalResult(None, ["NO_PULLBACK"])
            jp = jh + 1 + int(np.argmin(m.l[jh + 1:i + 1]))
            pb = m.l[jp]
            impulse = ext - origin
            depth = (ext - pb) / impulse if impulse > 0 else np.nan
            touched = pb <= ema[jp] + p["ema_touch_atr"] * atr if fin(ema[jp]) else False
        else:
            jh = lo + int(np.argmin(m.l[lo:i]))
            ext = m.l[jh]
            j0 = lo + int(np.argmax(m.h[lo:jh + 1]))
            origin = m.h[j0]
            if jh >= i:
                return EvalResult(None, ["NO_PULLBACK"])
            jp = jh + 1 + int(np.argmax(m.h[jh + 1:i + 1]))
            pb = m.h[jp]
            impulse = origin - ext
            depth = (pb - ext) / impulse if impulse > 0 else np.nan
            touched = pb >= ema[jp] - p["ema_touch_atr"] * atr if fin(ema[jp]) else False

        reasons = []
        if not (j0 < jh):
            reasons.append("NO_IMPULSE")
        if impulse < p["impulse_min_atr"] * atr:
            reasons.append("IMPULSE_TOO_SMALL")
        if not fin(depth) or not (p["pullback_min_depth"] <= depth <= p["pullback_max_depth"]):
            reasons.append("PULLBACK_DEPTH")
        if i - jh > p["pullback_max_bars"]:
            reasons.append("PULLBACK_TOO_LONG")
        if not touched:
            reasons.append("NO_EMA_TOUCH")

        # ---------------- momentum confirmation on the last CLOSED bar
        s = side.sign
        c, o = m.c[i], m.o[i]
        if side is Side.BUY:
            mom = c > o and c > m.h[i - 1] and c > ema[i] and rsi[i] >= rsi_mid and rsi[i] > rsi[i - 1]
            resumed = c >= ext
        else:
            mom = c < o and c < m.l[i - 1] and c < ema[i] and rsi[i] <= 100 - rsi_mid and rsi[i] < rsi[i - 1]
            resumed = c <= ext
        if not mom:
            reasons.append("MOMENTUM_NOT_CONFIRMED")
        if resumed:
            reasons.append("ALREADY_RESUMED")
        if reasons:
            return EvalResult(None, reasons)

        # ---------------- trade plan
        entry = v.ask if side is Side.BUY else v.bid
        buffer = p["sl_buffer_atr"] * atr
        sl = pb - buffer if side is Side.BUY else pb + buffer + v.spread_price
        stop = abs(entry - sl)
        if (side is Side.BUY and sl >= entry) or (side is Side.SELL and sl <= entry):
            return EvalResult(None, ["INVALID_STOP_SIDE"])
        if stop < p["min_stop_atr"] * atr or stop > p["max_stop_atr"] * atr:
            return EvalResult(None, ["STOP_DISTANCE"])
        target_r = self.exit_model.get("target_r")
        tp = entry + s * float(target_r) * stop if target_r else None

        key = f"{self.id}:{side}:{int(m.t[jp])}"
        q = (0.25 * min(adx / 40.0, 1.0)
             + 0.25 * max(0.0, 1.0 - abs(depth - 0.45) / 0.45)
             + 0.25 * max(0.0, min((rsi[i] - 50.0) * s / 20.0, 1.0))
             + 0.25 * min(impulse / (4.0 * atr), 1.0))
        feats = {"h1_adx": float(adx), "h1_slope": float(slope), "depth": float(depth),
                 "impulse_atr": float(impulse / atr), "rsi": float(rsi[i]), "atr": float(atr),
                 "stop_atr": float(stop / atr), "pullback_bars": int(i - jh), "pullback_extreme": float(pb),
                 "impulse_extreme": float(ext)}
        plan = TradePlan(direction=side, entry=float(entry), sl=float(sl), tp=float(tp) if tp else None,
                         exit_model=dict(self.exit_model), setup_key=key, quality=round(float(q), 3),
                         ttl_seconds=int(p["signal_ttl_sec"]), features=feats)
        return EvalResult(plan, [])
