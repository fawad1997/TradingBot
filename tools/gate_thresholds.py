#!/usr/bin/env python3
"""Recommend a research-gate threshold for EACH strategy, from that strategy's own record.

The problem this solves. One `gate_min_trades` and one `gate_block_below_r` were applied to all eight
strategies. They are not comparable to each other. On identical history a breakout strategy produced 34
training trades while mean reversion produced 5, so a shared bar of 30 judges the first and leaves the
second permanently unjudged - which looks like caution and is actually a blind spot. And a -0.3R average
is a catastrophe for a scalper taking many small trades, yet well inside ordinary variation for a strategy
taking few large ones.

How the recommendations are derived. Both from the strategy's OWN numbers, never from a constant:

  min_trades     scaled by how often this strategy trades relative to the busiest one, so every strategy
                 reaches a verdict after a comparable amount of CALENDAR time rather than a comparable
                 number of trades. Floored at 10, because below that no verdict means anything.

  block_below_r  scaled by this strategy's own dispersion of outcomes (the standard deviation of its R
                 multiples) relative to the median strategy. A strategy whose results naturally swing
                 twice as wide needs a threshold twice as far from zero before a run of bad luck is
                 treated as proof of no edge.

These are STARTING POINTS, not answers. They say "this bar is proportionate to how this strategy
behaves"; they cannot say "this strategy is good". Paste what you agree with into
`research_lab.gate_overrides` and leave the rest on the global default.

    python tools/gate_thresholds.py            # table plus a ready-to-paste config block
    python tools/gate_thresholds.py --json
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
MIN_SENSIBLE_TRADES = 10
MIN_TRADES_FOR_STATS = 5          # below this a strategy's own sd is itself noise


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def collect(rows: Sequence[dict]) -> Dict[str, dict]:
    """Per strategy: how many trades, how spread out, over how long."""
    rs: Dict[str, List[float]] = defaultdict(list)
    times: Dict[str, List[float]] = defaultdict(list)
    for t in rows:
        if str(t.get("state") or "").upper() not in CLOSED:
            continue
        r = _num(t.get("r_multiple"))
        if r is None:
            continue
        sid = str(t.get("strategy_id") or "unknown")
        rs[sid].append(r)
        ts = t.get("closed_at") or t.get("opened_at")
        if hasattr(ts, "timestamp"):
            times[sid].append(ts.timestamp())
        elif _num(ts) is not None:
            times[sid].append(float(ts))

    out: Dict[str, dict] = {}
    for sid, vals in rs.items():
        n = len(vals)
        span_days = None
        if len(times[sid]) >= 2:
            span_days = max(1e-9, (max(times[sid]) - min(times[sid])) / 86400.0)
        out[sid] = {
            "n_trades": n,
            "sd_r": round(statistics.stdev(vals), 4) if n >= 2 else None,
            "mean_r": round(statistics.fmean(vals), 4),
            "span_days": None if span_days is None else round(span_days, 1),
            "trades_per_day": None if not span_days else round(n / span_days, 3),
        }
    return out


def recommend(stats: Dict[str, dict], global_min_trades: int = 30,
              global_block_below_r: float = -0.3) -> Dict[str, dict]:
    """Turn observed behaviour into a per-strategy bar. Returns {strategy: {...recommendation}}."""
    rates = [s["trades_per_day"] for s in stats.values() if s.get("trades_per_day")]
    sds = [s["sd_r"] for s in stats.values() if s.get("sd_r")]
    busiest = max(rates) if rates else None
    median_sd = statistics.median(sds) if sds else None

    out: Dict[str, dict] = {}
    for sid, s in stats.items():
        rec: Dict[str, object] = {"observed": s}
        why: List[str] = []

        if s["n_trades"] < MIN_TRADES_FOR_STATS:
            rec.update(min_trades=None, block_below_r=None, confidence="NONE")
            why.append(f"only {s['n_trades']} live trades - not enough to set anything from; "
                       f"leave this strategy on the global default until it has traded more")
            rec["why"] = why
            out[sid] = rec
            continue

        # --- how many trades before this strategy may be judged ---
        if busiest and s.get("trades_per_day"):
            share = s["trades_per_day"] / busiest
            scaled = int(math.ceil(global_min_trades * share))
            mt = max(MIN_SENSIBLE_TRADES, min(global_min_trades, scaled))
            why.append(f"trades {share:.2f}x as often as the busiest strategy, so {mt} trades here is "
                       f"about as much calendar time as {global_min_trades} there")
        else:
            mt = global_min_trades
            why.append("no usable timestamps - kept the global trade count")
        rec["min_trades"] = mt

        # --- how bad is bad enough to call it no-edge ---
        if median_sd and s.get("sd_r"):
            ratio = s["sd_r"] / median_sd
            bb = round(global_block_below_r * max(0.4, min(2.5, ratio)), 3)
            why.append(f"outcomes swing {ratio:.2f}x as wide as the median strategy, so the no-edge line "
                       f"sits at {bb:+.2f}R rather than {global_block_below_r:+.2f}R")
        else:
            bb = global_block_below_r
            why.append("not enough trades to measure dispersion - kept the global limit")
        rec["block_below_r"] = bb

        rec["confidence"] = ("LOW" if s["n_trades"] < 30 else
                             "MEDIUM" if s["n_trades"] < 100 else "HIGH")
        rec["why"] = why
        out[sid] = rec
    return out


def render(recs: Dict[str, dict], global_min_trades: int, global_block_below_r: float) -> str:
    lines: List[str] = []
    lines.append(f"Global defaults in use: min_trades={global_min_trades}, "
                 f"block_below_r={global_block_below_r:+.2f}R")
    lines.append("")
    lines.append(f"  {'strategy':<22}{'trades':>7}{'/day':>7}{'sd R':>8}"
                 f"{'min_trades':>12}{'block below':>13}  confidence")
    for sid, r in sorted(recs.items()):
        o = r["observed"]
        mt = "-" if r.get("min_trades") is None else str(r["min_trades"])
        bb = "-" if r.get("block_below_r") is None else f"{r['block_below_r']:+.2f}R"
        lines.append(f"  {sid:<22}{o['n_trades']:>7}"
                     f"{(o.get('trades_per_day') or 0):>7.2f}"
                     f"{(o.get('sd_r') or 0):>8.2f}{mt:>12}{bb:>13}  {r['confidence']}")

    lines.append("")
    lines.append("Reasoning, per strategy:")
    for sid, r in sorted(recs.items()):
        lines.append(f"  {sid}")
        for w in r["why"]:
            lines.append(f"      - {w}")

    usable = {s: r for s, r in recs.items() if r.get("min_trades") is not None}
    lines.append("")
    if not usable:
        lines.append("Nothing to paste yet: no strategy has enough live trades to set its own bar.")
        lines.append("Every strategy stays on the global default, which is the correct state for now.")
        return "\n".join(lines)

    lines.append("Paste into config.yaml under research_lab (review each line first):")
    lines.append("")
    lines.append("  gate_overrides:")
    for sid, r in sorted(usable.items()):
        lines.append(f"    {sid}:")
        lines.append(f"      min_trades: {r['min_trades']}")
        lines.append(f"      block_below_r: {r['block_below_r']}")
        lines.append(f"      note: \"{r['why'][0][:90]}\"")
    lines.append("")
    lines.append("LOW confidence means the recommendation is itself built on a small sample.")
    lines.append("A threshold is a judgement about a strategy; do not adopt one you cannot justify.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from app.config import ConfigManager, load_secrets
    from app.storage.repo import PostgresRepo

    cfgm = ConfigManager()
    lab = cfgm.app.research_lab
    repo = PostgresRepo(load_secrets().database_url)
    stats = collect(repo.select("trades") or [])
    recs = recommend(stats, lab.gate_min_trades, lab.gate_block_below_r)
    print(json.dumps(recs, indent=2, default=str) if args.json
          else render(recs, lab.gate_min_trades, lab.gate_block_below_r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
