"""Statistics that count every attempt: is a lab result an edge, or what luck alone would produce?

WHY THIS EXISTS
---------------
In October 2026 the lab scored 195 rule x symbol x timeframe cells, each picking the best of 16 stop/target
settings on its training window. 22 of the 46 that reached walk-forward came out positive - about what pure noise
gives - and "ROBUST_ON_TEST" needed only expectancy > 0 and PF > 1 on 15 unseen trades, so +0.01R over 27 trades
passed. Nothing in that verdict asked whether the number could be luck, or how many other cells were looked at.

Four questions, each with a standard answer:

  mean_ci                   How sure are we of R per trade?  Moving-block bootstrap 95% range (trades cluster in
                            regimes, so resampling single trades would understate the uncertainty).
  psr / dsr                 Is the Sharpe beyond what the BEST of N noise trials would show?  The probabilistic and
                            deflated Sharpe ratios (Bailey & Lopez de Prado 2012, 2014). N must count every cell
                            looked at, kept or not.
  pbo_cscv                  Does picking the best setting in-sample pick a good one out-of-sample?  Probability of
                            backtest overfitting by combinatorially symmetric cross-validation (Bailey, Borwein,
                            Lopez de Prado & Zhu 2015).
  random_direction          Does the ENTRY carry information, or do the exits do the work?  The same entries and the
                            same bracket with random sides (the benchmark in the 2026 replication of the
                            opening-range-breakout paper, which found the rule's edge was about the spread).

`grade` combines them. EDGE needs all three of: a range above zero, DSR >= 0.95, and beating random sides at
p < 0.05. Everything is in R per trade after costs, the bot's definition of accuracy.

Pure functions, numpy only, deterministic for a given seed.
"""
from __future__ import annotations

import math
from itertools import combinations
from statistics import NormalDist
from typing import Dict, Optional, Sequence

import numpy as np

_N = NormalDist()
EULER_GAMMA = 0.5772156649015329

EDGE, NO_EDGE_SHOWN, NEGATIVE, THIN = "EDGE", "NO_EDGE_SHOWN", "NEGATIVE", "THIN"


def _clean(rs) -> np.ndarray:
    x = np.asarray([v for v in (rs if rs is not None else []) if v is not None], float)
    return x[np.isfinite(x)]


def mean_ci(rs, n_boot: int = 2000, block: Optional[int] = None, seed: int = 0, level: float = 0.95) -> dict:
    """Mean R per trade with a moving-block bootstrap range. Block length defaults to n^(1/3)."""
    x = _clean(rs)
    n = len(x)
    if n < 2:
        return {"n": n, "mean": round(float(x.mean()), 4) if n else None, "lo": None, "hi": None}
    b = int(block or max(1, round(n ** (1.0 / 3.0))))
    b = min(b, n)
    rng = np.random.default_rng(seed)
    k = int(math.ceil(n / b))
    starts = rng.integers(0, n - b + 1, size=(n_boot, k))
    idx = (starts[:, :, None] + np.arange(b)[None, None, :]).reshape(n_boot, -1)[:, :n]
    means = x[idx].mean(axis=1)
    a = (1.0 - level) / 2.0
    return {"n": n, "mean": round(float(x.mean()), 4), "lo": round(float(np.quantile(means, a)), 4),
            "hi": round(float(np.quantile(means, 1.0 - a)), 4), "block": b}


def sharpe_stats(rs) -> dict:
    """Per-trade Sharpe (mean / sd, not annualised) with skewness and NON-excess kurtosis (normal = 3)."""
    x = _clean(rs)
    n = len(x)
    if n < 3:
        return {"n": n, "sr": None, "skew": None, "kurt": None}
    m, sd = float(x.mean()), float(x.std(ddof=1))
    if sd <= 0:
        return {"n": n, "sr": None, "skew": None, "kurt": None}
    z = (x - m) / float(x.std(ddof=0) or sd)
    return {"n": n, "sr": m / sd, "skew": float(np.mean(z ** 3)), "kurt": float(np.mean(z ** 4))}


def psr(sr: float, n: int, skew: float = 0.0, kurt: float = 3.0, sr_star: float = 0.0) -> Optional[float]:
    """Probability that the true Sharpe exceeds `sr_star`, given an estimate from n observations."""
    if sr is None or n is None or n < 3:
        return None
    var = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if not np.isfinite(var) or var <= 0:
        return None
    return float(_N.cdf((sr - sr_star) * math.sqrt(n - 1) / math.sqrt(var)))


def expected_max_sr(n_trials: int, var_sr: float) -> float:
    """The Sharpe the best of `n_trials` zero-edge trials would show, given the spread of their estimates."""
    if n_trials is None or n_trials < 2 or var_sr is None or not np.isfinite(var_sr) or var_sr <= 0:
        return 0.0
    a = _N.inv_cdf(1.0 - 1.0 / n_trials)
    b = _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(var_sr) * ((1.0 - EULER_GAMMA) * a + EULER_GAMMA * b)


def dsr(sr: float, n: int, skew: float, kurt: float, n_trials: int, var_sr: float) -> Optional[float]:
    """Deflated Sharpe ratio: PSR against the best-of-n_trials-noise benchmark instead of zero."""
    return psr(sr, n, skew, kurt, expected_max_sr(n_trials, var_sr))


