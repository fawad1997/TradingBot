"""Combined risk of several open positions, counted as a portfolio rather than a pile of independent bets.

THE PROBLEM THIS REPLACES
-------------------------
Risk was aggregated as `sum(exposure.values())`. Two uncorrelated 1% positions consumed 2% of the budget
even though their combined risk is about 1.41%, so the second strategy to produce a signal was refused on
budget that the first was not really using. The correlated-group check did consult correlation, but only
through `abs(correlation)`, so -0.9 (positions that partly offset) was treated exactly like +0.9
(positions that double up), and direction was never consulted at all.

THE MODEL
---------
Standard portfolio variance over the risk amounts, with direction folded in as a sign:

    combined^2  =  SUM_i SUM_j  r_i * r_j * pair(i, j)

    pair(i, j)  =  max( d_i * d_j * rho_ij , floor )      and pair(i, i) = 1

`r` is the money at risk if that position's stop is hit, `d` is +1 long / -1 short, `rho` is the rolling
correlation already computed by `app/risk/correlation.py`. It behaves the way the arithmetic sum does in
the only case where the arithmetic sum is right, and better everywhere else:

    perfectly correlated, same direction  ->  combined = sum(r)      (unchanged from before)
    uncorrelated                          ->  combined = sqrt(sum(r^2))
    perfectly correlated, opposite        ->  floored, see below

WHY THERE IS A FLOOR, AND WHY IT IS ZERO
----------------------------------------
Taking the hedge credit at face value is how portfolio risk models kill accounts. A pair that has been
-0.9 correlated for months can go to +1 in the hour that matters, and both legs can be stopped out in a
whipsaw even when the instruments really are opposed. So `diversification_floor` clips every pairwise
term from below, and it defaults to **0.0**: the model gives full credit for being UNCORRELATED and no
credit at all for being negatively correlated. Low correlation is a measurable property of the data;
a hedge holding through the event you need it for is a hope.

THREE CHECKS, NOT ONE
---------------------
Diversification lets the portfolio carry more open positions, so on its own it is a lever that only ever
loosens. Two ceilings bound it:

  1. diversified  - the model above, against the normal risk budget. The check that permits more.
  2. gross        - the plain arithmetic sum, against its own larger ceiling. However well diversified
                    the book looks, total money at risk stays bounded. This is the backstop for the
                    model simply being wrong.
  3. stressed     - the model again with every pairwise term raised to `stress_correlation`, which is
                    what a crisis does to a correlation matrix. A book that only fits while markets are
                    calm does not fit.

A position is admitted only if all three pass.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

#: Correlation every pair is assumed to reach under stress. Crises synchronise markets; a book that
#: relies on diversification surviving that assumption is relying on the wrong thing.
DEFAULT_STRESS_CORRELATION = 0.8

#: Lower clip on each pairwise term. 0.0 = credit for being uncorrelated, none for being hedged.
DEFAULT_DIVERSIFICATION_FLOOR = 0.0


@dataclass
class Leg:
    """One position's contribution: how much is at risk, which way, on what instrument."""
    symbol: str
    risk_money: float
    direction: int = 1          # +1 long, -1 short

    def signed(self) -> float:
        return self.risk_money * (1 if self.direction >= 0 else -1)


@dataclass
class PortfolioRisk:
    gross: float                      # plain arithmetic sum - what the old code computed
    diversified: float                # correlation and direction aware
    stressed: float                   # the same, with correlations forced up
    pairs_missing_correlation: List[Tuple[str, str]] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    @property
    def diversification_benefit(self) -> float:
        """How much budget the correlation model frees, as a fraction of the gross sum. 0 when the book
        is perfectly correlated, approaching 1 for many small independent positions."""
        if self.gross <= 0:
            return 0.0
        return max(0.0, 1.0 - self.diversified / self.gross)

    def to_dict(self) -> dict:
        return {"gross": round(self.gross, 4), "diversified": round(self.diversified, 4),
                "stressed": round(self.stressed, 4),
                "diversification_benefit": round(self.diversification_benefit, 4),
                "pairs_missing_correlation": [list(p) for p in self.pairs_missing_correlation],
                **self.detail}


def _rho(a: str, b: str, correlations: Dict[str, Dict[str, Optional[float]]]) -> Optional[float]:
    """Correlation between two symbols from a possibly one-sided mapping."""
    if a == b:
        return 1.0
    for first, second in ((a, b), (b, a)):
        row = correlations.get(first)
        if isinstance(row, dict) and second in row:
            v = row[second]
            if v is None:
                continue
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(f):
                return max(-1.0, min(1.0, f))
    return None


