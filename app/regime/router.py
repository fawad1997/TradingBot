"""Strategy router: regime + strategy status + environment + health -> eligibility.

A strategy is never activated just because the regime label matches. It must
also hold a promotion status that permits the current environment.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from app.core.types import Eligibility, Environment, StrategyHealth, StrategyStatus
from app.governance.health import strategy_health_key
from app.regime.detector import RegimeState

# Which promotion statuses may EXECUTE in which environment
EXECUTION_ALLOWED = {
    Environment.DRY_RUN: {StrategyStatus.DEMO, StrategyStatus.MICRO_LIVE, StrategyStatus.VALIDATED},
    Environment.DEMO: {StrategyStatus.DEMO, StrategyStatus.MICRO_LIVE, StrategyStatus.VALIDATED},
    Environment.REAL: {StrategyStatus.MICRO_LIVE, StrategyStatus.VALIDATED},
    Environment.SHADOW: set(),
}


@dataclass
class RouteResult:
    strategy_id: str
    eligibility: Eligibility
    reason: str
    executable: bool       # may send orders in this environment
    shadow: bool           # evaluate and track as counterfactual only
    risk_multiplier: float = 1.0


def route(strategies: List, regime: RegimeState, env: Environment,
          health: Dict[str, StrategyHealth], shadow_enabled: bool,
          allocations: Optional[Dict] = None) -> Dict[str, RouteResult]:
    """`allocations` ({strategy_id: Allocation}) comes from the selector in auto mode; None leaves routing as is."""
    out: Dict[str, RouteResult] = {}
    for s in strategies:
        sid = s.id
        status = StrategyStatus(s.status)
        h = health.get(strategy_health_key(sid, s.version), StrategyHealth.HEALTHY)
        if not s.enabled or status in (StrategyStatus.DISABLED,) or h == StrategyHealth.DISABLED:
            out[sid] = RouteResult(sid, Eligibility.DISABLED, "disabled", False, False)
            continue
        executable_status = status in EXECUTION_ALLOWED[env]
        research_only = status in (StrategyStatus.RESEARCH_ONLY, StrategyStatus.SHADOW, StrategyStatus.QUARANTINE)
        if research_only or not executable_status:
            out[sid] = RouteResult(sid, Eligibility.RESEARCH_ONLY,
                                   f"status {status} not executable in {env}", False, shadow_enabled)
            continue
        if regime.stress:
            out[sid] = RouteResult(sid, Eligibility.RESTRICTED, "market stress", False, True)
            continue
        allowed = s.allowed_regimes
        if allowed and str(regime.trend) not in allowed:
            out[sid] = RouteResult(sid, Eligibility.DISABLED, f"regime {regime.trend} not in {allowed}", False, True)
            continue
        if h == StrategyHealth.RESTRICTED:
            out[sid] = RouteResult(sid, Eligibility.RESTRICTED, "strategy health RESTRICTED", False, True)
            continue
        mult = float(getattr(s, "risk_multiplier", 1.0) or 1.0)
        alloc = (allocations or {}).get(sid)
        if alloc is not None:
            if str(regime.trend) in alloc.vetoed_regimes:
                out[sid] = RouteResult(sid, Eligibility.RESTRICTED,
                                       f"selector: loses in regime {regime.trend}", False, True)
                continue
            if alloc.multiplier <= 0:
                out[sid] = RouteResult(sid, Eligibility.RESTRICTED,
                                       f"selector {alloc.state}: {alloc.reason}", False, True)
                continue
            mult *= min(1.0, alloc.multiplier)
        if h == StrategyHealth.DEGRADED:
            out[sid] = RouteResult(sid, Eligibility.DEGRADED, "strategy health DEGRADED", True, False, 0.5 * mult)
            continue
        out[sid] = RouteResult(sid, Eligibility.ELIGIBLE, "ok", True, False, mult)
    return out
