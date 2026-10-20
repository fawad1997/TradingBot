#!/usr/bin/env python3
"""Is the cost gate's estimate right? The one question that could legitimately produce more trades.

The cost gate refuses a signal when its estimated round-trip cost exceeds a fraction of the money at
risk, and it is among the most frequent refusals in the system. The estimate is persisted on every
signal as `cost_r`. Nothing has ever compared it against what trades actually cost.

Kissell ch.3:

    Z = (estimated cost - actual cost) / timing risk        should be N(0, 1)

A positive mean says the model OVERestimates: the gate has been refusing trades it should have taken,
and loosening it is then justified by measurement rather than by hope. That is the only route in the
whole backlog to more trades without a new edge. A negative mean is more dangerous - trades cost more
than the gate believes, so some that passed should not have.

HOW ACTUAL COST IS MEASURED

    actual_cost_r = side * (entry_price - decision_price) / stop_distance

    The decision price is `signals.entry`, the price the strategy committed to when it produced the
    plan. This is Perold's implementation shortfall benchmark: the cost measured from the moment of
    the DECISION, not the moment of the fill, so it includes the round-robin wait and gate processing.

HOW TIMING RISK IS MEASURED

    Kissell derives it from price volatility over the trading horizon. ATR is not stored on the
    signal row, so rather than invent a number this uses the EMPIRICAL dispersion of actual costs for
    the same symbol. That is a defensible denominator - it is the actual variability of the quantity
    being predicted - but it is not Kissell's formulation, and the distinction matters: with an
    empirical scale the variance test is partly circular, so read the MEAN of z, not its variance.

    Pass --atr-timing-risk to use the modelled version instead, where ATR is available in features.

    python tools/cost_model_audit.py
    python tools/cost_model_audit.py --since 2026-10-17 --json

Read only. A test walks the module's call graph and fails if that changes.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.execution.cost_audit import audit_cost_model, mean_sd        # noqa: E402

DEFAULT_SINCE = "2026-10-17"


def _f(v) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _when(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _sign(direction) -> int:
    return -1 if "SELL" in str(direction or "").upper() else 1


def build_rows(signals: List[dict], trades: List[dict]) -> List[Dict[str, Any]]:
    """Join signals to their fills and compute estimated vs actual cost, both in R."""
    by_signal = {}
    for t in trades:
        sid = t.get("signal_id")
        if sid is not None and t.get("entry_price") is not None:
            by_signal[sid] = t
    out: List[Dict[str, Any]] = []
    for s in signals:
        est = _f(s.get("cost_r"))
        t = by_signal.get(s.get("id"))
        if est is None or t is None:
            continue
        decision, sl = _f(s.get("entry")), _f(s.get("sl"))
        fill = _f(t.get("entry_price"))
        if decision is None or sl is None or fill is None:
            continue
        stop = abs(decision - sl)
        if stop <= 0:
            continue
        actual = _sign(s.get("direction")) * (fill - decision) / stop
        out.append({
            "signal_id": s.get("id"), "symbol": s.get("symbol"),
            "strategy_id": s.get("strategy_id"), "direction": s.get("direction"),
            "estimated_cost_r": est, "actual_cost_r": actual,
            "stop_distance": stop, "decision_price": decision, "fill_price": fill,
        })
    return out


def attach_empirical_timing_risk(rows: List[Dict[str, Any]], floor: float = 1e-4) -> List[Dict[str, Any]]:
    """Use the per-symbol standard deviation of actual cost as the z-score denominator.

    Symbols with too few fills to estimate a dispersion fall back to the pooled figure, and anything
    still degenerate is dropped rather than divided by a near-zero scale.
    """
    by_symbol: Dict[str, List[float]] = {}
    for r in rows:
        by_symbol.setdefault(str(r.get("symbol") or "?"), []).append(r["actual_cost_r"])
    pooled = mean_sd([r["actual_cost_r"] for r in rows])["sd"]
    scales = {k: (mean_sd(v)["sd"] if len(v) >= 5 else pooled) for k, v in by_symbol.items()}
    out = []
    for r in rows:
        tr = scales.get(str(r.get("symbol") or "?"), pooled)
        if tr is None or tr <= floor:
            tr = pooled
        if tr is None or tr <= floor:
            continue
        out.append({**r, "timing_risk": tr})
    return out


def render(d: Dict[str, Any], n_rows: int) -> str:
    out = [f"Cost model audit: {d['n']} fills with both an estimate and a realised cost "
           f"(from {n_rows} joined signals)", ""]
    out.append(f"  mean z   {d['mean_z']:+.4f}    sd {d['sd_z']:.4f}    95% CI "
               f"[{d['ci95'][0]:+.4f}, {d['ci95'][1]:+.4f}]")
    out.append(f"  verdict  {d['verdict']}")
    out.append(f"           {d['what_to_do']}")
    if d.get("by_strategy"):
        out.append("")
        out.append("  By strategy (Kissell: the test must be run separately, a model can be wrong in")
        out.append("  opposite directions for two strategies and still average to zero):")
        for k, v in d["by_strategy"].items():
            out.append(f"       {k:<24} n={v['n']:<5} mean z {v['mean_z']:+.4f}")
    if d.get("by_symbol"):
        out.append("")
        out.append("  By symbol:")
        for k, v in d["by_symbol"].items():
            out.append(f"       {k:<12} n={v['n']:<5} mean z {v['mean_z']:+.4f}")
    out.append("")
    out.append("  Positive mean = the gate expects trades to cost more than they do, and is refusing")
    out.append("  trades it should take. Negative = costs are being understated.")
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description="Validate the cost model against realised costs (Kissell ch.3).")
    p.add_argument("--since", default=DEFAULT_SINCE)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    from app.config import load_secrets
    from app.storage.repo import PostgresRepo
    repo = PostgresRepo(load_secrets().database_url)
    cutoff = _when(a.since)

    def recent(table):
        rows = repo.select(table) or []
        if not cutoff:
            return rows
        return [r for r in rows if (_when(r.get("created_at")) or cutoff) >= cutoff]

    rows = build_rows(recent("signals"), recent("trades"))
    rows = attach_empirical_timing_risk(rows)
    d = audit_cost_model(rows)
    print(json.dumps({**d, "joined_rows": len(rows)}, indent=2, default=str) if a.json
          else render(d, len(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