def combine(legs: Sequence[Leg], correlations: Dict[str, Dict[str, Optional[float]]],
            diversification_floor: float = DEFAULT_DIVERSIFICATION_FLOOR,
            stress_correlation: float = DEFAULT_STRESS_CORRELATION,
            missing_correlation_value: float = 1.0) -> PortfolioRisk:
    """Combined risk of `legs` three ways. See the module docstring for the model.

    `missing_correlation_value` is what an unknown pair is assumed to be. It defaults to **1.0** -
    fully correlated - so a pair with no history is treated as the worst case rather than quietly
    granting a diversification benefit the data does not support."""
    legs = [l for l in legs if l.risk_money and math.isfinite(l.risk_money) and l.risk_money > 0]
    if not legs:
        return PortfolioRisk(0.0, 0.0, 0.0)

    gross = sum(l.risk_money for l in legs)
    missing: List[Tuple[str, str]] = []
    var_div = 0.0
    var_str = 0.0

    for i, a in enumerate(legs):
        for j, b in enumerate(legs):
            if i == j:
                var_div += a.risk_money ** 2
                var_str += a.risk_money ** 2
                continue
            rho = _rho(a.symbol, b.symbol, correlations)
            if rho is None:
                rho = missing_correlation_value
                if i < j:
                    missing.append((a.symbol, b.symbol))
            sign = 1.0 if (a.direction >= 0) == (b.direction >= 0) else -1.0
            term = max(sign * rho, diversification_floor)
            var_div += a.risk_money * b.risk_money * term
            # Under stress every pair behaves as at least `stress_correlation`, in whichever direction
            # hurts: same-direction legs reinforce, opposed legs stop reinforcing each other.
            var_str += a.risk_money * b.risk_money * max(sign * stress_correlation,
                                                         diversification_floor)

    diversified = math.sqrt(max(0.0, var_div))
    stressed = math.sqrt(max(0.0, var_str))
    return PortfolioRisk(
        gross=gross, diversified=diversified, stressed=stressed,
        pairs_missing_correlation=missing,
        detail={"legs": len(legs),
                "symbols": sorted({l.symbol for l in legs}),
                "floor": diversification_floor,
                "stress_correlation": stress_correlation})


def admits(candidate: Leg, open_legs: Sequence[Leg],
           correlations: Dict[str, Dict[str, Optional[float]]],
           budget: float, gross_budget: float, stress_budget: float,
           diversification_floor: float = DEFAULT_DIVERSIFICATION_FLOOR,
           stress_correlation: float = DEFAULT_STRESS_CORRELATION,
           missing_correlation_value: float = 1.0) -> Tuple[bool, str, dict]:
    """May `candidate` be opened alongside `open_legs`? Returns (ok, reason, numbers).

    All three ceilings must hold. The reason names the one that refused, with both figures in it, so a
    refusal can be acted on rather than guessed at."""
    combined = combine([*open_legs, candidate], correlations, diversification_floor,
                       stress_correlation, missing_correlation_value)
    nums = combined.to_dict()
    nums.update({"budget": round(budget, 4), "gross_budget": round(gross_budget, 4),
                 "stress_budget": round(stress_budget, 4)})

    if combined.gross > gross_budget:
        return False, (f"PORTFOLIO_GROSS_RISK_LIMIT (gross {combined.gross:.2f} > {gross_budget:.2f}; "
                       f"total money at risk is capped regardless of diversification)"), nums
    if combined.diversified > budget:
        return False, (f"PORTFOLIO_OPEN_RISK_LIMIT (combined {combined.diversified:.2f} > {budget:.2f} "
                       f"after {combined.diversification_benefit * 100:.0f}% diversification credit)"), nums
    if combined.stressed > stress_budget:
        return False, (f"PORTFOLIO_STRESSED_RISK_LIMIT (under correlation {stress_correlation:.2f} the "
                       f"book would carry {combined.stressed:.2f} > {stress_budget:.2f})"), nums
    return True, "OK", nums


def marginal_risk(candidate: Leg, open_legs: Sequence[Leg],
                  correlations: Dict[str, Dict[str, Optional[float]]],
                  diversification_floor: float = DEFAULT_DIVERSIFICATION_FLOOR) -> float:
    """How much the candidate actually ADDS to combined risk, which is the number that should be
    charged against the budget - not its standalone risk. Always between 0 and its own risk_money."""
    before = combine(open_legs, correlations, diversification_floor).diversified
    after = combine([*open_legs, candidate], correlations, diversification_floor).diversified
    return max(0.0, after - before)
