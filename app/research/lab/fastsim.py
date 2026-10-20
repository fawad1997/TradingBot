"""A small, fast bracket-order simulator used for the timeframe comparison and the Optuna search.

Same rules as the VectorBT sweep: enter at the NEXT bar's open after a signal, one position at a time, stop and target
as ATR multiples, the spread (and commission) paid once per round trip and expressed in R. If the stop and the target
are both inside the same bar the stop is assumed to fill first (the pessimistic reading of a bar we cannot see inside).
Positions still open at the end of the data are closed at the last close.
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np

from app.research.metrics import trade_metrics


def simulate(o, h, l, c, longs, shorts, atr, sl_atr: float, rr: float, spread_price=None,
             commission_price: float = 0.0, start: int = 0, end: Optional[int] = None,
             max_hold: int = 2000, min_stop_spread_multiple: float = 0.0) -> List[float]:
    """R multiple of every trade whose SIGNAL bar lies in [start, end).
    The stop is floored at `min_stop_spread_multiple` x the spread at entry, exactly as the live strategy does."""
    n = len(o)
    end = n if end is None else min(end, n)
    sp = np.zeros(n) if spread_price is None else np.nan_to_num(np.asarray(spread_price, float), nan=0.0)
    sig = np.flatnonzero((longs | shorts)[start:end]) + start
    out: List[float] = []
    busy_until = -1
    for j in sig:
        e = j + 1                                   # filled at the next bar's open
        if e >= n or e <= busy_until:
            continue
        a = atr[j]
        if not np.isfinite(a) or a <= 0:
            continue
        side = 1.0 if longs[j] else -1.0
        entry = o[e]
        risk = max(sl_atr * a, min_stop_spread_multiple * sp[e])
        stop = entry - side * risk
        target = entry + side * risk * rr
        stop_end = min(n, e + max_hold)
        hi, lo = h[e:stop_end], l[e:stop_end]
        if side > 0:
            hit_sl, hit_tp = lo <= stop, hi >= target
        else:
            hit_sl, hit_tp = hi >= stop, lo <= target
        i_sl = int(np.argmax(hit_sl)) if hit_sl.any() else None
        i_tp = int(np.argmax(hit_tp)) if hit_tp.any() else None
        if i_sl is None and i_tp is None:
            k = stop_end - 1
            raw = side * (c[k] - entry) / risk
        elif i_tp is None or (i_sl is not None and i_sl <= i_tp):
            k = e + i_sl
            raw = -1.0
        else:
            k = e + i_tp
            raw = rr
        busy_until = k
        out.append(raw - (sp[e] + commission_price) / risk)
    return out


def grid_train_test(o, h, l, c, longs, shorts, atr, sl_grid, rr_grid, train_fraction: float, min_trades: int,
                    spread_price=None, commission_price: float = 0.0, min_stop_spread_multiple: float = 0.0) -> dict:
    """Same shape as the VectorBT sweep result: best on train, scored on the unseen rest."""
    n = len(o)
    split = int(n * train_fraction)
    best, rows = None, []
    for s in sl_grid:
        for r in rr_grid:
            tr = simulate(o, h, l, c, longs, shorts, atr, s, r, spread_price, commission_price, 0, split,
                          min_stop_spread_multiple=min_stop_spread_multiple)
            te = simulate(o, h, l, c, longs, shorts, atr, s, r, spread_price, commission_price, split, n,
                          min_stop_spread_multiple=min_stop_spread_multiple)
            row = {"sl_atr": float(s), "rr": float(r), "train": trade_metrics(tr), "test": trade_metrics(te)}
            rows.append(row)
            if row["train"].get("n_trades", 0) >= min_trades and (
                    best is None or row["train"]["expectancy_r"] > best["train"]["expectancy_r"]):
                best = row
    verdict = "NO_QUALIFYING_PARAMETERS"
    if best is not None:
        t = best["test"]
        ok = (t.get("n_trades", 0) >= max(10, min_trades // 2) and (t.get("expectancy_r") or 0) > 0
              and (t.get("profit_factor") or 0) > 1.0)
        verdict = "ROBUST_ON_TEST" if ok else "NOT_ROBUST_ON_TEST"
    return {"combos": rows, "best": best, "verdict": verdict, "signals": int(longs.sum() + shorts.sum())}


def simulate_signals(o, h, l, c, signals, spread_price=None, commission_price: float = 0.0,
                     start: int = 0, end: Optional[int] = None, max_hold: int = 2000) -> List[float]:
    """Like `simulate`, but each entry brings its OWN stop distance and reward:risk - the ones the strategy
    actually asked for. `signals` is a list of (bar index, +1 long / -1 short, stop distance, reward:risk)."""
    n = len(o)
    end = n if end is None else min(end, n)
    sp = np.zeros(n) if spread_price is None else np.nan_to_num(np.asarray(spread_price, float), nan=0.0)
    out: List[float] = []
    busy_until = -1
    for j, side, risk, rr in signals:
        if not (start <= j < end):
            continue
        e = j + 1
        if e >= n or e <= busy_until or not np.isfinite(risk) or risk <= 0:
            continue
        entry = o[e]
        stop = entry - side * risk
        target = entry + side * risk * rr
        stop_end = min(n, e + max_hold)
        hi, lo = h[e:stop_end], l[e:stop_end]
        hit_sl, hit_tp = ((lo <= stop, hi >= target) if side > 0 else (hi >= stop, lo <= target))
        i_sl = int(np.argmax(hit_sl)) if hit_sl.any() else None
        i_tp = int(np.argmax(hit_tp)) if hit_tp.any() else None
        if i_sl is None and i_tp is None:
            k = stop_end - 1
            raw = side * (c[k] - entry) / risk
        elif i_tp is None or (i_sl is not None and i_sl <= i_tp):
            k, raw = e + i_sl, -1.0
        else:
            k, raw = e + i_tp, rr
        busy_until = k
        out.append(raw - (sp[e] + commission_price) / risk)
    return out
