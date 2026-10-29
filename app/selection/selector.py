"""Evidence-driven strategy selection.

Reverse-engineered from two commercial ideas, minus their marketing:

* Trade Ideas' Holly AI re-tests every strategy overnight and only trades the ones whose record clears a bar.
* Cryptohopper's "Algorithm Intelligence" scores strategies per market regime and stops using one where it loses.

Here a strategy on a symbol earns a share of its normal risk from its OWN evidence after costs:

    ACTIVE     confidently positive (lower bound > 0, with enough real account trades)   1.0
    PROBATION  positive estimate, not yet confidently so                                 0.5
    UNPROVEN   not enough evidence either way (cold start)                               0.25
    BENCHED    negative estimate, or confidently negative                                 0 -> shadow only

The multiplier only ever REDUCES the strategy's configured `risk_multiplier`; nothing here can raise risk, edit an
entry rule, change a parameter or a promotion status. A benched strategy keeps being evaluated in shadow, so its
counterfactual record keeps growing and it can earn its way back.

The estimate is a shrunk mean: evidence is pulled toward a slightly negative prior (a strategy with no record is
assumed to lose its costs), so a handful of lucky trades cannot activate anything. Live trades count fully;
shadow counterfactuals and backtests count at reduced weight, because their fills are hypothetical.

Pure functions: the trading core gathers the rows, this module only scores them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence

ACTIVE, PROBATION, UNPROVEN, BENCHED = "ACTIVE", "PROBATION", "UNPROVEN", "BENCHED"
CLOSED_STATES = ("CLOSED", "BROKER_CLOSED")
#: regime labels that are not a market condition a strategy can be judged in
NON_REGIMES = {"", "NONE", "UNKNOWN", "STRESS"}


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _epoch(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


@dataclass
class Evidence:
    weighted_sum: float = 0.0
    weight: float = 0.0
    weighted_sq: float = 0.0
    counts: Dict[str, int] = field(default_factory=dict)

    def add(self, source: str, rs: Iterable[Optional[float]], w: float) -> None:
        for r in rs:
            if r is None or w <= 0:
                continue
            self.weighted_sum += w * r
            self.weighted_sq += w * r * r
            self.weight += w
            self.counts[source] = self.counts.get(source, 0) + 1

    def add_summary(self, source: str, n: int, mean_r: Optional[float], w: float, sd_r: float) -> None:
        """A backtest window arrives as (trade count, mean R), not as individual trades."""
        if n <= 0 or mean_r is None or w <= 0:
            return
        self.weighted_sum += w * n * mean_r
        self.weighted_sq += w * n * (sd_r * sd_r + mean_r * mean_r)
        self.weight += w * n
        self.counts[source] = self.counts.get(source, 0) + n


@dataclass
class Score:
    n_eff: float
    mean_r: Optional[float]
    posterior_r: float
    lower_r: float
    upper_r: float
    sd_r: float


def score(ev: Evidence, cfg) -> Score:
    n, k = ev.weight, float(cfg.prior_trades)
    mean = ev.weighted_sum / n if n > 0 else None
    if n >= 2 and mean is not None:
        var = max(ev.weighted_sq / n - mean * mean, 0.0) * n / (n - 1)
        sd = max(math.sqrt(var), 0.1)
    else:
        sd = float(cfg.default_sd_r)
    post = (ev.weighted_sum + k * float(cfg.prior_r)) / (n + k)
    half = float(cfg.z) * sd / math.sqrt(n + k)
    return Score(n, mean, post, post - half, post + half, sd)


def decide(s: Score, live_trades: int, previous: Optional[str], cfg) -> tuple:
    """(state, multiplier, reason). BENCHED is sticky: it lifts only once the estimate clears a margin."""
    if s.n_eff >= cfg.min_bench_evidence and s.upper_r < 0:
        return BENCHED, 0.0, f"confidently negative: upper bound {s.upper_r:+.3f}R"
    if s.n_eff >= cfg.min_evidence and s.posterior_r < 0:
        return BENCHED, 0.0, f"negative after costs: {s.posterior_r:+.3f}R per trade"
    if previous == BENCHED and s.posterior_r <= cfg.unbench_margin_r:
        return BENCHED, 0.0, (f"still benched: {s.posterior_r:+.3f}R, needs above "
                              f"{cfg.unbench_margin_r:+.3f}R to return")
    if s.n_eff < cfg.min_evidence:
        return UNPROVEN, float(cfg.unproven_multiplier), (f"{s.n_eff:.0f} of {cfg.min_evidence:.0f} "
                                                          f"evidence trades")
    if s.lower_r > 0 and live_trades >= cfg.min_live_trades_for_active:
        return ACTIVE, float(cfg.active_multiplier), f"lower bound {s.lower_r:+.3f}R > 0"
    if s.lower_r > 0:
        return PROBATION, float(cfg.probation_multiplier), (f"positive, but only {live_trades} of "
                                                            f"{cfg.min_live_trades_for_active} account trades")
    return PROBATION, float(cfg.probation_multiplier), (f"{s.posterior_r:+.3f}R, lower bound "
                                                        f"{s.lower_r:+.3f}R not yet above 0")


@dataclass
class Allocation:
    strategy_id: str
    symbol: str
    state: str
    multiplier: float
    reason: str
    posterior_r: float
    lower_r: float
    upper_r: float
    n_eff: float
    counts: Dict[str, int]
    vetoed_regimes: List[str] = field(default_factory=list)
    regime_scores: Dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"strategy_id": self.strategy_id, "symbol": self.symbol, "state": self.state,
                "multiplier": self.multiplier, "reason": self.reason,
                "posterior_r": round(self.posterior_r, 6), "lower_r": round(self.lower_r, 6),
                "upper_r": round(self.upper_r, 6), "n_eff": round(self.n_eff, 1), "counts": self.counts,
                "vetoed_regimes": self.vetoed_regimes, "regime_scores": self.regime_scores}

    @classmethod
    def from_dict(cls, d: dict) -> "Allocation":
        return cls(strategy_id=d["strategy_id"], symbol=d.get("symbol", ""), state=d["state"],
                   multiplier=float(d["multiplier"]), reason=d.get("reason", ""),
                   posterior_r=float(d.get("posterior_r", 0.0)), lower_r=float(d.get("lower_r", 0.0)),
                   upper_r=float(d.get("upper_r", 0.0)), n_eff=float(d.get("n_eff", 0.0)),
                   counts=dict(d.get("counts") or {}), vetoed_regimes=list(d.get("vetoed_regimes") or []),
                   regime_scores=dict(d.get("regime_scores") or {}))


def _recent(rows: Sequence[dict], key: str, since: float) -> List[dict]:
    return [r for r in rows if (_epoch(r.get(key)) or 0.0) >= since]


def _trade_rs(rows: Sequence[dict]) -> List[Optional[float]]:
    return [_num(r.get("r_multiple")) for r in rows if str(r.get("state") or "").upper() in CLOSED_STATES]


def _cf_rows(rows: Sequence[dict], gates: Sequence[str]) -> List[dict]:
    """Shadow outcomes, but only for signals rejected for reasons unrelated to how good the setup was."""
    allowed = {g.upper() for g in gates}
    return [r for r in rows if str(r.get("status") or "").upper() == "RESOLVED"
            and str(r.get("gate") or "").upper() in allowed]


def _lab_evidence(ev: Evidence, lab: Optional[dict], version: Optional[str], lab_age_hours: Optional[float],
                  cfg) -> None:
    """Per-strategy research at live parameters (no sweep), only when it is fresh and for the live version."""
    if not lab or lab_age_hours is None or lab_age_hours > cfg.lab_max_age_hours:
        return
    if version and lab.get("strategy_version") and str(lab["strategy_version"]) != str(version):
        return
    sd = _num(lab.get("r_sd")) or float(cfg.default_sd_r)
    for window, w in (("test", cfg.lab_test_weight), ("train", cfg.lab_train_weight)):
        m = lab.get(window) or {}
        ev.add_summary(f"lab_{window}", int(_num(m.get("n_trades")) or 0), _num(m.get("expectancy_r")), w, sd)


def allocate_strategy(strategy_id: str, version: Optional[str], symbol: str, trades: Sequence[dict],
                      counterfactuals: Sequence[dict], lab: Optional[dict], lab_created_at,
                      previous: Optional[Allocation], cfg, now: float) -> Allocation:
    """One strategy on one symbol. `trades` / `counterfactuals` are that strategy's rows on ALL symbols:
    the symbol's own rows decide its allocation, the pooled rows decide the regime veto."""
    since = now - cfg.lookback_days * 86400.0
    lab_ts = _epoch(lab_created_at)
    lab_age_hours = (now - lab_ts) / 3600.0 if lab_ts is not None else None
    trades = _recent(trades, "closed_at", since)
    cfs = _recent(_cf_rows(counterfactuals, cfg.counterfactual_gates), "resolved_at", since)
    own_trades = [t for t in trades if t.get("symbol") == symbol]
    own_cfs = [c for c in cfs if c.get("symbol") == symbol]

    ev = Evidence()
    live_rs = _trade_rs(own_trades)
    ev.add("live", live_rs, cfg.live_weight)
    ev.add("shadow", [_num(c.get("hypothetical_r")) for c in own_cfs], cfg.counterfactual_weight)
    _lab_evidence(ev, lab, version, lab_age_hours, cfg)
    s = score(ev, cfg)
    live_n = sum(1 for r in live_rs if r is not None)
    state, mult, reason = decide(s, live_n, previous.state if previous else None, cfg)

    vetoed, regime_scores = [], {}
    if cfg.regime_veto:
        prev_veto = set(previous.vetoed_regimes) if previous else set()
        by_regime: Dict[str, Evidence] = {}
        for t in trades:
            reg = str(t.get("regime") or "").upper()
            if reg not in NON_REGIMES:
                by_regime.setdefault(reg, Evidence()).add("live", _trade_rs([t]), cfg.live_weight)
        for c in cfs:
            reg = str(c.get("regime") or "").upper()
            if reg not in NON_REGIMES:
                by_regime.setdefault(reg, Evidence()).add("shadow", [_num(c.get("hypothetical_r"))],
                                                         cfg.counterfactual_weight)
        for reg, rev in sorted(by_regime.items()):
            rs = score(rev, cfg)
            rstate, _, rreason = decide(rs, rev.counts.get("live", 0), BENCHED if reg in prev_veto else None, cfg)
            regime_scores[reg] = {"posterior_r": round(rs.posterior_r, 4), "n_eff": round(rs.n_eff, 1),
                                  "state": rstate}
            if rstate == BENCHED:
                vetoed.append(reg)
    return Allocation(strategy_id, symbol, state, mult, reason, s.posterior_r, s.lower_r, s.upper_r, s.n_eff,
                      dict(ev.counts), vetoed, regime_scores)
