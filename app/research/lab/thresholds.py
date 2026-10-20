"""What a threshold costs: setups per week AND edge, measured together.

"Make it trade more" and "make it trade well" pull against each other, and a threshold moved by feel usually buys
trades with expectancy. So each gating threshold is swept one at a time over real history, and for every value two
numbers are reported side by side:

    setups per week   does the rule fire often enough to be worth running
    unseen R          does what it fires on still make money on the last 30% of the window

The entries are the strategy's own: the real `evaluate()` is replayed, and each signal is simulated with the stop
and target that signal asked for. A value is only worth adopting when it raises the first number without pushing
the second below zero; the research gate and walk-forward still apply afterwards.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np

from app.features.frame import FeatureFrame
from app.research.lab.fastsim import simulate_signals
from app.research.lab.funnel import _h1_index
from app.research.metrics import trade_metrics
from app.strategies.base import StrategyView

log = logging.getLogger("research_lab")

# one value list per threshold; everything else stays at its configured value
SWEEPS: Dict[str, Dict[str, List[float]]] = {
    "pullback_v1": {
        "h1_adx_min": [0, 5, 10, 15, 20, 25],
        "h1_slope_min": [0.0, 0.005, 0.01, 0.02, 0.04],
        "pullback_max_depth": [0.5, 0.618, 0.786, 0.9],
        "pullback_max_bars": [4, 8, 12, 20],
    },
    "mean_reversion_v1": {
        "h1_adx_max": [14, 18, 22, 26, 30, 100],
        "rsi_extreme": [20, 25, 30, 35, 40],
        "min_reward_r": [0.5, 0.75, 1.0, 1.5],
    },
    "breakout_retest_v1": {"lookback": [20, 40, 80], "wait": [6, 12, 24], "tol_atr": [0.15, 0.25, 0.5]},
    "momentum_trend_v1": {"min_mom_atr": [0.25, 0.5, 1.0, 2.0], "slow": [30, 50, 100]},
    "session_breakout_v1": {"min_range_atr": [0.0, 0.5, 1.0], "buffer_atr": [0.0, 0.1, 0.25]},
    "vwap_reversion_v1": {"min_dev_atr": [1.0, 1.5, 2.0, 3.0]},
    "htf_continuation_v1": {"min_slope_atr": [0.0, 0.1, 0.2, 0.5]},
}


def collect(strategy, m15: FeatureFrame, h1: FeatureFrame, point: float, spread_price: float,
            symbol: str = "") -> List[tuple]:
    """Every signal the strategy's own evaluate() produces: (bar, side, its stop distance, its reward:risk)."""
    idx = _h1_index(m15, h1)
    out: List[tuple] = []
    seen = set()
    for i in range(len(m15)):
        k = int(idx[i])
        if k < 1:
            continue
        mid = float(m15.c[i])
        view = StrategyView(m15=m15, i=i, h1=h1, k=k, symbol=symbol, point=point, spread_price=spread_price,
                            bid=mid - spread_price / 2.0, ask=mid + spread_price / 2.0, spread_safe=True,
                            session="LONDON", regime_trend="UNKNOWN")
        try:
            res = strategy.evaluate(view)
        except Exception:
            continue
        p = res.plan
        if p is None or p.setup_key in seen:
            continue
        seen.add(p.setup_key)
        stop = abs(p.entry - p.sl)
        if stop <= 0:
            continue
        rr = abs(p.tp - p.entry) / stop if p.tp else 2.0
        out.append((i, 1 if str(p.direction) == "BUY" else -1, stop, rr))
    return out


def _score(m15: FeatureFrame, signals: List[tuple], spread: float, commission: float, train_fraction: float) -> dict:
    n = len(m15)
    split = int(n * train_fraction)
    sp = np.full(n, spread)
    tr = simulate_signals(m15.o, m15.h, m15.l, m15.c, signals, sp, commission, 0, split)
    te = simulate_signals(m15.o, m15.h, m15.l, m15.c, signals, sp, commission, split, n)
    weeks = n * 900 / 604800.0
    return {"setups": len(signals), "setups_per_week": round(len(signals) / max(1e-9, weeks), 2),
            "train": {k: trade_metrics(tr).get(k) for k in ("n_trades", "expectancy_r", "profit_factor")},
            "test": {k: trade_metrics(te).get(k) for k in ("n_trades", "expectancy_r", "profit_factor")}}


def sweep(cfgm, m15: FeatureFrame, h1: FeatureFrame, point: float, spread: float = 0.0, commission: float = 0.0,
          symbol: str = "", train_fraction: float = 0.7, strategy_ids: Optional[List[str]] = None) -> dict:
    """{strategy: {param: {"configured": v, "values": [row per value]}}}. Never changes live config."""
    from app.strategies.registry import build
    out: Dict[str, dict] = {}
    for cfg in cfgm.strategies.strategies:
        grid = SWEEPS.get(cfg.id)
        if not grid or (strategy_ids is not None and cfg.id not in strategy_ids):
            continue
        params = cfg.param_values()
        per_param: Dict[str, dict] = {}
        for name, values in grid.items():
            if name not in params:
                continue
            rows = []
            for value in values:
                try:
                    strategy = build(cfg, param_overrides={name: value})
                    signals = collect(strategy, m15, h1, point, spread, symbol)
                    rows.append({"value": value, **_score(m15, signals, spread, commission, train_fraction),
                                 "configured": value == params[name]})
                except Exception as exc:
                    log.exception("threshold sweep failed for %s.%s=%s", cfg.id, name, value)
                    rows.append({"value": value, "error": str(exc)})
            per_param[name] = {"configured": params[name], "values": rows}
        if per_param:
            out[cfg.id] = per_param
    return out