def block_matrix(per_combo: Sequence[tuple], n_bars: int, blocks: int = 8):
    """(sums, counts), each shaped (blocks, combos): per time block, the summed R and trade count of every combo.
    `per_combo` is [(entry bars, r multiples), ...], one pair per setting tried."""
    sums = np.zeros((blocks, len(per_combo)))
    counts = np.zeros((blocks, len(per_combo)))
    width = max(1, int(math.ceil(n_bars / blocks)))
    for m, (bars, rs) in enumerate(per_combo):
        for j, r in zip(bars, rs):
            if r is None or not np.isfinite(r):
                continue
            b = min(blocks - 1, int(j) // width)
            sums[b, m] += r
            counts[b, m] += 1
    return sums, counts


def pbo_cscv(sums: np.ndarray, counts: np.ndarray) -> Optional[dict]:
    """Probability of backtest overfitting over every balanced split of the time blocks.

    For each split the setting with the best in-sample R per trade is found and its out-of-sample rank among all
    settings recorded. PBO is the share of splits where that pick lands in the bottom half out of sample. Noise
    gives about 0.5; a setting that is genuinely best everywhere gives about 0."""
    sums, counts = np.asarray(sums, float), np.asarray(counts, float)
    s, m = sums.shape
    if m < 2 or s < 2 or s % 2:
        return None
    logits = []
    for is_blocks in combinations(range(s), s // 2):
        mask = np.zeros(s, bool)
        mask[list(is_blocks)] = True
        n_is, n_oos = counts[mask].sum(axis=0), counts[~mask].sum(axis=0)
        ok = (n_is > 0) & (n_oos > 0)
        if ok.sum() < 2:
            continue
        perf_is = np.where(ok, sums[mask].sum(axis=0) / np.maximum(n_is, 1), -np.inf)
        perf_oos = np.where(ok, sums[~mask].sum(axis=0) / np.maximum(n_oos, 1), np.nan)
        best = int(np.argmax(perf_is))
        valid = perf_oos[ok]
        rank = float((valid < perf_oos[best]).sum() + 0.5 * ((valid == perf_oos[best]).sum() - 1) + 1)
        omega = rank / (len(valid) + 1.0)
        logits.append(math.log(omega / (1.0 - omega)))
    if not logits:
        return None
    lg = np.asarray(logits)
    return {"pbo": round(float((lg <= 0).mean()), 4), "splits": len(lg), "median_logit": round(float(np.median(lg)), 4)}


def random_direction(r_long, r_short, sides, n: int = 200, seed: int = 0) -> Optional[dict]:
    """The rule's mean R against the same entries with random sides.

    r_long[t] / r_short[t] are entry t's R taken long / short (identical stop and target), sides[t] the side the
    rule chose. p_value is the share of random-side runs that did at least as well (+1 smoothing)."""
    rl, rsh, sd = np.asarray(r_long, float), np.asarray(r_short, float), np.asarray(sides, float)
    ok = np.isfinite(rl) & np.isfinite(rsh)
    rl, rsh, sd = rl[ok], rsh[ok], sd[ok]
    if len(rl) < 2:
        return None
    rule = float(np.where(sd > 0, rl, rsh).mean())
    rng = np.random.default_rng(seed)
    flips = rng.random((n, len(rl))) < 0.5
    means = np.where(flips, rl[None, :], rsh[None, :]).mean(axis=1)
    return {"n": int(len(rl)), "rule": round(rule, 4), "random_mean": round(float(means.mean()), 4),
            "random_p95": round(float(np.quantile(means, 0.95)), 4),
            "edge_vs_random": round(rule - float(means.mean()), 4),
            "p_value": round(float((1 + (means >= rule).sum()) / (n + 1)), 4)}


def grade(ci: Optional[dict], dsr_value: Optional[float], random: Optional[dict], min_trades: int = 20) -> str:
    """EDGE only when the range is above zero, the deflated Sharpe clears 0.95 AND random sides do worse."""
    n = (ci or {}).get("n") or 0
    if n < min_trades or ci.get("lo") is None:
        return THIN
    if ci["hi"] < 0:
        return NEGATIVE
    if (ci["lo"] > 0 and dsr_value is not None and dsr_value >= 0.95
            and random is not None and random.get("p_value", 1.0) < 0.05):
        return EDGE
    return NO_EDGE_SHOWN


def deflate_cells(cells: Dict[str, dict], min_trades: int = 20) -> dict:
    """Second pass over every cell scored in one cycle: deflate each Sharpe by the number of cells LOOKED AT and
    the spread of their Sharpes, then grade. Mutates each cell's `stats` in place; returns the trial summary.

    `cells` maps a label to an entry holding a `stats` dict with `sr`, `skew`, `kurt`, `n`, `ci` and `random`."""
    srs = [c["stats"]["sr"] for c in cells.values()
           if c.get("stats") and c["stats"].get("sr") is not None and (c["stats"].get("n") or 0) >= 3]
    n_trials = len(srs)
    var_sr = float(np.var(srs, ddof=1)) if n_trials >= 2 else 0.0
    counts: Dict[str, int] = {}
    for c in cells.values():
        st = c.get("stats")
        if not st:
            continue
        d = dsr(st.get("sr"), st.get("n") or 0, st.get("skew") or 0.0, st.get("kurt") or 3.0, n_trials, var_sr)
        st["dsr"] = None if d is None else round(d, 4)
        st["grade"] = grade(st.get("ci"), d, st.get("random"), min_trades)
        counts[st["grade"]] = counts.get(st["grade"], 0) + 1
    return {"n_trials": n_trials, "sr_variance": round(var_sr, 6),
            "benchmark_sr": round(expected_max_sr(n_trials, var_sr), 4), "grades": counts}
