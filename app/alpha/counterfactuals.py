"""Counterfactual trading engine (sections 13-14).

Every rejected signal (and every shadow-strategy signal) is followed bar by bar
with the same fill/exit rules as a real trade, so we can measure whether each
safety gate saves money or silently destroys opportunity.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.alpha.exits import SimulatedTrade
from app.core.types import Bar, Side, Signal, new_id
from app.features.frame import FeatureFrame

log = logging.getLogger(__name__)


@dataclass
class _CF:
    id: str
    signal_id: str
    signal_bar_time: int
    sim: SimulatedTrade
    risk_money: Optional[float]
    gate: str


class CounterfactualTracker:
    def __init__(self, ledger, point: float, slip_points: float, max_holding_bars: int, enabled: bool = True):
        self.ledger = ledger
        self.point = point
        self.slip_points = slip_points
        self.max_bars = max_holding_bars
        self.enabled = enabled
        self.open: Dict[str, _CF] = {}

    def add(self, signal: Signal, gate: str, reason: str, rejection_id: Optional[str], signal_bar_time: int,
            spread_points: float, volatility: str, proposed_size: Optional[float] = None,
            risk_money: Optional[float] = None) -> Optional[str]:
        if not self.enabled:
            return None
        plan = signal.plan
        cf_id = new_id()
        sim = SimulatedTrade(plan.direction, plan.entry, plan.sl, plan.tp, plan.exit_model, signal_bar_time + 900,
                             self.slip_points * self.point, 0.0, self.max_bars)
        self.open[cf_id] = _CF(cf_id, signal.id, signal_bar_time, sim, risk_money, gate)
        self.ledger.insert("counterfactuals", {
            "id": cf_id, "created_at": signal.created_at, "updated_at": signal.created_at, "signal_id": signal.id,
            "rejection_id": rejection_id, "strategy_id": signal.strategy_id, "symbol": signal.symbol,
            "direction": str(plan.direction), "gate": gate, "reason": reason, "status": "OPEN",
            "hypothetical_fill": plan.entry, "sl": plan.sl, "tp": plan.tp, "exit_plan": plan.exit_model,
            "proposed_size": proposed_size, "market_snapshot_id": signal.snapshot_id, "regime": signal.regime,
            "session": signal.session, "news_state": signal.event_state, "spread_points": spread_points})
        return cf_id

    def on_bar(self, bar: Bar, ff: Optional[FeatureFrame], i: Optional[int], regime_trend: str,
               now: datetime) -> List[dict]:
        resolved = []
        for cf_id, cf in list(self.open.items()):
            if bar.time <= cf.signal_bar_time:
                continue
            res = cf.sim.step(bar.open, bar.high, bar.low, bar.close, bar.spread * self.point, ff, i, regime_trend)
            if res is None:
                continue
            pnl = res.r_multiple * cf.risk_money if cf.risk_money else None
            row = {"status": "RESOLVED", "updated_at": now, "resolved_at": now, "mfe_r": res.mfe_r,
                   "mae_r": res.mae_r, "hypothetical_exit": res.exit_price, "exit_reason": res.exit_reason,
                   "hypothetical_r": res.r_multiple, "hypothetical_pnl": pnl, "bars_held": res.bars_held}
            self.ledger.update("counterfactuals", "id", cf_id, row)
            resolved.append({"id": cf_id, "gate": cf.gate, **row})
            del self.open[cf_id]
        return resolved

    def restore(self, rows: List[dict], bar_time_of: callable) -> None:
        """Re-open unresolved counterfactuals after a restart (catch-up happens via replay_bars)."""
        for r in rows:
            try:
                side = Side(r["direction"])
                sim = SimulatedTrade(side, float(r["hypothetical_fill"]), float(r["sl"]),
                                     float(r["tp"]) if r.get("tp") is not None else None, r.get("exit_plan") or {},
                                     0, self.slip_points * self.point, 0.0, self.max_bars)
                sbt = int(bar_time_of(r))
                sim.st.entry_time = sbt + 900
                self.open[str(r["id"])] = _CF(str(r["id"]), str(r["signal_id"]), sbt, sim, None, r.get("gate", ""))
            except Exception as e:  # a malformed row must not stop the trading core
                log.warning("could not restore counterfactual %s: %s", r.get("id"), e)

    def open_summary(self) -> dict:
        return {"open": len(self.open)}
