"""Walk-forward analysis with a small parameter family (section 29).

1. Every combination of the strategy's `research_grid` (kept small on purpose)
   is backtested ONCE over the full history (worker processes - CPU-heavy work
   never runs in the trading loop).
2. For each rolling window: pick the combination with the best TRAIN expectancy
   (minimum trades required), then take that combination's trades in the
   following TEST window only. Stitched TEST trades = out-of-sample record.
3. Nearby-value stability: neighbours of the chosen combination (one grid step
   away in one parameter) must also be profitable in TRAIN - a lone peak is suspicious.
4. Deflated Sharpe uses the number of combinations tried.
"""
from __future__ import annotations

import itertools
import math
import statistics
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from app.config import StrategyCfg
from app.core.types import SymbolSpec
from app.features.frame import FeatureFrame
from app.research.backtester import BTSettings, regime_series, run_backtest
from app.research.metrics import deflated_sharpe, trade_metrics


def grid_combinations(grid: Dict[str, list], limit: int = 64) -> List[Dict]:
    if not grid:
        return [{}]
    keys = sorted(grid)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*[grid[k] for k in keys])]
    if len(combos) > limit:
        raise ValueError(f"research grid has {len(combos)} combinations (> {limit}). "
                         f"Test fewer parameters at once (section 29).")
    return combos


def neighbours(combo: Dict, grid: Dict[str, list]) -> List[Dict]:
    out = []
    for k, vals in grid.items():
        idx = vals.index(combo[k])
        for d in (-1, 1):
            j = idx + d
            if 0 <= j < len(vals):
                n = dict(combo)
                n[k] = vals[j]
                out.append(n)
    return out


def _key(c: Dict) -> Tuple:
    return tuple(sorted(c.items()))


def _run_one(args):
    scfg_json, m15_arrays, h1_arrays, settings, spec, combo, regimes = args
    scfg = StrategyCfg.model_validate_json(scfg_json)
    m15 = FeatureFrame(*m15_arrays, timeframe="M15")
    h1 = FeatureFrame(*h1_arrays, timeframe="H1")
    res = run_backtest(scfg, m15, h1, settings, spec, param_overrides=combo, regimes=regimes)
    return _key(combo), res.trades


def _arrays(ff: FeatureFrame):
    return (ff.t, ff.o, ff.h, ff.l, ff.c, ff.spread, ff.tick_volume)


def month_add(ts: int, months: int) -> int:
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    y, m = d.year + (d.month - 1 + months) // 12, (d.month - 1 + months) % 12 + 1
    return int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())


@dataclass
class WFAResult:
    windows: List[Dict]
    oos_trades: List[Dict]
    oos_metrics: Dict
    combos_tested: int
    dsr: Optional[float]
    stability: Dict
    per_combo: Dict


def walk_forward(scfg: StrategyCfg, m15: FeatureFrame, h1: FeatureFrame, settings: BTSettings,
                 spec: Optional[SymbolSpec] = None, train_months: int = 12, test_months: int = 3,
                 min_train_trades: int = 30, workers: int = 1) -> WFAResult:
    combos = grid_combinations(scfg.research_grid)
    regimes = regime_series(h1)
    jobs = [(scfg.model_dump_json(), _arrays(m15), _arrays(h1), settings, spec, c, regimes) for c in combos]
    results: Dict[Tuple, List[dict]] = {}
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for key, trades in ex.map(_run_one, jobs):
                results[key] = trades
    else:
        for j in jobs:
            key, trades = _run_one(j)
            results[key] = trades

    def exp_in(trades, a, b):
        rs = [t["r_multiple"] for t in trades if a <= t["signal_time"] < b]
        return (statistics.fmean(rs) if rs else None), len(rs)

    t0, t_end = int(m15.t[0]), int(m15.t[-1])
    first = month_add(t0, 1)
    windows, oos = [], []
    stab_scores = []
    start = first
    while True:
        train_a, train_b = start, month_add(start, train_months)
        test_a, test_b = train_b, month_add(train_b, test_months)
        if test_a >= t_end:
            break
        best, best_exp = None, -math.inf
        for c in combos:
            e, n = exp_in(results[_key(c)], train_a, train_b)
            if e is not None and n >= min_train_trades and e > best_exp:
                best, best_exp = c, e
        w = {"train": [train_a, train_b], "test": [test_a, min(test_b, t_end)], "chosen": best,
             "train_expectancy_r": None if best is None else round(best_exp, 4)}
        if best is not None:
            nb = neighbours(best, scfg.research_grid)
            nb_exp = [exp_in(results[_key(n)], train_a, train_b)[0] for n in nb]
            nb_exp = [x for x in nb_exp if x is not None]
            pos = sum(1 for x in nb_exp if x > 0) / len(nb_exp) if nb_exp else None
            w["neighbour_positive_fraction"] = pos
            if pos is not None:
                stab_scores.append(pos)
            test_trades = [dict(t, wfa_window=len(windows)) for t in results[_key(best)]
                           if test_a <= t["signal_time"] < test_b]
            w["test_trades"] = len(test_trades)
            e, _ = exp_in(test_trades, test_a, test_b)
            w["test_expectancy_r"] = None if e is None else round(e, 4)
            oos.extend(test_trades)
        windows.append(w)
        start = month_add(start, test_months)
    rs = [t["r_multiple"] for t in oos]
    m = trade_metrics(rs, [t["mfe_r"] for t in oos], [t["mae_r"] for t in oos], [t["giveback_r"] for t in oos],
                      [t["bars_held"] for t in oos])
    trial_srs = []
    per_combo = {}
    for c in combos:
        r_c = [t["r_multiple"] for t in results[_key(c)]]
        mc = trade_metrics(r_c)
        per_combo[str(c)] = {"n": mc.get("n_trades"), "expectancy_r": mc.get("expectancy_r"),
                             "pf": mc.get("profit_factor")}
        if mc.get("sharpe_per_trade") is not None:
            trial_srs.append(mc["sharpe_per_trade"])
    dsr = deflated_sharpe(rs, trial_srs) if len(rs) >= 10 else None
    stability = {"mean_neighbour_positive_fraction": round(statistics.fmean(stab_scores), 3) if stab_scores else None,
                 "ok": bool(stab_scores) and statistics.fmean(stab_scores) >= 0.6}
    return WFAResult(windows, oos, m, len(combos), dsr, stability, per_combo)
