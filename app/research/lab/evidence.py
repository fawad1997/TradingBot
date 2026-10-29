"""Evidence for one lab result: the numbers `stats.py` needs, produced from the lab's own simulators.

Two shapes of result get a `stats` block:

  cell_stats      a rule on a symbol at a timeframe, scored by the stop/target grid (the hypotheses and the
                  micro_scalper timeframes). The grid's best setting is judged on its UNSEEN window.
  strategy_stats  a configured strategy replayed at its live parameters by the generic backtester (per-strategy
                  research). Its unseen trades are judged as they were traded.

Both add the same three things: the 95% range of R per trade, the per-trade Sharpe with its shape (for the
deflated Sharpe, computed once every cell of the cycle is known - `stats.deflate_cells`), and the random-direction
benchmark. Both also re-score the same entries under a second exit, so the report can show what breakeven and the
profit lock add or cost on identical trades:

  bracket     stop and target only
  as_traded   + breakeven and the profit lock, exactly as `with_execution_exits` adds them live
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from app.research.lab import stats
from app.research.lab.fastsim import bracket_signals, simulate, trade_outcomes

#: Random-side runs per benchmark. 200 resolves p = 0.05 to about +-0.015, which is all a grade needs.
RANDOM_RUNS = 200
#: Time blocks for the overfitting test. 8 gives C(8,4) = 70 splits.
PBO_BLOCKS = 8


def _summary(rs) -> dict:
    ci = stats.mean_ci(rs)
    return {"n": ci["n"], "mean": ci["mean"], "lo": ci["lo"], "hi": ci["hi"]}


def _base(rs) -> dict:
    ci = stats.mean_ci(rs)
    sh = stats.sharpe_stats(rs)
    return {"n": ci["n"], "ci": ci, "sr": None if sh["sr"] is None else round(sh["sr"], 4),
            "skew": None if sh["skew"] is None else round(sh["skew"], 4),
            "kurt": None if sh["kurt"] is None else round(sh["kurt"], 4)}


def cell_stats(frame, longs, shorts, atr, sl_grid: Sequence[float], rr_grid: Sequence[float],
               train_fraction: float, best: Optional[dict], spread_price=None, commission_price: float = 0.0,
               min_stop_spread_multiple: float = 0.0, traded_model: Optional[dict] = None,
               seed: int = 0) -> Optional[dict]:
    """Statistics for one grid-scored cell. `best` is the grid's pick ({"sl_atr", "rr"}); None means the grid
    found nothing to judge and there is nothing to add."""
    if not best or best.get("sl_atr") is None:
        return None
    o, h, l, c = frame.o, frame.h, frame.l, frame.c
    n = len(o)
    split = int(n * train_fraction)
    kw = dict(spread_price=spread_price, commission_price=commission_price,
              min_stop_spread_multiple=min_stop_spread_multiple)
    # 1. overfitting: every setting over the whole history, bucketed by time
    per_combo = []
    for s in sl_grid:
        for r in rr_grid:
            rs, ent = simulate(o, h, l, c, longs, shorts, atr, s, r, start=0, end=n, return_entries=True, **kw)
            per_combo.append(([j for j, _ in ent], rs))
    sums, counts = stats.block_matrix(per_combo, n, PBO_BLOCKS)
    pbo = stats.pbo_cscv(sums, counts)
    # 2. the pick, on the unseen window
    sl, rr = float(best["sl_atr"]), float(best["rr"])
    rs, ent = simulate(o, h, l, c, longs, shorts, atr, sl, rr, start=split, end=n, return_entries=True, **kw)
    out = _base(rs)
    out["pbo"] = pbo
    out["setting"] = {"sl_atr": sl, "rr": rr}
    if len(ent) >= 2:
        sig = bracket_signals(ent, atr, spread_price, sl, rr, min_stop_spread_multiple)
        r_long = trade_outcomes(o, h, l, c, [(j, 1, risk, q) for j, _, risk, q in sig], spread_price,
                                commission_price)
        r_short = trade_outcomes(o, h, l, c, [(j, -1, risk, q) for j, _, risk, q in sig], spread_price,
                                 commission_price)
        sides = np.asarray([side for _, side in ent], float)
        out["random"] = stats.random_direction(r_long, r_short, sides, RANDOM_RUNS, seed)
        bracket = np.where(sides > 0, r_long, r_short)
        out["exits"] = {"bracket": _summary(bracket)}
        if traded_model is not None:
            out["exits"]["as_traded"] = _summary(trade_outcomes(o, h, l, c, sig, spread_price, commission_price,
                                                                traded_model))
    return out


def strategy_stats(trades: Sequence[dict], m15, split_time: int, spread_price=None,
                   commission_price: float = 0.0, seed: int = 0) -> Optional[dict]:
    """Statistics for one strategy on one symbol from the generic backtester's trades. The unseen window starts
    at `split_time`. The backtester already traded them with the live exit, so `as_traded` is its own R and
    `bracket` re-scores the same entries with only their stop and target."""
    test = [t for t in trades if t.get("r_multiple") is not None and (t.get("entry_time") or 0) >= split_time]
    rs = [float(t["r_multiple"]) for t in test]
    out = _base(rs)
    if len(test) < 2:
        return out
    tt = np.asarray(m15.t, np.int64)
    sig = []
    for t in test:
        fill = int(np.searchsorted(tt, int(t["entry_time"]), side="left"))
        stop = float(t.get("stop") or 0.0)
        tp = t.get("tp")
        entry = float(t.get("entry") or 0.0)
        rr = abs(float(tp) - entry) / stop if tp is not None and stop > 0 else float("inf")
        sig.append((fill - 1, 1 if str(t.get("direction")) in ("BUY", "Side.BUY") else -1, stop, rr))
    o, h, l, c = m15.o, m15.h, m15.l, m15.c
    r_long = trade_outcomes(o, h, l, c, [(j, 1, risk, q) for j, _, risk, q in sig], spread_price, commission_price)
    r_short = trade_outcomes(o, h, l, c, [(j, -1, risk, q) for j, _, risk, q in sig], spread_price,
                             commission_price)
    sides = np.asarray([side for _, side, _, _ in sig], float)
    out["random"] = stats.random_direction(r_long, r_short, sides, RANDOM_RUNS, seed)
    out["exits"] = {"as_traded": _summary(rs), "bracket": _summary(np.where(sides > 0, r_long, r_short))}
    return out
