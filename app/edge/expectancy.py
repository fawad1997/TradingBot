"""Edge estimation with shrinkage. Stats come from the strategy's validated
evidence package (written by `promote`) - never from optimism."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from app.core.types import EvidenceLevel


@dataclass
class EdgeEstimate:
    n: int
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    gross_r: float          # raw expectancy per trade, R (already net of backtest costs)
    shrunk_r: float         # shrunk toward zero by sample size
    confidence: float       # 0..1
    evidence: EvidenceLevel
    validated: bool

    def kelly_fraction(self) -> float:
        """Full-Kelly f* for a binary approximation: p - (1-p)/b, using shrunk inputs."""
        if self.n <= 0 or self.avg_loss_r <= 0 or self.avg_win_r <= 0:
            return 0.0
        b = self.avg_win_r / self.avg_loss_r
        p = self.win_rate
        # shrink p toward the break-even probability with the same weight as the edge
        p_be = 1.0 / (1.0 + b)
        p_s = p_be + (p - p_be) * self.confidence
        return max(0.0, p_s - (1.0 - p_s) / b)

    def to_dict(self) -> dict:
        return {"n": self.n, "win_rate": self.win_rate, "avg_win_r": self.avg_win_r, "avg_loss_r": self.avg_loss_r,
                "gross_r": self.gross_r, "shrunk_r": self.shrunk_r, "confidence": self.confidence,
                "evidence": str(self.evidence), "validated": self.validated}


def estimate_from_stats(stats: Optional[dict], shrinkage_trades: int) -> EdgeEstimate:
    s = stats or {}
    n = int(s.get("n_trades", 0) or 0)
    evidence = EvidenceLevel(s.get("evidence_level", "INSUFFICIENT"))
    if n <= 0:
        return EdgeEstimate(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, evidence, False)
    exp_r = float(s.get("expectancy_r", 0.0))
    conf = n / (n + float(shrinkage_trades))
    # use the out-of-sample number when present - in-sample stats overstate edge
    oos = s.get("oos_expectancy_r")
    base = float(oos) if oos is not None else exp_r
    shrunk = base * conf
    validated = evidence in (EvidenceLevel.ROBUST, EvidenceLevel.VALIDATED)
    return EdgeEstimate(n=n, win_rate=float(s.get("win_rate", 0.0)), avg_win_r=float(s.get("avg_win_r", 0.0)),
                        avg_loss_r=abs(float(s.get("avg_loss_r", 1.0))), gross_r=base, shrunk_r=shrunk,
                        confidence=conf, evidence=evidence, validated=validated)


def wilson_lower(wins: int, n: int, z: float = 1.64) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom
