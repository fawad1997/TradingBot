"""Optuna search with walk-forward validation.

The grid sweep tunes stop and reward:risk on one 70/30 split. Here Optuna also tunes the entry rule (the relative
tick-volume threshold) over a continuous range, and the score that matters is OUT OF SAMPLE: the data is cut into
consecutive folds, parameters are tuned on everything before a fold and then traded, untouched, on that fold. Only the
stitched-together fold results count. A positive walk-forward number means the tuning procedure itself works on data it
has not seen, not merely that some parameter set fitted the past.
"""
from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional

import numpy as np

from app.research.lab.fastsim import simulate
from app.research.lab.signals import apply_volume
from app.research.metrics import trade_metrics

log = logging.getLogger("research_lab")


def _score(rs: List[float], min_trades: int) -> float:
    if len(rs) < min_trades:
        return -10.0 + len(rs) / max(1, min_trades)          # steer away from parameter sets that barely trade
    mean = float(np.mean(rs))
    sd = float(np.std(rs)) or 1.0
    return mean - 0.5 * sd / math.sqrt(len(rs))              # mean R minus half a standard error: favours stability


STOP_FLOOR = {"multiple": 0.0}      # set per run by walk_forward (live min_stop_spread_multiple)


def _run(frame, cands, p, spread, commission, start, end):
    s = apply_volume(cands, p["min_rv"])
    return simulate(frame.o, frame.h, frame.l, frame.c, s["long"], s["short"], s["atr"], p["sl_atr"], p["rr"],
                    spread, commission, start, end, min_stop_spread_multiple=STOP_FLOOR["multiple"])


def _tune(frame, cands, spread, commission, start, end, n_trials, min_trades, seed) -> Dict:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    def objective(trial):
        p = {"sl_atr": trial.suggest_float("sl_atr", 0.75, 4.0, step=0.25),
             "rr": trial.suggest_float("rr", 0.75, 4.0, step=0.25),
             "min_rv": trial.suggest_float("min_rv", 0.6, 2.5, step=0.1)}
        return _score(_run(frame, cands, p, spread, commission, start, end), min_trades)

    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=seed))
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    return dict(study.best_params)


# Lopez de Prado ch.7: drop training bars adjacent to the test window so overlapping outcomes cannot leak.
PURGE_FRACTION = 0.01


def _tune_random(frame, cands, spread, commission, start, end, n_trials, min_trades, seed) -> Dict:
    """The same search, same budget, drawn uniformly at random instead of by TPE.

    This is the control arm. Peng Liu ch.8 runs exactly this alongside the clever optimisers and plots
    them together, and the comparison is the point: a tuner that cannot beat random draws from its own
    parameter space is not optimising, it is fitting noise, and every parameter it has produced is a
    coincidence with a confidence interval around it.
    """
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    def objective(trial):
        p = {"sl_atr": trial.suggest_float("sl_atr", 0.75, 4.0, step=0.25),
             "rr": trial.suggest_float("rr", 0.75, 4.0, step=0.25),
             "min_rv": trial.suggest_float("min_rv", 0.6, 2.5, step=0.1)}
        return _score(_run(frame, cands, p, spread, commission, start, end), min_trades)

    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.RandomSampler(seed=seed))
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    return dict(study.best_params)


def _random_arm(frame, cands, spread, commission, train_start, train_end, test_start, test_end,
                n_trials, min_trades, seed) -> List[float]:
    """Tune randomly on the same training window, then trade the same test window. Never raises."""
    try:
        p = _tune_random(frame, cands, spread, commission, train_start, train_end, n_trials, min_trades, seed)
        return _run(frame, cands, p, spread, commission, test_start, test_end)
    except Exception as exc:
        log.warning("random baseline arm failed: %s", exc)
        return []


