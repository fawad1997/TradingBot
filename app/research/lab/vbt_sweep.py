"""VectorBT: fast sweep of stop-loss / reward:risk over the bot's real entries, with train / test split."""
from __future__ import annotations

import warnings
from typing import Dict, List

import numpy as np
import pandas as pd

from app.features.frame import FeatureFrame
from app.research.metrics import trade_metrics


def _series(values, index):
    return pd.Series(np.asarray(values), index=index)


def run_sweep(m1: FeatureFrame, sig: dict, point: float, sl_grid, rr_grid, train_fraction: float, min_trades: int,
              spread_price=None, commission_price: float = 0.0, min_stop_spread_multiple: float = 0.0) -> dict:
    """`spread_price`: spread in price units per bar (from ticks when available); default = the bar spread field.
    `commission_price`: round-trip commission per unit, in price units. Half of (spread + commission) is charged on
    each side, so a round trip pays the full spread plus the full commission.
    `min_stop_spread_multiple`: the live strategy never places a stop closer than this many spreads; the replay
    applies the same floor (stop = max(sl_atr x ATR, multiple x spread at entry)), or it would charge trades the live
    bot never takes - a quiet-market M1 ATR can be smaller than the spread itself."""
    import vectorbt as vbt
    warnings.filterwarnings("ignore")
    n = len(m1)
    idx = pd.to_datetime(m1.t, unit="s", utc=True)
    open_, high, low, close = (_series(a, idx) for a in (m1.o, m1.h, m1.l, m1.c))
    # entries happen at the NEXT bar's open (the bot acts after the M1 bar closes)
    longs = _series(np.r_[False, sig["long"][:-1]], idx)
    shorts = _series(np.r_[False, sig["short"][:-1]], idx)
    atr = _series(np.r_[np.nan, sig["atr"][:-1]], idx)
    sp = (np.asarray(m1.spread, float) * point) if spread_price is None else np.asarray(spread_price, float)
    sp = np.where(np.isfinite(sp), sp, np.asarray(m1.spread, float) * point)
    half_spread = _series(((sp + commission_price) / 2.0) / np.where(m1.o > 0, m1.o, np.nan), idx).fillna(0.0)
    combos = [(float(s), float(r)) for s in sl_grid for r in rr_grid]
    cols = pd.MultiIndex.from_tuples(combos, names=["sl_atr", "rr"])

    def broadcast(s):
        return pd.DataFrame({k: s for k in combos}).set_axis(cols, axis=1)

    floor = _series(float(min_stop_spread_multiple) * sp, idx)        # spread at the ENTRY bar's open

    def stop_dist(k):
        return np.maximum(k[0] * atr, floor) if min_stop_spread_multiple > 0 else k[0] * atr
    sl_frac = pd.DataFrame({k: (stop_dist(k) / open_) for k in combos}).set_axis(cols, axis=1)
    tp_frac = pd.DataFrame({k: (k[1] * stop_dist(k) / open_) for k in combos}).set_axis(cols, axis=1)
    pf = vbt.Portfolio.from_signals(
        broadcast(close), entries=broadcast(longs), exits=False, short_entries=broadcast(shorts), short_exits=False,
        price=broadcast(open_), open=broadcast(open_), high=broadcast(high), low=broadcast(low),
        sl_stop=sl_frac, tp_stop=tp_frac, fees=broadcast(half_spread), freq="1min", accumulate=False,
        upon_opposite_entry="Ignore")
    rec = pf.trades.records_readable
    split_ts = idx[int(n * train_fraction)]
    rows: List[dict] = []
    best = None
    if len(rec):
        pos_of = {ts: i for i, ts in enumerate(idx)}
        col_pos = {k: c for c, k in enumerate(combos)}
        sl_vals = sl_frac.values
        ent = rec["Entry Timestamp"]
        entry_i = ent.map(pos_of).astype(int).values
        col_i = np.array([col_pos[tuple(c)] for c in rec["Column"]])
        slf = sl_vals[entry_i, col_i]
        with np.errstate(divide="ignore", invalid="ignore"):
            r_mult = (rec["Return"].values / slf)
        rec = rec.assign(R=r_mult, is_train=(ent < split_ts).values)
        rec = rec[np.isfinite(rec["R"])]
    for k in combos:
        sub = rec[rec["Column"].map(lambda c: tuple(c) == k)] if len(rec) else rec
        tr = sub[sub["is_train"]]["R"].tolist() if len(sub) else []
        te = sub[~sub["is_train"]]["R"].tolist() if len(sub) else []
        row = {"sl_atr": k[0], "rr": k[1], "train": trade_metrics(tr), "test": trade_metrics(te)}
        rows.append(row)
        tn = row["train"].get("n_trades", 0)
        if tn >= min_trades and (best is None or row["train"]["expectancy_r"] > best["train"]["expectancy_r"]):
            best = row
    verdict = "NO_QUALIFYING_PARAMETERS"
    if best is not None:
        t = best["test"]
        ok = (t.get("n_trades", 0) >= max(10, min_trades // 2) and (t.get("expectancy_r") or 0) > 0
              and (t.get("profit_factor") or 0) > 1.0)
        verdict = "ROBUST_ON_TEST" if ok else "NOT_ROBUST_ON_TEST"
    return {"combos": rows, "best": best, "verdict": verdict, "split_time": str(split_ts), "signals": int(longs.sum() + shorts.sum())}
