"""Performance metrics, statistical evidence (PSR / DSR), Monte Carlo and the
evidence-level classification (section 28: never "50 trades = proven")."""
from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from statistics import NormalDist
from typing import Dict, Iterable, List, Optional, Sequence

from app.core.types import EvidenceLevel

N = NormalDist()


def _f(x, nd=4):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(x, nd)


def max_drawdown(curve: Sequence[float]) -> float:
    peak, mdd = 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return mdd


def max_drawdown_duration(curve: Sequence[float]) -> int:
    """How many trades the longest underwater stretch lasted, not how deep it went.

    Chan ch.2: a strategy with deep OR lengthy drawdowns is unlikely to have a high Sharpe ratio, and
    these are different failures. Depth is what the risk tiers watch. Duration is what makes people
    switch a working system off - a shallow drawdown that lasts four months ends more strategies than
    a sharp one that recovers in a week.

    Measured in trades rather than days, because that is the index of the curve passed in. The longest
    run still open at the end counts, since an unrecovered drawdown is the one that matters most.
    """
    peak, longest, since_peak = float("-inf"), 0, 0
    for v in curve:
        if v >= peak:
            peak, since_peak = v, 0
        else:
            since_peak += 1
            longest = max(longest, since_peak)
    return longest


def cumulative(rs: Sequence[float]) -> List[float]:
    out, s = [], 0.0
    for r in rs:
        s += r
        out.append(s)
    return out