def walk_forward(frame, cands: dict, spread=None, commission: float = 0.0, folds: int = 4, n_trials: int = 40,
                 min_trades: int = 30, seed: int = 7, min_stop_spread_multiple: float = 0.0) -> Dict:
    """Expanding-window walk-forward. Fold k (k >= 1) is traded with parameters tuned on folds 0..k-1."""
    STOP_FLOOR["multiple"] = float(min_stop_spread_multiple)
    n = len(frame)
    edges = [int(n * i / (folds + 1)) for i in range(folds + 2)]      # first chunk is training-only
    # Purge gap between train and test (Lopez de Prado ch.7). A trade opened just before the boundary
    # can still be open past it, so its outcome depends on test-period bars that shaped the tuning.
    # 1% of the series is the author's rule of thumb and is ample for holding periods this short.
    purge = max(1, int(n * PURGE_FRACTION))
    oos: List[float] = []
    per_fold = []
    for k in range(1, folds + 1):
        train_end, test_end = edges[k], edges[k + 1]
        train_end_purged = max(1, train_end - purge)
        try:
            p = _tune(frame, cands, spread, commission, 0, train_end_purged, n_trials,
                      max(10, min_trades // 2), seed + k)
        except Exception as exc:
            log.warning("optuna fold %d failed: %s", k, exc)
            continue
        rs = _run(frame, cands, p, spread, commission, train_end, test_end)
        oos.extend(rs)
        # Control arm: the same budget spent on random draws. If the tuner cannot beat this, the
        # "optimisation" is fitting noise and every parameter it produced is a coincidence.
        rand_rs = _random_arm(frame, cands, spread, commission, 0, train_end_purged, train_end, test_end,
                              n_trials, max(10, min_trades // 2), seed + k)
        per_fold.append({"fold": k, "params": p, "n_trades": len(rs),
                         "expectancy_r": round(float(np.mean(rs)), 4) if rs else None,
                         "random_expectancy_r": round(float(np.mean(rand_rs)), 4) if rand_rs else None,
                         "purged_bars": purge})
    final: Optional[Dict] = None
    try:
        final = _tune(frame, cands, spread, commission, 0, n, n_trials, min_trades, seed)
    except Exception as exc:
        log.warning("optuna final tuning failed: %s", exc)
    m = trade_metrics(oos)
    ok = (m.get("n_trades", 0) >= min_trades and (m.get("expectancy_r") or 0) > 0
          and (m.get("profit_factor") or 0) > 1.0)
    # Did the tuner actually beat random? Reported beside the verdict so it cannot be overlooked.
    tuned_e = [f["expectancy_r"] for f in per_fold if f.get("expectancy_r") is not None]
    rand_e = [f["random_expectancy_r"] for f in per_fold if f.get("random_expectancy_r") is not None]
    baseline = None
    if tuned_e and rand_e:
        t, r = float(np.mean(tuned_e)), float(np.mean(rand_e))
        baseline = {"tuned_expectancy_r": round(t, 4), "random_expectancy_r": round(r, 4),
                    "edge_over_random_r": round(t - r, 4), "tuner_beat_random": bool(t > r)}
    return {"verdict": "WALK_FORWARD_POSITIVE" if ok else "WALK_FORWARD_NEGATIVE",
            "walk_forward": {k: m.get(k) for k in ("n_trades", "expectancy_r", "win_rate", "profit_factor",
                                                    "max_drawdown_r")},
            "folds": per_fold, "params_on_all_data": final, "trials_per_fold": n_trials,
            "baseline": baseline, "purge_fraction": PURGE_FRACTION}


# ------------------------------------------------------------------ fixed signals (research hypotheses)
def walk_forward_fixed(frame, sig: dict, spread=None, commission: float = 0.0, folds: int = 4,
                       n_trials: int = 30, min_trades: int = 30, seed: int = 7,
                       min_stop_spread_multiple: float = 0.0) -> Dict:
    """Walk-forward for a rule whose ENTRIES are fixed: only the stop and the reward:risk are tuned, on past data
    only, then traded untouched on the next slice. Used for the candidate rules in hypotheses.py."""
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    n = len(frame)
    edges = [int(n * i / (folds + 1)) for i in range(folds + 2)]
    run = lambda p, a, b: simulate(frame.o, frame.h, frame.l, frame.c, sig["long"], sig["short"], sig["atr"],
                                   p["sl_atr"], p["rr"], spread, commission, a, b,
                                   min_stop_spread_multiple=min_stop_spread_multiple)

    def tune(a, b, need, s):
        def objective(trial):
            p = {"sl_atr": trial.suggest_float("sl_atr", 0.75, 4.0, step=0.25),
                 "rr": trial.suggest_float("rr", 0.75, 4.0, step=0.25)}
            return _score(run(p, a, b), need)
        study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=s))
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
        return dict(study.best_params)

    oos: List[float] = []
    per_fold = []
    for k in range(1, folds + 1):
        try:
            p = tune(0, edges[k], max(10, min_trades // 2), seed + k)
        except Exception as exc:
            log.warning("optuna fold %d failed: %s", k, exc)
            continue
        rs = run(p, edges[k], edges[k + 1])
        oos.extend(rs)
        per_fold.append({"fold": k, "params": p, "n_trades": len(rs),
                         "expectancy_r": round(float(np.mean(rs)), 4) if rs else None})
    final = None
    try:
        final = tune(0, n, min_trades, seed)
    except Exception as exc:
        log.warning("optuna final tuning failed: %s", exc)
    m = trade_metrics(oos)
    ok = (m.get("n_trades", 0) >= min_trades and (m.get("expectancy_r") or 0) > 0
          and (m.get("profit_factor") or 0) > 1.0)
    return {"verdict": "WALK_FORWARD_POSITIVE" if ok else "WALK_FORWARD_NEGATIVE",
            "walk_forward": {k: m.get(k) for k in ("n_trades", "expectancy_r", "win_rate", "profit_factor",
                                                    "max_drawdown_r")},
            "folds": per_fold, "params_on_all_data": final, "trials_per_fold": n_trials}
