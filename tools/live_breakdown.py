#!/usr/bin/env python3
"""What the LIVE record says, per strategy and per strategy+symbol, with honest error bars.

Why this exists alongside the research lab: for any strategy that has actually traded, live evidence beats
backtest evidence. The lab replays history under modelled costs; this reads what the account really did.
When the two disagree, the live record is the one that paid.

The number that matters is not the mean - it is the confidence interval. A strategy showing +0.31R per
trade over 23 trades and one showing +0.31R over 2300 trades are not the same finding, and printing only
the mean hides that completely. Every row here carries a 95% interval, and the VERDICT column says
NOT_PROVEN whenever that interval contains zero, however good the mean looks.

    python tools/live_breakdown.py                 # by strategy, then by strategy+symbol
    python tools/live_breakdown.py --min-trades 10 # hide cells too small to read
    python tools/live_breakdown.py --json
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CLOSED = ("CLOSED", "BROKER_CLOSED")


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def summarise(rs: Sequence[float]) -> Dict:
    """Mean R per trade with a 95% interval. Fewer than 2 trades gets no interval, because it has none."""
    rs = [r for r in rs if r is not None and math.isfinite(r)]
    n = len(rs)
    if n == 0:
        return {"n": 0, "mean_r": None, "total_r": 0.0, "verdict": "NO_TRADES"}
    total = sum(rs)
    mean = statistics.fmean(rs)
    wins = sum(1 for r in rs if r > 0)
    out = {"n": n, "mean_r": round(mean, 4), "total_r": round(total, 2),
           "win_rate": round(wins / n, 3)}
    if n < 2:
        out.update(lo=None, hi=None, verdict="TOO_FEW")
        return out
    sd = statistics.stdev(rs)
    se = sd / math.sqrt(n)
    lo, hi = mean - 1.96 * se, mean + 1.96 * se
    out.update(sd=round(sd, 4), se=round(se, 4), lo=round(lo, 4), hi=round(hi, 4))
    if lo > 0:
        out["verdict"] = "POSITIVE"
    elif hi < 0:
        out["verdict"] = "NEGATIVE"
    else:
        out["verdict"] = "NOT_PROVEN"
    return out


def trades_needed(sd: float, effect: float) -> Optional[int]:
    """Roughly how many trades before an edge of `effect` R would clear zero at 95%."""
    if not sd or not effect or effect <= 0:
        return None
    return int(math.ceil((1.96 * sd / effect) ** 2))


def build(rows: Sequence[dict], min_trades: int = 1) -> Dict:
    by_strategy: Dict[str, List[float]] = defaultdict(list)
    by_cell: Dict[tuple, List[float]] = defaultdict(list)
    overall: List[float] = []

    for t in rows:
        if str(t.get("state") or "").upper() not in CLOSED:
            continue
        r = _num(t.get("r_multiple"))
        if r is None:
            continue
        sid = str(t.get("strategy_id") or "unknown")
        sym = str(t.get("symbol") or "unknown")
        by_strategy[sid].append(r)
        by_cell[(sid, sym)].append(r)
        overall.append(r)

    strategies = {sid: summarise(rs) for sid, rs in by_strategy.items()}
    cells = {f"{sid} | {sym}": summarise(rs) for (sid, sym), rs in by_cell.items()
             if len(rs) >= min_trades}
    return {"overall": summarise(overall), "by_strategy": strategies, "by_cell": cells,
            "cells_total": len(by_cell), "cells_shown": len(cells)}


def _row(name: str, s: Dict, width: int) -> str:
    if not s.get("n"):
        return f"  {name:<{width}}  {'0':>5}"
    ci = "" if s.get("lo") is None else f"[{s['lo']:+.3f}, {s['hi']:+.3f}]"
    return (f"  {name:<{width}}  {s['n']:>5}  {s['total_r']:>8.2f}R  {s['mean_r']:>+8.4f}R  "
            f"{(s.get('win_rate') or 0) * 100:>5.1f}%  {ci:>20}  {s['verdict']}")


def render(d: Dict) -> str:
    out: List[str] = []
    o = d["overall"]
    out.append("OVERALL")
    out.append(_row("all closed trades", o, 34))
    if o.get("verdict") == "NOT_PROVEN" and o.get("sd"):
        need = trades_needed(o["sd"], abs(o["mean_r"]) or 0.05)
        if need:
            out.append(f"        at this spread of outcomes, ~{need} trades would be needed for the current "
                       f"mean to clear zero")
    out.append("")
    hdr = f"  {'':<34}  {'n':>5}  {'total':>9}  {'per trade':>9}  {'win':>6}  {'95% interval':>20}  verdict"

    out.append("BY STRATEGY")
    out.append(hdr)
    for sid, s in sorted(d["by_strategy"].items(), key=lambda kv: kv[1].get("total_r") or 0):
        out.append(_row(sid, s, 34))

    out.append("")
    out.append(f"BY STRATEGY + SYMBOL  ({d['cells_shown']} of {d['cells_total']} cells shown)")
    out.append(hdr)
    for name, s in sorted(d["by_cell"].items(), key=lambda kv: kv[1].get("total_r") or 0):
        out.append(_row(name, s, 34))

    proven = [k for k, v in d["by_cell"].items() if v.get("verdict") in ("POSITIVE", "NEGATIVE")]
    out.append("")
    out.append(f"Cells whose interval excludes zero: {len(proven)} of {d['cells_shown']}")
    if not proven:
        out.append("  None. Every combination is consistent with having no edge at all, in either direction.")
        out.append("  That is the expected result at this sample size and is not a reason to change anything.")
    else:
        for k in proven:
            out.append(f"  {k}: {d['by_cell'][k]['verdict']}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-trades", type=int, default=1, help="hide cells with fewer trades than this")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from app.config import load_secrets
    from app.storage.repo import PostgresRepo

    repo = PostgresRepo(load_secrets().database_url)
    rows = repo.select("trades") or []
    d = build(rows, args.min_trades)
    print(json.dumps(d, indent=2, default=str) if args.json else render(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
