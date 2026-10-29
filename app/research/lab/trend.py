"""Daily trend following, replayed over the H4 history the lab fetches (research only).

WHY THIS RULE
-------------
Time-series momentum is the best-documented trading edge there is: 58 futures markets (Moskowitz, Ooi & Pedersen
2012), 67 markets back to 1880 (Hurst, Ooi & Pedersen), and 82 markets nobody had tested before ("Trends
Everywhere"). It is slow - weeks to months per trade - so the spread is a rounding error next to the stop, which
is exactly where Quantara's intraday rules lose: their edges, where they exist at all, are about the size of the
spread.

The rule is Andreas Clenow's ("Following the Trend", 2012), with his parameters, fixed IN ADVANCE: nothing here is
searched, so there is no selection to deflate. Entry on a close at a 50-day high with EMA50 > EMA100 (short in
reverse), stop 3 x ATR(100), trailed at the same distance from the best price since entry (a chandelier exit).
No target, no breakeven, no profit lock.

WHAT THIS MODULE MEASURES
-------------------------
Each trade is stepped day by day through `SimulatedTrade` with the exit model the live TrendDaily strategy uses,
so the backtest and the live trail are the same function. Costs: the spread at entry and exit, slippage, and the
account's REAL swap for every night held (SymbolSpec swap_long / swap_short, triple day from the broker). Swap
matters here as nowhere else: Exness charges natural-gas longs about 0.44% a night.

Alongside the result:
  random     the same entries with random sides (does the direction carry information?)
  exits      the same entries with the account's profit lock and breakeven switched on - the reason the live
             strategy switches them off
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from app.alpha.exits import SimulatedTrade, with_execution_exits
from app.core.types import Side
from app.features.frame import FeatureFrame
from app.research.backtester import rollover_nights, triple_weekday
from app.research.lab import stats
from app.research.lab.hypotheses import FAR_TARGET_R, TREND_DAILY_DEFAULTS, daily_bars, trend_daily
from app.research.metrics import trade_metrics


def trend_exit_model(distance: float) -> dict:
    """The exit the live TrendDaily strategy attaches to every trade: a chandelier trail at a FIXED distance set
    at entry, active from the first bar, and nothing that banks profit early."""
    return {"type": "chandelier", "target_r": FAR_TARGET_R,
            "trail": {"activate_r": 0.0, "distance_price": float(distance)},
            "breakeven": {"enabled": False}, "profit_lock": {"enabled": False},
            "time_stop": {"enabled": False}, "thesis_invalidation": False}


def _run(daily, e: int, side: int, dist: float, spread: float, slip: float, model: dict):
    """One trade filled at the open of daily bar e: (SimResult or None if still open, last bar, the sim)."""
    o, h, l, c = daily.o, daily.h, daily.l, daily.c
    n = len(o)
    entry = float(o[e]) + (spread + slip if side > 0 else -slip)
    far = model.get("target_r")
    tp = entry + side * float(far) * dist if far else None
    sim = SimulatedTrade(Side.BUY if side > 0 else Side.SELL, entry, entry - side * dist, tp, model,
                         int(daily.t[e]), slip, spread)
    for k in range(e, n):
        res = sim.step(float(o[k]), float(h[k]), float(l[k]), float(c[k]), spread, daily, k)
        if res is not None:
            return res, k, sim
    return None, n - 1, sim


def _r(res, sim, daily, k: int, spread: float) -> float:
    return res.r_multiple if res is not None else sim.mark_to_market(float(daily.c[k]), spread)


def swap_cost_r(spec, side: int, t0: int, t1: int, dist: float) -> tuple:
    """(nights, swap cost in R) for holding from t0 to t1. Positive carry is not counted as income."""
    if spec is None or dist <= 0:
        return 0, 0.0
    sw = float(spec.swap_long if side > 0 else spec.swap_short)
    nights = rollover_nights(t0, t1, triple_weekday(spec))
    if sw >= 0 or int(getattr(spec, "swap_mode", 1)) not in (0, 1):
        return nights, 0.0
    return nights, -sw * float(spec.point) * nights / dist


def research_trend(h4, spec=None, spread_price: float = 0.0, slip_price: float = 0.0,
                   params: Optional[dict] = None, execution=None, seed: int = 0,
                   rule=trend_daily, defaults: Optional[dict] = None, min_days: Optional[int] = None,
                   exit_model=None, weekdays_only: bool = False) -> dict:
    """A daily rule on one symbol over all of `h4`. Every trade is in R after spread, slippage and swap.

    Defaults to trend_daily. Any rule returning long / short / dist masks on daily bars - trend_ensemble,
    turn_of_month - is replayed the same way: `exit_model(dist)` gives each trade's exit (the trailing trend exit
    unless told otherwise) and `min_days` the history the rule needs. `weekdays_only` drops the Sunday-evening stub
    an index CFD opens with, so a hold counted in bars is counted in trading days."""
    daily = daily_bars(h4)
    if weekdays_only and len(daily):
        keep = np.flatnonzero((np.asarray(daily.t, dtype=np.int64) // 86400 + 3) % 7 < 5)   # 1 Jan 1970 was a Thursday
        daily = FeatureFrame(daily.t[keep], daily.o[keep], daily.h[keep], daily.l[keep], daily.c[keep],
                             daily.spread[keep], daily.tick_volume[keep], "D1")
    p = {**(TREND_DAILY_DEFAULTS if defaults is None else defaults), **(params or {})}
    exit_model = exit_model or trend_exit_model
    out: Dict[str, object] = {"days": len(daily), "params": p,
                              "first_day": int(daily.t[0]) if len(daily) else None,
                              "last_day": int(daily.t[-1]) if len(daily) else None}
    need = min_days if min_days is not None else int(p["ema_slow"]) + int(p["breakout_days"])
    if len(daily) < need:
        out["verdict"] = "INSUFFICIENT_DATA"
        return out
    sig = rule(daily, None, p)
    dist = np.asarray(sig["dist"], float)
    n = len(daily)
    trades: List[dict] = []
    alt = []                                     # (r long, r short, side) per entry, for the random benchmark
    locked = []                                  # the same entries with the profit lock and breakeven on
    lock_model = (with_execution_exits({"type": "chandelier", "target_r": FAR_TARGET_R}, execution)
                  if execution is not None else None)
    busy_until = -1
    for j in np.flatnonzero(sig["long"] | sig["short"]):
        e = int(j) + 1
        if e >= n or e <= busy_until or not np.isfinite(dist[j]) or dist[j] <= 0:
            continue
        side = 1 if sig["long"][j] else -1
        d = float(dist[j])
        t0 = int(daily.t[e])
        res, k, sim = _run(daily, e, side, d, spread_price, slip_price, exit_model(d))
        nights, swap_r = swap_cost_r(spec, side, t0, int(daily.t[k]) + 43200, d)
        r = _r(res, sim, daily, k, spread_price) - swap_r
        trades.append({"entry_time": t0, "exit_time": int(daily.t[k]) + 43200,
                       "direction": "BUY" if side > 0 else "SELL", "r_multiple": r, "swap_r": swap_r,
                       "nights": nights, "bars_held": sim.st.bars_held,
                       "exit_reason": res.exit_reason if res is not None else "OPEN_AT_END", "stop": d})
        busy_until = k
        # the opposite side on the same entry, same distance, same costs
        res2, k2, sim2 = _run(daily, e, -side, d, spread_price, slip_price, exit_model(d))
        r2 = _r(res2, sim2, daily, k2, spread_price) - swap_cost_r(spec, -side, t0, int(daily.t[k2]) + 43200, d)[1]
        alt.append((r, r2, 1.0) if side > 0 else (r2, r, -1.0))
        if lock_model is not None:
            lm = dict(lock_model)
            lm["trail"] = {"activate_r": 0.0, "distance_price": d}
            res3, k3, sim3 = _run(daily, e, side, d, spread_price, slip_price, lm)
            locked.append(_r(res3, sim3, daily, k3, spread_price)
                          - swap_cost_r(spec, side, t0, int(daily.t[k3]) + 43200, d)[1])
    rs = [t["r_multiple"] for t in trades]
    m = trade_metrics(rs)
    out["trades"] = trades
    out["metrics"] = {k: m.get(k) for k in ("n_trades", "expectancy_r", "win_rate", "profit_factor",
                                            "max_drawdown_r", "avg_win_r", "avg_loss_r") if k in m}
    out["avg_nights"] = round(float(np.mean([t["nights"] for t in trades])), 1) if trades else None
    out["avg_swap_r"] = round(float(np.mean([t["swap_r"] for t in trades])), 4) if trades else None
    out["stats"] = rule_stats(rs, alt, seed)
    out["_alt"] = alt                                     # for the pooled benchmark; dropped before storage
    if locked:
        out["exits"] = {"trail_only": stats.mean_ci(rs), "with_profit_lock": stats.mean_ci(locked)}
    if len(trades) >= 4:                                  # out-of-time consistency: the two halves of history
        half = len(trades) // 2
        out["halves"] = [stats.mean_ci(rs[:half]), stats.mean_ci(rs[half:])]
    out["verdict"] = out["stats"].get("grade")
    return out


def rule_stats(rs: List[float], alt: List[tuple], seed: int = 0, min_trades: int = 20) -> dict:
    """Range, Sharpe shape, random-direction benchmark and grade for ONE pre-registered rule.

    Nothing was selected, so the deflation is by a single trial: the deflated Sharpe is the PSR against zero."""
    ci = stats.mean_ci(rs)
    sh = stats.sharpe_stats(rs)
    st = {"n": ci["n"], "ci": ci, "sr": None if sh["sr"] is None else round(sh["sr"], 4),
          "skew": sh["skew"], "kurt": sh["kurt"]}
    if len(alt) >= 2:
        r_long, r_short, sides = zip(*alt)
        st["random"] = stats.random_direction(r_long, r_short, sides, 200, seed)
    d = stats.psr(sh["sr"], ci["n"], sh["skew"] or 0.0, sh["kurt"] or 3.0) if sh["sr"] is not None else None
    st["dsr"] = None if d is None else round(d, 4)
    st["grade"] = stats.grade(ci, d, st.get("random"), min_trades)
    return st


def pooled(per_symbol: Dict[str, dict], seed: int = 0) -> dict:
    """All symbols together - the number the DEMO-or-SHADOW decision is made on. One symbol is a handful of trades
    a year; only the pool has the sample to say anything. The random benchmark is pooled the same way: every
    entry's long and short outcome, whatever symbol it came from."""
    rs, alt = [], []
    for e in per_symbol.values():
        rs.extend(t["r_multiple"] for t in (e.get("trades") or []))
        alt.extend(e.get("_alt") or [])
    out = {"symbols": len([e for e in per_symbol.values() if e.get("trades")]), "n_trades": len(rs)}
    out.update(rule_stats(rs, alt, seed))
    return out
