"""Why a strategy produces no setups: its own code, replayed bar by bar, with a count at every condition.

This calls the PRODUCTION strategy object (`evaluate`) on each closed M15 bar of real history, so the counts are
the live logic's, not a copy of it that could drift. Two things come back:

* `reasons` - how many bars died at each named reason, in the order the code tests them, ending in SETUP.
* `conditions` - for the conjunction that usually blocks everything (the H1 regime test), how often EACH clause
  passes on its own, and the value a clause would need for a given share of bars to pass it.

A conjunction of five clauses each passing 40% of the time fires on 1% of bars; the per-clause rates say which one
to question, and the quantiles say what it would have to become. That is a diagnosis, not permission to loosen a
threshold: a rule relaxed until it trades is an overfitted rule, and the lab's walk-forward still decides.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np

from app.core.types import Side
from app.features.frame import FeatureFrame
from app.strategies.base import StrategyView

log = logging.getLogger("research_lab")

# the order each strategy's own code tests things, so the funnel reads top to bottom
ORDER = {
    "pullback_v1": ["INSUFFICIENT_HISTORY", "H1_FEATURES_NAN", "H1_TREND_INVALID", "M15_FEATURES_NAN",
                    "NO_PULLBACK", "NO_IMPULSE", "IMPULSE_TOO_SMALL", "PULLBACK_DEPTH", "PULLBACK_TOO_LONG",
                    "NO_EMA_TOUCH", "MOMENTUM_NOT_CONFIRMED", "ALREADY_RESUMED", "INVALID_STOP_SIDE",
                    "STOP_DISTANCE", "SETUP"],
    "mean_reversion_v1": ["INSUFFICIENT_HISTORY", "H1_FEATURES_NAN", "H1_NOT_RANGE", "M15_FEATURES_NAN",
                          "NO_BAND_REENTRY", "REWARD_TOO_SMALL", "SETUP"],
}

# The five candidate rules share one evaluate() (app/strategies/candidates.py), so they share one reason order.
# They were missing from ORDER, and because diagnose() skips any strategy it has no order for, the funnel card
# silently excluded exactly the strategies most in need of explaining. Replaying all seven costs ~47s per symbol
# over the 90-day window, ~10 min per lab cycle across 13 symbols, in the low-priority research process.
_CANDIDATE_ORDER = ["INSUFFICIENT_HISTORY", "H1_HISTORY_UNAVAILABLE", "ATR_NOT_READY", "NO_SETUP",
                    "INVALID_STOP_SIDE", "SETUP"]
for _sid in ("breakout_retest_v1", "momentum_trend_v1", "session_breakout_v1", "vwap_reversion_v1",
             "htf_continuation_v1"):
    ORDER[_sid] = list(_CANDIDATE_ORDER)


def _h1_index(m15: FeatureFrame, h1: FeatureFrame) -> np.ndarray:
    """The last H1 bar that had CLOSED when each M15 bar closed."""
    return np.searchsorted(np.asarray(h1.t, np.int64) + 3600,
                           np.asarray(m15.t, np.int64) + 900, side="right") - 1


def replay(strategy, m15: FeatureFrame, h1: FeatureFrame, point: float = 0.01,
           spread_price: float = 0.0, symbol: str = "") -> dict:
    """Count how many closed M15 bars reach each condition of this strategy's own evaluate()."""
    idx = _h1_index(m15, h1)
    counts: Dict[str, int] = {}
    setups = 0
    evaluated = 0
    for i in range(len(m15)):
        k = int(idx[i])
        if k < 1:
            continue
        evaluated += 1
        mid = float(m15.c[i])
        view = StrategyView(m15=m15, i=i, h1=h1, k=k, symbol=symbol, point=point,
                            spread_price=spread_price, bid=mid - spread_price / 2.0, ask=mid + spread_price / 2.0,
                            spread_safe=True, session="LONDON", regime_trend="UNKNOWN")
        try:
            res = strategy.evaluate(view)
        except Exception as exc:                 # a strategy that raises is a finding, not a crash
            counts["ERROR"] = counts.get("ERROR", 0) + 1
            if counts["ERROR"] == 1:
                log.warning("%s raised during funnel replay: %s", getattr(strategy, "id", "?"), exc)
            continue
        if res.plan is not None:
            setups += 1
            counts["SETUP"] = counts.get("SETUP", 0) + 1
            continue
        for reason in (res.reasons or ["NO_REASON"]):
            counts[reason] = counts.get(reason, 0) + 1
    order = ORDER.get(getattr(strategy, "id", ""), [])
    named = [r for r in order if r in counts] + sorted(r for r in counts if r not in order)
    return {"bars": len(m15), "evaluated": evaluated, "setups": setups,
            "setups_per_week": round(setups / max(1e-9, len(m15) * 900 / 604800.0), 2),
            "reasons": {r: counts[r] for r in named}}


def _rate(mask: np.ndarray) -> float:
    m = np.asarray(mask, bool)
    return round(float(m.mean()), 4) if len(m) else 0.0


