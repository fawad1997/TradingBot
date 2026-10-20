#!/usr/bin/env python3
"""What would different stops and targets have produced, on the trades you already took?

THE QUESTION THIS ANSWERS
-------------------------
Your exits currently average +0.405R on a win and -0.859R on a loss, a payout of 0.47:1. Break-even at
that payout needs a 68% win rate; a perfect coin flip gives 50%. So the geometry loses 0.227R per trade
before any indicator is consulted, and three quarters of the measured -0.309R comes from the shape of the
exits rather than from the quality of the entries.

The obvious reaction is to pick a smaller target, since no recent trade reached even 0.82R against
targets set near 1.5R. Choosing that number by looking at the same trades is curve fitting - the mistake
this whole codebase keeps guarding against. So this tool measures instead of choosing: it replays a grid
of geometries over the trades you actually took, splits them in time, and refuses to recommend anything
the unseen half does not also support.

WHAT IT CAN AND CANNOT SIMULATE - READ THIS BEFORE TRUSTING A NUMBER
--------------------------------------------------------------------
Each trade records `mfe_r` (furthest it went your way) and `mae_r` (furthest against) while it was open.
That is enough for some changes and not others:

  LOWERING a target      simulable. If mfe reached the new target, the trade would have hit it.
  TIGHTENING a stop      simulable. If mae reached the new stop, the trade would have stopped earlier.
  RAISING  a target      partly. A trade whose mfe never reached it simply exits as it really did.
  WIDENING a stop        NOT SIMULABLE, and the tool refuses to pretend. The recorded path ENDS when the
                         trade closed, so for a trade stopped at -1R there is no record of where price
                         went afterwards. Any "wider stops would have worked" claim built from this data
                         is invented, and widening is where that temptation is strongest.

THE ORDERING PROBLEM
--------------------
mfe and mae do not say which came first. A trade that reached +0.8R and -0.9R might have hit either
first, and that decides whether a given geometry takes the target or the stop. So every result is
reported as a PAIR OF BOUNDS:

    optimistic   assume the favourable excursion came first wherever both are reachable
    pessimistic  assume the adverse one did

If both bounds agree, the conclusion holds whatever the real order was. If they disagree, the data cannot
answer it and the tool says so rather than splitting the difference.

    python tools/exit_geometry.py
    python tools/exit_geometry.py --json
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CLOSED = ("CLOSED", "BROKER_CLOSED")
#: Stops wider than this cannot be simulated from recorded excursions - see the module docstring.
MAX_SIMULABLE_STOP = 1.0
DEFAULT_STOPS = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
DEFAULT_TARGETS = (0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.25, 1.5)


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _when(v) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if v is None:
        return None
    try:
        d = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def load(rows: Sequence[dict]) -> List[dict]:
    """Closed trades carrying both excursions. Trades without them cannot be replayed at all."""
    out = []
    for t in rows:
        if str(t.get("state") or "").upper() not in CLOSED:
            continue
        mfe, mae, r = _num(t.get("mfe_r")), _num(t.get("mae_r")), _num(t.get("r_multiple"))
        when = _when(t.get("closed_at"))
        if mfe is None or mae is None or r is None or when is None:
            continue
        out.append({"mfe": mfe, "mae": -abs(mae), "r": r, "when": when,
                    "strategy": str(t.get("strategy_id") or "unknown")})
    out.sort(key=lambda t: t["when"])
    return out


def replay(trade: dict, stop: float, target: float, optimistic: bool) -> float:
    """What this one trade would have returned under a given stop and target, in R of ORIGINAL risk.

    A trade is scaled by the stop: halving the stop doubles the R value of the same price move, which is
    why a tighter stop is not simply "the same trades with smaller losses"."""
    mfe, mae = trade["mfe"], trade["mae"]
    hit_target = mfe >= target
    hit_stop = mae <= -stop
    if hit_target and hit_stop:
        # Both were reachable; which came first is unrecorded. That is the whole reason for two bounds.
        return (target / stop) if optimistic else -1.0
    if hit_target:
        return target / stop
    if hit_stop:
        return -1.0
    # Neither barrier touched: the trade ends where it really ended, rescaled to the new risk unit.
    return trade["r"] / stop


def evaluate(trades: Sequence[dict], stop: float, target: float) -> Dict:
    opt = [replay(t, stop, target, True) for t in trades]
    pes = [replay(t, stop, target, False) for t in trades]
    n = len(opt)
    if not n:
        return {"n": 0}

    def stat(xs):
        mean = statistics.fmean(xs)
        sd = statistics.stdev(xs) if n > 1 else 0.0
        se = sd / math.sqrt(n) if n else 0.0
        return {"mean_r": round(mean, 4), "total_r": round(sum(xs), 2),
                "lo": round(mean - 1.96 * se, 4), "hi": round(mean + 1.96 * se, 4),
                "win_rate": round(sum(1 for x in xs if x > 0) / n, 3)}

    o, p = stat(opt), stat(pes)
    # A conclusion only counts when it survives the worse of the two orderings.
    if o["mean_r"] > 0 and p["mean_r"] > 0:
        verdict = "POSITIVE_BOTH_WAYS"
    elif o["mean_r"] <= 0 and p["mean_r"] <= 0:
        verdict = "NEGATIVE_BOTH_WAYS"
    else:
        verdict = "DEPENDS_ON_ORDER"
    return {"n": n, "stop": stop, "target": target, "optimistic": o, "pessimistic": p,
            "verdict": verdict}


def grid(trades: Sequence[dict], stops=DEFAULT_STOPS, targets=DEFAULT_TARGETS,
         train_fraction: float = 0.7) -> Dict:
    """Every geometry on a time-split record. The split is what stops this being curve fitting."""
    cut = max(1, int(len(trades) * train_fraction))
    train, test = trades[:cut], trades[cut:]
    rows = []
    for s in stops:
        if s > MAX_SIMULABLE_STOP:
            continue                       # refuse rather than invent - see the module docstring
        for t in targets:
            tr, te = evaluate(train, s, t), evaluate(test, s, t)
            if not tr.get("n") or not te.get("n"):
                continue
            rows.append({"stop": s, "target": t, "train": tr, "test": te,
                         # held to the pessimistic ordering on BOTH halves
                         "holds_up": (tr["pessimistic"]["mean_r"] > 0
                                      and te["pessimistic"]["mean_r"] > 0)})
    current = evaluate(trades, 1.0, 1.5)
    return {"n_trades": len(trades), "train": len(train), "test": len(test),
            "current": current, "rows": rows,
            "survivors": [r for r in rows if r["holds_up"]]}


def render(d: Dict) -> str:
    out: List[str] = []
    if not d.get("n_trades"):
        return ("No trades carry both excursions (mfe_r and mae_r), so nothing can be replayed. "
                "Those are recorded as trades close; this needs a record with them populated.")
    cur = d["current"]
    out.append(f"{d['n_trades']} trades with excursions recorded "
               f"({d['train']} to search, {d['test']} held back)\n")
    out.append("CURRENT GEOMETRY  (stop 1.0R, target 1.5R)")
    out.append(f"  optimistic  {cur['optimistic']['mean_r']:+.3f}R per trade   "
               f"win rate {cur['optimistic']['win_rate']*100:.0f}%")
    out.append(f"  pessimistic {cur['pessimistic']['mean_r']:+.3f}R per trade   "
               f"win rate {cur['pessimistic']['win_rate']*100:.0f}%")
    out.append(f"  -> {cur['verdict']}\n")

    rows = sorted(d["rows"], key=lambda r: -r["test"]["pessimistic"]["mean_r"])[:12]
    out.append("BEST ON THE HELD-BACK HALF, judged pessimistically")
    out.append(f"  {'stop':>5}{'target':>8}{'search half':>14}{'held back':>13}   survives?")
    for r in rows:
        out.append(f"  {r['stop']:>5.2f}{r['target']:>8.2f}"
                   f"{r['train']['pessimistic']['mean_r']:>+14.3f}"
                   f"{r['test']['pessimistic']['mean_r']:>+13.3f}   "
                   f"{'yes' if r['holds_up'] else 'no'}")

    surv = d["survivors"]
    out.append("")
    if not surv:
        out.append("NOTHING SURVIVES.")
        out.append("  No stop-and-target combination is positive on both halves under the cautious")
        out.append("  ordering. The geometry is not what is costing you the money - or at least, no")
        out.append("  reachable geometry rescues these entries. Changing the targets would be moving")
        out.append("  a number without evidence behind it.")
    else:
        out.append(f"{len(surv)} combination(s) positive on BOTH halves, pessimistically:")
        for r in sorted(surv, key=lambda r: -r["test"]["pessimistic"]["mean_r"])[:5]:
            t = r["test"]["pessimistic"]
            out.append(f"  stop {r['stop']:.2f}R  target {r['target']:.2f}R   "
                       f"held-back {t['mean_r']:+.3f}R  95% [{t['lo']:+.3f}, {t['hi']:+.3f}]")
        out.append("")
        out.append("  Treat these as candidates to RESEARCH, not settings to adopt. They were chosen by")
        out.append("  searching a grid, so the best of them is flattered by the search itself. Confirm")
        out.append("  against the lab before changing anything that trades.")

    out.append("")
    out.append("Not simulable from this data: WIDER stops. The recorded path ends when the trade closed,")
    out.append("so for anything stopped out there is no record of where price went next. The tool")
    out.append(f"refuses stops above {MAX_SIMULABLE_STOP:.1f}R rather than invent them.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", default=None, help="only this strategy")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from app.config import load_secrets
    from app.storage.repo import PostgresRepo
    repo = PostgresRepo(load_secrets().database_url)
    trades = load(repo.select("trades") or [])
    if args.strategy:
        trades = [t for t in trades if t["strategy"] == args.strategy]
    d = grid(trades)
    print(json.dumps(d, indent=2, default=str) if args.json else render(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
