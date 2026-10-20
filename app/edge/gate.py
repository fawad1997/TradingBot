"""Edge / cost gate: Expected Net Edge > research-calibrated cost buffer."""
from __future__ import annotations

from dataclasses import dataclass

from app.config import CostCfg
from app.core.types import Environment, Gate
from app.edge.cost import CostEstimate
from app.edge.expectancy import EdgeEstimate


@dataclass
class GateResult:
    passed: bool
    gate: Gate
    reason: str
    net_edge_r: float
    detail: dict


def edge_gate(est: EdgeEstimate, cost: CostEstimate, cfg: CostCfg, env: Environment) -> GateResult:
    detail = {"edge": est.to_dict(), "cost": cost.to_dict(), "buffer_r": cfg.cost_buffer_r}
    # 1. structural cost check - applies to every environment
    if cost.cost_r > cfg.max_cost_fraction_of_risk:
        return GateResult(False, Gate.COST, "COSTS_EXCEED_MAX_FRACTION_OF_RISK", -cost.cost_r, detail)
    net = est.shrunk_r - cost.cost_r
    # 2. evidence check
    if not est.validated:
        if env is Environment.REAL:
            return GateResult(False, Gate.EDGE, "EDGE_UNVALIDATED", net, detail)
        # DRY_RUN / DEMO exist precisely to gather evidence: allow, flagged
        detail["unvalidated_research_trade"] = True
        return GateResult(True, Gate.EDGE, "UNVALIDATED_RESEARCH_TRADE", net, detail)
    # 3. validated strategy: expected net edge must clear the calibrated buffer
    if net <= cfg.cost_buffer_r:
        return GateResult(False, Gate.EDGE, "NET_EDGE_BELOW_BUFFER", net, detail)
    return GateResult(True, Gate.EDGE, "OK", net, detail)
