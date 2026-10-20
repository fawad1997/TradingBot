"""Daily trading report (alert-channel summary + daily_reports table)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from app.execution.book import as_epoch
from app.storage.repo import Repository


def daily_report(repo: Repository, now: datetime) -> Dict[str, Any]:
    start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def today(rows, col="created_at"):
        return [r for r in rows if as_epoch(r.get(col)) >= start]

    trades = today(repo.select("trades", {"state": ["CLOSED", "BROKER_CLOSED"]}, order_by="closed_at", desc=True,
                               limit=500), "closed_at")
    rs = [float(t["r_multiple"]) for t in trades if t.get("r_multiple") is not None]
    pnl = sum(float(t.get("net_pnl") or 0) for t in trades)
    fills = today(repo.select("execution_events", {"event_type": "FILL"}, order_by="id", desc=True, limit=500))
    slips = [float(f["slippage_points"]) for f in fills if f.get("slippage_points") is not None]
    rejections = today(repo.select("rejections", order_by="created_at", desc=True, limit=2000))
    cfs = [c for c in repo.select("counterfactuals", {"status": "RESOLVED"}, order_by="resolved_at", desc=True,
                                  limit=2000) if as_epoch(c.get("resolved_at")) >= start]
    risk = today(repo.select("risk_states", order_by="id", desc=True, limit=500))
    max_dd = max([float(r.get("drawdown") or 0) for r in risk], default=0.0)
    by_gate: Dict[str, float] = {}
    for c in cfs:
        by_gate[c.get("gate") or "?"] = by_gate.get(c.get("gate") or "?", 0.0) + float(c.get("hypothetical_r") or 0)
    return {
        "trades": len(trades),
        "win_rate": round(sum(1 for r in rs if r > 0) / len(rs), 3) if rs else None,
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
        "net_pnl": round(pnl, 2),
        "max_drawdown": round(max_dd, 4),
        "avg_slippage_points": round(sum(slips) / len(slips), 2) if slips else None,
        "rejected_signals": len(rejections),
        "counterfactual_resolved": len(cfs),
        "counterfactual_r": round(sum(float(c.get("hypothetical_r") or 0) for c in cfs), 2),
        "counterfactual_r_by_gate": {k: round(v, 2) for k, v in by_gate.items()},
    }
