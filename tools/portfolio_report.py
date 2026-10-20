#!/usr/bin/env python3
"""Judge the PORTFOLIO, not the strategies one at a time.

A per-strategy win-rate table answers the wrong question. A strategy with a 70% win rate that loses on
exactly the days everything else loses is worth less than a 45% strategy that wins when the rest are
down, and no column in a per-strategy table can show that. What matters is what the book did together:

  TOTAL NET R          the only figure that pays. Win rates do not add up to money.
  DRAWDOWN             depth and duration. Depth is what the risk tiers watch; duration is what makes
                       people switch a working system off.
  CORRELATED LOSSES    how often several strategies lost in the same window. Independent-looking
                       strategies that lose together are one strategy wearing several names, and the
                       portfolio risk model will have been sized as if they were separate.
  MARGINAL CONTRIBUTION  leave-one-out: total net R with this strategy removed, versus with it in.
                       This is the question "should this strategy exist", and it is NOT the same as
                       the strategy's own total - removing a strategy that loses while others win can
                       still hurt if it was carrying the book on their bad days.

    python tools/portfolio_report.py
    python tools/portfolio_report.py --bucket W     # weekly instead of daily co-loss buckets
    python tools/portfolio_report.py --json
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CLOSED = ("CLOSED", "BROKER_CLOSED")


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
    """Closed trades with an R multiple and a timestamp, oldest first."""
    out = []
    for t in rows:
        if str(t.get("state") or "").upper() not in CLOSED:
            continue
        r = _num(t.get("r_multiple"))
        when = _when(t.get("closed_at") or t.get("opened_at"))
        if r is None or when is None:
            continue
        out.append({"strategy": str(t.get("strategy_id") or "unknown"),
                    "symbol": str(t.get("symbol") or "unknown"),
                    "r": r, "when": when})
    out.sort(key=lambda t: t["when"])
    return out


# ------------------------------------------------------------------ drawdown
def drawdown(rs: Sequence[float]) -> Dict:
    """Depth and duration of the worst peak-to-trough stretch of the cumulative R curve.

    An open drawdown at the end counts, because that is the one you are living in."""
    peak = 0.0
    equity = 0.0
    worst = 0.0
    trough_i = peak_i = 0
    longest = 0
    cur_start: Optional[int] = None
    for i, r in enumerate(rs):
        equity += r
        if equity >= peak:
            peak = equity
            if cur_start is not None:
                longest = max(longest, i - cur_start)
                cur_start = None
        else:
            if cur_start is None:
                cur_start = i
            dd = peak - equity
            if dd > worst:
                worst, trough_i, peak_i = dd, i, cur_start
    if cur_start is not None:
        longest = max(longest, len(rs) - cur_start)
    return {"max_drawdown_r": round(worst, 3), "trough_trade": trough_i,
            "started_at_trade": peak_i, "longest_drawdown_trades": longest,
            "still_open": cur_start is not None}


# ------------------------------------------------------------------ co-losses
def _bucket(when: datetime, mode: str) -> str:
    return when.strftime("%G-W%V") if mode.upper() == "W" else when.strftime("%Y-%m-%d")


def correlated_losses(trades: Sequence[dict], mode: str = "D") -> Dict:
    """How often several strategies lost in the same bucket, and which pairs did it most.

    `together_rate` is the share of losing buckets in which two or more strategies lost. High means the
    book is less diversified than the strategy count suggests."""
    per_bucket: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in trades:
        per_bucket[_bucket(t["when"], mode)][t["strategy"]] += t["r"]

    losing_buckets = 0
    multi_loss_buckets = 0
    pair_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    pair_possible: Dict[Tuple[str, str], int] = defaultdict(int)
    worst: List[Tuple[str, float, int]] = []

    for bucket, per_strategy in per_bucket.items():
        losers = sorted(s for s, r in per_strategy.items() if r < 0)
        total = sum(per_strategy.values())
        if total < 0:
            losing_buckets += 1
            if len(losers) >= 2:
                multi_loss_buckets += 1
                worst.append((bucket, round(total, 2), len(losers)))
        present = sorted(per_strategy)
        for i, a in enumerate(present):
            for b in present[i + 1:]:
                pair_possible[(a, b)] += 1
                if a in losers and b in losers:
                    pair_counts[(a, b)] += 1

    pairs = []
    for pair, n in sorted(pair_counts.items(), key=lambda kv: -kv[1]):
        poss = pair_possible[pair]
        pairs.append({"pair": list(pair), "lost_together": n, "both_traded": poss,
                      "rate": round(n / poss, 3) if poss else None})
    worst.sort(key=lambda w: w[1])
    return {"bucket": "week" if mode.upper() == "W" else "day",
            "buckets": len(per_bucket),
            "losing_buckets": losing_buckets,
            "multi_strategy_loss_buckets": multi_loss_buckets,
            "together_rate": round(multi_loss_buckets / losing_buckets, 3) if losing_buckets else None,
            "worst_buckets": [{"bucket": b, "net_r": r, "strategies_losing": n} for b, r, n in worst[:5]],
            "pairs": pairs[:10]}


# ------------------------------------------------------------------ marginal contribution
def marginal_contribution(trades: Sequence[dict]) -> Dict[str, Dict]:
    """Leave-one-out. For each strategy: what the portfolio looks like with it removed.

    `marginal_r` is total-with minus total-without, which equals the strategy's own total R. The number
    that is NOT obvious, and the reason this function exists, is what removing it does to DRAWDOWN: a
    strategy can lose money overall and still have reduced the worst stretch, by winning on the days the
    others lost. `drawdown_delta` positive means removing it would make the worst drawdown deeper."""
    all_r = [t["r"] for t in trades]
    total = sum(all_r)
    base_dd = drawdown(all_r)["max_drawdown_r"]
    strategies = sorted({t["strategy"] for t in trades})

    out: Dict[str, Dict] = {}
    for sid in strategies:
        without = [t["r"] for t in trades if t["strategy"] != sid]
        own = [t["r"] for t in trades if t["strategy"] == sid]
        wo_total = sum(without)
        wo_dd = drawdown(without)["max_drawdown_r"] if without else 0.0
        out[sid] = {
            "n_trades": len(own),
            "own_total_r": round(sum(own), 3),
            "portfolio_without_r": round(wo_total, 3),
            "marginal_r": round(total - wo_total, 3),
            "portfolio_dd_without": round(wo_dd, 3),
            # positive => removing this strategy DEEPENS the drawdown => it was cushioning the book
            "drawdown_delta": round(wo_dd - base_dd, 3),
        }
        # Materiality, not sign. A 0.04R shift in a 15R drawdown is noise, and labelling it a finding
        # would invite acting on it. The bar scales with the drawdown being measured.
        material = max(0.25, 0.05 * base_dd)
        out[sid]["material_threshold_r"] = round(material, 3)
        verdict = []
        if out[sid]["marginal_r"] > 0:
            verdict.append("adds R")
        elif out[sid]["marginal_r"] < 0:
            verdict.append("costs R")
        if out[sid]["drawdown_delta"] > material:
            verdict.append("cushions drawdown")
        elif out[sid]["drawdown_delta"] < -material:
            verdict.append("deepens drawdown")
        out[sid]["verdict"] = ", ".join(verdict) or "no measurable effect"
    return out


def build(rows: Sequence[dict], bucket: str = "D") -> Dict:
    trades = load(rows)
    if not trades:
        return {"n_trades": 0, "note": "no closed trades with an R multiple and a timestamp"}
    rs = [t["r"] for t in trades]
    n = len(rs)
    sd = statistics.stdev(rs) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n else 0.0
    mean = statistics.fmean(rs)
    return {
        "n_trades": n,
        "total_net_r": round(sum(rs), 3),
        "mean_r": round(mean, 4),
        "sd_r": round(sd, 4),
        "ci95": [round(mean - 1.96 * se, 4), round(mean + 1.96 * se, 4)] if n > 1 else None,
        "win_rate": round(sum(1 for r in rs if r > 0) / n, 3),
        "from": trades[0]["when"].isoformat(),
        "to": trades[-1]["when"].isoformat(),
        "drawdown": drawdown(rs),
        "correlated_losses": correlated_losses(trades, bucket),
        "marginal": marginal_contribution(trades),
    }


def render(d: Dict) -> str:
    if not d.get("n_trades"):
        return d.get("note", "no data")
    out: List[str] = []
    dd, cl = d["drawdown"], d["correlated_losses"]

    out.append("PORTFOLIO")
    out.append(f"  trades                {d['n_trades']}")
    out.append(f"  TOTAL NET R           {d['total_net_r']:+.2f}R        <- the only figure that pays")
    out.append(f"  per trade             {d['mean_r']:+.4f}R"
               + (f"   95% [{d['ci95'][0]:+.3f}, {d['ci95'][1]:+.3f}]" if d.get("ci95") else ""))
    out.append(f"  win rate              {d['win_rate'] * 100:.1f}%")
    if d.get("ci95") and d["ci95"][0] < 0 < d["ci95"][1]:
        out.append("  -> the interval spans zero: this portfolio has not been shown to have an edge")
    out.append("")
    out.append("DRAWDOWN")
    out.append(f"  worst peak-to-trough  {dd['max_drawdown_r']:.2f}R")
    out.append(f"  longest stretch       {dd['longest_drawdown_trades']} trades"
               + ("  (still open)" if dd["still_open"] else ""))
    out.append("")
    out.append(f"CORRELATED LOSSES  (per {cl['bucket']})")
    out.append(f"  losing {cl['bucket']}s        {cl['losing_buckets']} of {cl['buckets']}")
    if cl["together_rate"] is not None:
        out.append(f"  2+ strategies losing  {cl['multi_strategy_loss_buckets']} "
                   f"({cl['together_rate'] * 100:.0f}% of losing {cl['bucket']}s)")
        if cl["together_rate"] > 0.5:
            out.append("  -> most losing periods hit several strategies at once. They are less")
            out.append("     independent than the strategy count suggests, and the portfolio risk")
            out.append("     model will have sized them as if they were separate.")
    if cl["pairs"]:
        out.append("  pairs that lose together most often:")
        for p in cl["pairs"][:5]:
            rate = "" if p["rate"] is None else f"{p['rate'] * 100:.0f}% of shared {cl['bucket']}s"
            out.append(f"      {p['pair'][0]} + {p['pair'][1]}: {p['lost_together']}/{p['both_traded']}  {rate}")
    out.append("")
    out.append("MARGINAL CONTRIBUTION  (what the portfolio looks like WITHOUT each strategy)")
    out.append(f"  {'strategy':<24}{'n':>5}{'own R':>9}{'portfolio w/o':>15}{'dd w/o':>9}  verdict")
    for sid, m in sorted(d["marginal"].items(), key=lambda kv: kv[1]["marginal_r"]):
        out.append(f"  {sid:<24}{m['n_trades']:>5}{m['own_total_r']:>+9.2f}"
                   f"{m['portfolio_without_r']:>+15.2f}{m['portfolio_dd_without']:>9.2f}  {m['verdict']}")
    out.append("")
    out.append("  'cushions drawdown' means removing it would make the worst stretch DEEPER - that")
    out.append("  strategy was winning when the others lost, which a win-rate table cannot show.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default="D", choices=["D", "W"], help="co-loss bucket: daily or weekly")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from app.config import load_secrets
    from app.storage.repo import PostgresRepo

    repo = PostgresRepo(load_secrets().database_url)
    d = build(repo.select("trades") or [], args.bucket)
    print(json.dumps(d, indent=2, default=str) if args.json else render(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
