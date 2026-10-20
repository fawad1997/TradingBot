#!/usr/bin/env python3
"""Did deploying a new build change anything measurable? Before versus after, from your own data.

Written because "it got worse after the changes" is a claim that cannot be settled by looking at a day
of results. A losing day proves nothing about a system whose whole record is consistent with having no
edge: you would see losing days at exactly this rate if nothing had changed at all. The only way to
answer it is to compare equal windows either side of the deploy and ask whether the difference is bigger
than the noise.

It will usually answer NOT CONCLUSIVE, and that is the honest answer rather than a failure of the tool.
Two days of trades on each side cannot distinguish a real change from an ordinary run of luck, and a
tool that pretended otherwise would be worse than none.

WHAT IT CHECKS

  1. Results          net R, per-trade expectancy and win rate, each side, with the spread of outcomes.
                      A difference is only called real when it clears the combined standard error.

  2. Spread gate      the one change in the October 2026 build that could loosen trading: the adaptive
                      spread ceiling now expires after `adaptive.max_spread_cap_age_minutes`. If it had
                      been stuck tight, trades it was blocking are now admitted - at wider spreads. The
                      signature is SPREAD rejections falling AND accepted spreads rising together.
                      Either alone is ordinary market variation.

  3. Adaptive health  that change can only bite after several CONSECUTIVE failed refreshes (the refresh
                      runs hourly; the cap expires at three hours). If refreshes were succeeding, the
                      change did nothing whatsoever and the spread gate is not a candidate at all.

    python tools/deploy_impact.py --since "2026-10-18T20:00:00+05:00"
    python tools/deploy_impact.py --since "2026-10-18T20:00" --json
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
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


def _stats(rs: Sequence[float]) -> Dict:
    rs = [r for r in rs if r is not None and math.isfinite(r)]
    n = len(rs)
    if not n:
        return {"n": 0, "total_r": 0.0, "mean_r": None, "sd": None, "se": None, "win_rate": None}
    mean = statistics.fmean(rs)
    sd = statistics.stdev(rs) if n > 1 else 0.0
    return {"n": n, "total_r": round(sum(rs), 3), "mean_r": round(mean, 4),
            "sd": round(sd, 4), "se": round(sd / math.sqrt(n), 4) if n else None,
            "win_rate": round(sum(1 for r in rs if r > 0) / n, 3)}


def compare_means(a: Dict, b: Dict) -> Dict:
    """Is the difference between two windows bigger than the noise in them?

    Welch's standard error on the difference. Deliberately conservative: anything inside two standard
    errors is reported as NOT CONCLUSIVE, because at these sample sizes it genuinely is not."""
    if not a["n"] or not b["n"] or a["mean_r"] is None or b["mean_r"] is None:
        return {"verdict": "NO_DATA", "difference_r": None}
    diff = b["mean_r"] - a["mean_r"]
    se = math.sqrt((a["se"] or 0) ** 2 + (b["se"] or 0) ** 2)
    if se <= 0:
        return {"verdict": "NOT_CONCLUSIVE", "difference_r": round(diff, 4), "se": 0.0}
    z = diff / se
    if a["n"] < 20 or b["n"] < 20:
        verdict = "NOT_CONCLUSIVE"
        note = f"only {a['n']} and {b['n']} trades - far too few to tell a change from a run of luck"
    elif abs(z) < 2:
        verdict = "NOT_CONCLUSIVE"
        note = "the difference is inside the noise of these samples"
    else:
        verdict = "WORSE_AFTER" if diff < 0 else "BETTER_AFTER"
        note = "the difference clears two standard errors - worth investigating, not yet a cause"
    return {"verdict": verdict, "difference_r": round(diff, 4), "se": round(se, 4),
            "z": round(z, 2), "note": note}


def build(trades: Sequence[dict], rejections: Sequence[dict], adaptive: Sequence[dict],
          since: datetime, window: Optional[timedelta] = None) -> Dict:
    now = datetime.now(timezone.utc)
    after_len = now - since
    window = window or after_len
    before_start = since - window

    def split(rows, field):
        before, after = [], []
        for r in rows:
            t = _when(r.get(field))
            if t is None:
                continue
            if before_start <= t < since:
                before.append(r)
            elif t >= since:
                after.append(r)
        return before, after

    tb, ta = split([t for t in trades if str(t.get("state") or "").upper() in CLOSED], "closed_at")
    rb, ra = split(rejections, "created_at")
    ab, aa = split(adaptive, "created_at")

    sb = _stats([_num(t.get("r_multiple")) for t in tb])
    sa = _stats([_num(t.get("r_multiple")) for t in ta])

    def gate_mix(rows):
        c = Counter(str(r.get("gate") or "?") for r in rows)
        return {k: v for k, v in c.most_common()}

    def spread_median(rows):
        vals = [_num(r.get("spread_points")) for r in rows]
        vals = [v for v in vals if v is not None and v > 0]
        return round(statistics.median(vals), 1) if vals else None

    # Accepted spreads: a rejection row carries the spread at the moment of the decision, so the
    # NON-spread rejections are a usable proxy for "the spread when a signal was not refused for it".
    acc_b = spread_median([r for r in rb if str(r.get("gate") or "").upper() != "SPREAD"])
    acc_a = spread_median([r for r in ra if str(r.get("gate") or "").upper() != "SPREAD"])

    gb, ga = gate_mix(rb), gate_mix(ra)
    spread_rej_b = gb.get("SPREAD", 0) / len(rb) if rb else None
    spread_rej_a = ga.get("SPREAD", 0) / len(ra) if ra else None

    # Adaptive health - the decisive check for whether the cap-expiry change could matter at all.
    def adaptive_health(rows):
        total = len(rows)
        failed = sum(1 for r in rows if not r.get("updated"))
        per_symbol_runs = defaultdict(list)
        for r in sorted(rows, key=lambda x: _when(x.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)):
            per_symbol_runs[r.get("symbol")].append(bool(r.get("updated")))
        worst = 0
        for sym, seq in per_symbol_runs.items():
            run = 0
            for ok in seq:
                run = 0 if ok else run + 1
                worst = max(worst, run)
        reasons = Counter(str(r.get("reason") or "?") for r in rows if not r.get("updated"))
        return {"cycles": total, "failed": failed,
                "failure_rate": round(failed / total, 3) if total else None,
                "worst_consecutive_failures": worst,
                "failure_reasons": dict(reasons.most_common(3))}

    hb, ha = adaptive_health(ab), adaptive_health(aa)
    # Three consecutive failures (hourly refresh, three-hour expiry) before the change does anything.
    cap_could_matter = max(hb["worst_consecutive_failures"], ha["worst_consecutive_failures"]) >= 3

    return {
        "since": since.isoformat(), "window_hours": round(window.total_seconds() / 3600, 1),
        "results": {"before": sb, "after": sa, "comparison": compare_means(sb, sa)},
        "rejections": {
            "before_total": len(rb), "after_total": len(ra),
            "before_by_gate": gb, "after_by_gate": ga,
            "spread_share_before": None if spread_rej_b is None else round(spread_rej_b, 3),
            "spread_share_after": None if spread_rej_a is None else round(spread_rej_a, 3),
            "accepted_spread_median_before": acc_b,
            "accepted_spread_median_after": acc_a,
        },
        "adaptive": {"before": hb, "after": ha, "cap_expiry_could_have_mattered": cap_could_matter},
    }


def render(d: Dict) -> str:
    o: List[str] = []
    r = d["results"]
    b, a, c = r["before"], r["after"], r["comparison"]
    o.append(f"Comparing the {d['window_hours']:.0f}h before {d['since']} with everything since.")
    o.append("")
    o.append("RESULTS")
    o.append(f"  {'':<10}{'trades':>8}{'net R':>10}{'per trade':>12}{'win rate':>10}")
    for label, s in (("before", b), ("after", a)):
        o.append(f"  {label:<10}{s['n']:>8}{s['total_r']:>10.2f}"
                 f"{(s['mean_r'] if s['mean_r'] is not None else 0):>+12.4f}"
                 f"{((s['win_rate'] or 0) * 100):>9.1f}%")
    o.append("")
    o.append(f"  verdict: {c['verdict']}")
    if c.get("note"):
        o.append(f"           {c['note']}")
    if c["verdict"] == "NOT_CONCLUSIVE":
        o.append("           A losing day is not evidence. You would see days like it at this rate")
        o.append("           even if nothing had changed, because the record has never shown an edge.")

    rj = d["rejections"]
    o.append("")
    o.append("DID THE SPREAD GATE LOOSEN?")
    o.append(f"  refusals for spread   {('-' if rj['spread_share_before'] is None else f'{rj_pct(rj, True)}%'):>8} before"
             f"   ->   {('-' if rj['spread_share_after'] is None else f'{rj_pct(rj, False)}%'):>7} after")
    o.append(f"  spread when accepted  {str(rj['accepted_spread_median_before'] or '-'):>8} pts before"
             f"   ->   {str(rj['accepted_spread_median_after'] or '-'):>7} pts after")
    loosened = (rj["spread_share_before"] is not None and rj["spread_share_after"] is not None
                and rj["spread_share_after"] < rj["spread_share_before"]
                and (rj["accepted_spread_median_after"] or 0) > (rj["accepted_spread_median_before"] or 0))
    o.append(f"  -> {'BOTH moved the way a looser gate would move them - investigate' if loosened else 'no combined signature of a looser gate'}")

    ad = d["adaptive"]
    o.append("")
    o.append("COULD THE SPREAD-CAP CHANGE HAVE MATTERED AT ALL?")
    for label, h in (("before", ad["before"]), ("after", ad["after"])):
        o.append(f"  {label:<7} {h['cycles']:>4} refreshes, {h['failed']} failed, "
                 f"worst run of consecutive failures: {h['worst_consecutive_failures']}")
        if h["failure_reasons"]:
            o.append(f"          reasons: {h['failure_reasons']}")
    if not ad["cap_expiry_could_have_mattered"]:
        o.append("  -> NO. The cap expires after three hours and the refresh runs hourly, so it takes")
        o.append("     three consecutive failures before the change alters anything. That never happened,")
        o.append("     which means the October build did not touch your spread gate at all.")
    else:
        o.append("  -> POSSIBLY. There was a long enough run of failed refreshes for a stale cap to have")
        o.append("     existed. Read the spread rows above before concluding anything.")
    return "\n".join(o)


def rj_pct(rj: Dict, before: bool) -> str:
    v = rj["spread_share_before"] if before else rj["spread_share_after"]
    return f"{v * 100:.0f}" if v is not None else "-"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", required=True, help="when you deployed, ISO format")
    ap.add_argument("--window-hours", type=float, default=None,
                    help="how much history to use as the 'before' window (default: same length as 'after')")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    since = _when(args.since)
    if since is None:
        print(f"could not read --since {args.since!r}; use e.g. 2026-10-18T20:00:00+05:00")
        return 2

    from app.config import load_secrets
    from app.storage.repo import PostgresRepo
    repo = PostgresRepo(load_secrets().database_url)
    d = build(repo.select("trades") or [],
              repo.select("rejections", order_by="id", desc=True, limit=20000) or [],
              repo.select("adaptive_param_history", order_by="id", desc=True, limit=20000) or [],
              since,
              timedelta(hours=args.window_hours) if args.window_hours else None)
    print(json.dumps(d, indent=2, default=str) if args.json else render(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