def trade_metrics(rs: Sequence[float], mfes: Optional[Sequence[float]] = None, maes: Optional[Sequence[float]] = None,
                  givebacks: Optional[Sequence[float]] = None, bars: Optional[Sequence[int]] = None) -> Dict:
    rs = [float(r) for r in rs if r is not None and math.isfinite(r)]
    n = len(rs)
    if n == 0:
        return {"n_trades": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    mean = statistics.fmean(rs)
    sd = statistics.pstdev(rs) if n > 1 else 0.0
    downside = math.sqrt(sum(min(0.0, r) ** 2 for r in rs) / n)
    gross_win, gross_loss = sum(wins), -sum(losses)
    srt = sorted(rs)
    streak = worst = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    m = {
        "n_trades": n,
        "win_rate": _f(len(wins) / n),
        "expectancy_r": _f(mean),
        "avg_r": _f(mean),
        "median_r": _f(statistics.median(rs)),
        "avg_win_r": _f(statistics.fmean(wins)) if wins else None,
        "avg_loss_r": _f(statistics.fmean(losses)) if losses else None,
        "profit_factor": _f(gross_win / gross_loss) if gross_loss > 0 else None,
        "total_r": _f(sum(rs)),
        "max_drawdown_r": _f(max_drawdown(cumulative(rs))),
        "sharpe_per_trade": _f(mean / sd) if sd > 0 else None,
        "sortino_per_trade": _f(mean / downside) if downside > 0 else None,
        "tail_win_p95_r": _f(srt[int(0.95 * (n - 1))]),
        "tail_loss_p05_r": _f(srt[int(0.05 * (n - 1))]),
        "max_consecutive_losses": worst,
        "skew": _f(_skew(rs)),
        "kurtosis": _f(_kurt(rs)),
    }
    if mfes:
        pairs = [(r, f) for r, f in zip(rs, mfes) if f is not None and f > 0]
        cap = [max(-1.0, min(1.5, r / f)) for r, f in pairs if r > 0]
        m["avg_mfe_r"] = _f(statistics.fmean([f for f in mfes if f is not None])) if mfes else None
        m["mfe_captured_pct"] = _f(100 * statistics.fmean(cap), 2) if cap else None
    if maes:
        vals = [a for a in maes if a is not None]
        m["avg_mae_r"] = _f(statistics.fmean(vals)) if vals else None
    if givebacks:
        g = [x for x, r in zip(givebacks, rs) if x is not None and r > 0]
        m["avg_giveback_r_winners"] = _f(statistics.fmean(g)) if g else None
        m["max_giveback_r"] = _f(max((x for x in givebacks if x is not None), default=0.0))
    if bars:
        b = [x for x in bars if x is not None]
        m["avg_holding_bars"] = _f(statistics.fmean(b), 2) if b else None
    return m


def _skew(x: Sequence[float]) -> float:
    n = len(x)
    if n < 3:
        return 0.0
    mu, sd = statistics.fmean(x), statistics.pstdev(x)
    return 0.0 if sd == 0 else sum(((v - mu) / sd) ** 3 for v in x) / n


def _kurt(x: Sequence[float]) -> float:
    """Non-excess kurtosis (normal = 3)."""
    n = len(x)
    if n < 4:
        return 3.0
    mu, sd = statistics.fmean(x), statistics.pstdev(x)
    return 3.0 if sd == 0 else sum(((v - mu) / sd) ** 4 for v in x) / n


def probabilistic_sharpe(rs: Sequence[float], sr_benchmark: float = 0.0) -> Optional[float]:
    """PSR (Bailey & Lopez de Prado): P(true per-trade Sharpe > benchmark)."""
    n = len(rs)
    if n < 10:
        return None
    sd = statistics.pstdev(rs)
    if sd == 0:
        return None
    sr = statistics.fmean(rs) / sd
    g3, g4 = _skew(rs), _kurt(rs)
    denom = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return None
    z = (sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(denom)
    return N.cdf(z)


def deflated_sharpe(rs: Sequence[float], trial_sharpes: Sequence[float]) -> Optional[float]:
    """DSR: PSR against the Sharpe expected from the best of N independent trials
    (corrects for having tried several parameter sets)."""
    trials = [s for s in trial_sharpes if s is not None and math.isfinite(s)]
    if len(trials) < 2:
        return probabilistic_sharpe(rs, 0.0)
    nt = len(trials)
    v = statistics.pvariance(trials)
    gamma = 0.5772156649
    sr0 = math.sqrt(v) * ((1 - gamma) * N.inv_cdf(1 - 1.0 / nt) + gamma * N.inv_cdf(1 - 1.0 / (nt * math.e)))
    return probabilistic_sharpe(rs, sr0)


def monte_carlo(rs: Sequence[float], n_sims: int = 5000, horizon: Optional[int] = None, seed: int = 42,
                risk_fraction: float = 0.01, ruin_equity_fraction: float = 0.5) -> Dict:
    """Bootstrap the trade sequence: distribution of total R, max drawdown (R) and
    the probability that compounding at `risk_fraction` loses `ruin_equity_fraction`."""
    rs = [r for r in rs if r is not None and math.isfinite(r)]
    if len(rs) < 20:
        return {"note": "fewer than 20 trades - Monte Carlo not meaningful"}
    rnd = random.Random(seed)
    h = horizon or len(rs)
    totals, dds, ruins = [], [], 0
    for _ in range(n_sims):
        seq = [rs[rnd.randrange(len(rs))] for _ in range(h)]
        totals.append(sum(seq))
        dds.append(max_drawdown(cumulative(seq)))
        eq, peak, ruined = 1.0, 1.0, False
        for r in seq:
            eq *= max(0.0, 1.0 + risk_fraction * r)
            peak = max(peak, eq)
            if eq <= peak * (1 - ruin_equity_fraction):
                ruined = True
                break
        ruins += ruined
    totals.sort()
    dds.sort()

    def q(a, p):
        return round(a[int(p * (len(a) - 1))], 3)
    return {"sims": n_sims, "horizon_trades": h, "total_r_p05": q(totals, 0.05), "total_r_p50": q(totals, 0.5),
            "total_r_p95": q(totals, 0.95), "prob_total_r_negative": round(sum(1 for t in totals if t < 0) / n_sims, 4),
            "max_dd_r_p50": q(dds, 0.5), "max_dd_r_p95": q(dds, 0.95), "max_dd_r_p99": q(dds, 0.99),
            "risk_fraction_assumed": risk_fraction,
            f"prob_lose_{int(ruin_equity_fraction * 100)}pct_from_peak": round(ruins / n_sims, 4)}


def group_metrics(trades: List[dict], key: str) -> Dict[str, Dict]:
    groups: Dict[str, List[float]] = defaultdict(list)
    for t in trades:
        groups[str(t.get(key))].append(t["r_multiple"])
    return {k: {"n": len(v), "expectancy_r": _f(statistics.fmean(v)), "win_rate": _f(sum(1 for r in v if r > 0) / len(v)),
                "total_r": _f(sum(v))} for k, v in sorted(groups.items())}


def welch_p_value(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """Two-sided p-value that two groups have the same mean R (normal approximation)."""
    if len(a) < 10 or len(b) < 10:
        return None
    va, vb = statistics.variance(a), statistics.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se == 0:
        return None
    z = (statistics.fmean(a) - statistics.fmean(b)) / se
    return round(2 * (1 - N.cdf(abs(z))), 4)


def classify_evidence(m: Dict, cost_stressed: Optional[Dict] = None, wfa: Optional[Dict] = None,
                      stability_ok: Optional[bool] = None, dsr: Optional[float] = None) -> EvidenceLevel:
    n = m.get("n_trades", 0)
    exp = m.get("expectancy_r") or 0.0
    pf = m.get("profit_factor") or 0.0
    if n < 30:
        return EvidenceLevel.INSUFFICIENT
    if exp <= 0 and n >= 100:
        return EvidenceLevel.INVALIDATED
    if n < 100:
        return EvidenceLevel.PRELIMINARY
    level = EvidenceLevel.PRELIMINARY
    stressed_ok = cost_stressed is None or (cost_stressed.get("expectancy_r") or 0) > 0
    if exp > 0 and pf > 1.1 and stressed_ok:
        level = EvidenceLevel.PROMISING
    if (level == EvidenceLevel.PROMISING and wfa is not None and (wfa.get("expectancy_r") or 0) > 0
            and wfa.get("n_trades", 0) >= 100 and stability_ok and (dsr is None or dsr >= 0.9)
            and cost_stressed is not None):
        level = EvidenceLevel.ROBUST
    return level