def _q(x: np.ndarray, qs=(0.5, 0.75, 0.9)) -> Dict[str, float]:
    v = np.asarray(x, float)
    v = v[np.isfinite(v)]
    if not len(v):
        return {}
    return {f"p{int(q * 100)}": round(float(np.quantile(v, q)), 4) for q in qs}


def h1_conditions(strategy_id: str, params: dict, h1: FeatureFrame) -> dict:
    """Pass rate of each clause of the H1 regime test, measured on its own over the same history."""
    p = params
    adx_a, pdi, mdi = h1.adx(14)
    adx = np.asarray(adx_a, float)
    if strategy_id == "mean_reversion_v1":
        limit = float(p.get("h1_adx_max", 18))
        return {"h1_adx<=max": {"rate": _rate(adx <= limit), "threshold": limit,
                                "adx_quantiles": _q(adx, (0.1, 0.25, 0.5, 0.75))},
                "all_clauses": {"rate": _rate(adx <= limit)}}
    if strategy_id != "pullback_v1":
        return {}
    ef = np.asarray(h1.ema(int(p.get("h1_ema_fast", 50))), float)
    es = np.asarray(h1.ema(int(p.get("h1_ema_slow", 200))), float)
    slope = np.asarray(h1.ema_slope(int(p.get("h1_ema_fast", 50)), int(p.get("h1_slope_len", 10)),
                                    int(p.get("atr_len", 14))), float)
    c = np.asarray(h1.c, float)
    s_min, a_min = float(p.get("h1_slope_min", 0.02)), float(p.get("h1_adx_min", 15))
    up = {"ema_aligned": ef > es, "close_beyond_ema": c > ef, "slope>=min": slope >= s_min,
          "adx>=min": adx >= a_min, "di_aligned": np.asarray(pdi, float) > np.asarray(mdi, float)}
    dn = {"ema_aligned": ef < es, "close_beyond_ema": c < ef, "slope>=min": slope <= -s_min,
          "adx>=min": adx >= a_min, "di_aligned": np.asarray(mdi, float) > np.asarray(pdi, float)}
    both = np.logical_or(np.logical_and.reduce(list(up.values())), np.logical_and.reduce(list(dn.values())))
    out = {name: {"rate": _rate(np.logical_or(up[name], dn[name]))} for name in up}
    out["slope>=min"]["threshold"] = s_min
    out["slope>=min"]["abs_slope_quantiles"] = _q(np.abs(slope))
    out["adx>=min"]["threshold"] = a_min
    out["adx>=min"]["adx_quantiles"] = _q(adx)
    out["all_clauses"] = {"rate": _rate(both)}
    return out


def regime_mix(h1: FeatureFrame) -> Dict[str, float]:
    """Share of H1 bars in each trend regime, from the live detector. A strategy whose allowed_regimes misses the
    common label is stopped by the ROUTER before its own code runs, which no reason string would ever show."""
    from app.regime.detector import RegimeDetector
    det = RegimeDetector()
    counts: Dict[str, int] = {}
    start = min(len(h1) - 1, det.lookback())
    total = 0
    for k in range(max(1, start), len(h1)):
        label = str(det.raw_trend(h1, k)[0])
        counts[label] = counts.get(label, 0) + 1
        total += 1
    return {k: round(v / max(1, total), 4) for k, v in sorted(counts.items(), key=lambda kv: -kv[1])}


def diagnose(cfgm, m15: FeatureFrame, h1: FeatureFrame, point: float, spread_price: float = 0.0,
             symbol: str = "", strategy_ids: Optional[List[str]] = None) -> dict:
    """Funnel + per-clause rates for every strategy that works off M15/H1 (micro_scalper is M1 and is
    covered by the timeframe study instead)."""
    from app.strategies.registry import build as build_strategy
    out: Dict[str, dict] = {}
    try:
        mix = regime_mix(h1)
    except Exception:
        log.exception("regime mix failed")
        mix = {}
    for cfg in cfgm.strategies.strategies:
        if strategy_ids is not None and cfg.id not in strategy_ids:
            continue
        if cfg.id not in ORDER:
            continue
        try:
            strategy = build_strategy(cfg)
            params = cfg.param_values()
            entry = replay(strategy, m15, h1, point, spread_price, symbol)
            entry["conditions"] = h1_conditions(cfg.id, params, h1)
            top = max(entry["reasons"].items(), key=lambda kv: kv[1]) if entry["reasons"] else None
            entry["biggest_blocker"] = top[0] if top else None
            allowed = list(cfg.allowed_regimes or [])
            entry["regime_mix"] = mix
            entry["allowed_regimes"] = allowed or ["(any)"]
            # the router never even calls a strategy whose allowed_regimes misses the current label
            entry["regime_allowed_share"] = (round(sum(v for k, v in mix.items() if k in allowed), 4)
                                             if allowed and mix else (1.0 if mix else None))
            entry["blocker_pct"] = (f"{100.0 * top[1] / max(1, entry['evaluated']):.0f}% of bars") if top else None
            out[cfg.id] = entry
        except Exception as exc:
            log.exception("funnel diagnosis failed for %s", cfg.id)
            out[cfg.id] = {"error": str(exc)}
    return out
