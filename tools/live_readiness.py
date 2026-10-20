#!/usr/bin/env python3
"""Is there enough evidence to risk real money yet? Measured, not judged.

The question "should I go live" normally gets answered by how the last few days felt. This answers it
from the record instead, and it is deliberately hard to pass.

Eleven checks run against the database. Each one prints the actual number next to the number it has to
beat, so a failure tells you how far off you are rather than just saying no. The exit code is 0 only when
every automatic check passes, so this drops into a scheduled task and stays quiet until something changes.

    python tools/live_readiness.py
    python tools/live_readiness.py --since 2026-10-17      # only trades recorded after the peak fix
    python tools/live_readiness.py --min-trades 200 --min-edge 0.05
    python tools/live_readiness.py --json

WHY THE DEFAULTS ARE WHAT THEY ARE

--min-trades 200
    At a per-trade standard deviation around 0.8R, distinguishing a true +0.10R edge from zero at 80%
    power needs roughly 500 trades; a +0.15R edge needs about 220. 200 is the point where a real edge
    of a size that survives live costs becomes visible. Below it the honest answer to "does this work"
    is "nobody can tell", whatever the running total says.

--min-edge 0.05
    The lower end of the confidence interval must clear +0.05R, not merely zero. Live spreads run wider
    than demo spreads, especially around news and rollover, and the spread gate already permits a trade
    to start 10% of its stop underwater. 0.05R is the haircut that difference is worth. An edge whose
    error bar reaches down to zero is an edge that live costs can erase entirely.

--since
    Trades closed before the peak-persistence fix carry recorded extremes that could only understate,
    and the cooldown fix changed which stop moves were even attempted. Anything from before those
    landed describes a system that no longer exists. Default is 2026-10-17.

WHAT IT CANNOT CHECK

Four things decide this that no database knows: whether live fills match demo fills, whether the
commission configured matches the account you will actually open, whether you can afford to lose the
deposit, and whether you will leave it alone. They are printed as manual confirmations at the end and
they are not scored. The tool refuses to call you ready; it can only tell you when you are definitely
not.

This reads and never writes. A test walks the module's call graph and fails if that ever changes.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_SINCE = "2026-10-17"
DEFAULT_MIN_TRADES = 200
DEFAULT_MIN_EDGE = 0.05
MIN_SLIPPAGE_SAMPLES = 20          # matches cost.slippage_sample_min
Z = 1.96                           # 95%


# ----------------------------------------------------------------------------- small helpers
def _f(v) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _when(v) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _stats(rs: List[float]) -> Dict[str, float]:
    """Mean, sample standard deviation, standard error and the 95% interval."""
    n = len(rs)
    if n == 0:
        return {"n": 0, "mean": 0.0, "sd": 0.0, "se": 0.0, "lo": 0.0, "hi": 0.0}
    mean = sum(rs) / n
    if n < 2:
        return {"n": n, "mean": mean, "sd": 0.0, "se": 0.0, "lo": mean, "hi": mean}
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1))
    se = sd / math.sqrt(n)
    return {"n": n, "mean": mean, "sd": sd, "se": se, "lo": mean - Z * se, "hi": mean + Z * se}


def _check(name: str, passed: bool, got: str, needs: str, why: str = "") -> Dict[str, Any]:
    return {"name": name, "pass": bool(passed), "got": got, "needs": needs, "why": why}


def _config_checks(cfgm=None) -> List[Dict[str, Any]]:
    """Settings that are reasonable on demo and unacceptable on a funded account.

    Kept separate from the trade-record checks because these are decisions, not measurements: nothing has to
    be collected, the operator simply has to have made the choice. Each one is a finding from the October 2026
    external review that no test or startup check would otherwise have caught.

    `cfgm` defaults to the live configuration; tests pass their own so that a fixture representing a good
    trade record is not forced to also carry a full config."""
    if cfgm is None:
        try:
            from app.config import ConfigManager
            cfgm = ConfigManager()
        except Exception as exc:                                       # noqa: BLE001
            return [_check("Configuration checks", False, f"config not loadable: {type(exc).__name__}",
                           "loadable", "These checks cannot run without the live configuration.")]

    lab, straddle, cost = cfgm.app.research_lab, cfgm.app.news_straddle, cfgm.app.cost
    out = [_check(
        "Research gate fails closed", bool(getattr(lab, "gate_require_evidence", False)),
        f"gate_require_evidence={getattr(lab, 'gate_require_evidence', False)}", "true",
        "With it off, a missing, stale, incomplete or thin research result PERMITS the trade. The gate only "
        "vetoes a proven absence of edge, so an unresearched symbol trades freely. On real money the safe "
        "answer to 'I don't know' is 'don't trade'.")]

    binds = straddle.ceiling_binds()
    out.append(_check(
        "News straddle ceiling binds", (not straddle.enabled) or binds,
        f"max_armed_symbols={straddle.max_armed_symbols} vs {len(straddle.symbols)} symbols",
        "ceiling below the symbol count",
        "A ceiling equal to the number of configured symbols refuses nothing. Either lower it so it is a real "
        "limit, or stop describing it as one."))

    commission_set = bool(cost.commission_per_lot_round_trip) or bool(cost.symbol_commission_per_lot_round_trip)
    out.append(_check(
        "Commission matches the account type", commission_set,
        f"commission_per_lot_round_trip={cost.commission_per_lot_round_trip}, "
        f"{len(cost.symbol_commission_per_lot_round_trip)} per-symbol overrides",
        "non-zero, or a deliberate Standard-account zero",
        "Zero is correct on Exness Standard and wrong on Raw Spread / Zero, where every backtest and every "
        "cost-gate decision understates the true cost. Confirm against the live account specification."))
    return out


# ----------------------------------------------------------------------------- the assessment
def assess(repo, now: Optional[datetime] = None, since: str = DEFAULT_SINCE,
           min_trades: int = DEFAULT_MIN_TRADES, min_edge: float = DEFAULT_MIN_EDGE,
           cfgm=None) -> Dict[str, Any]:
    """Run every automatic check. Returns the checks plus the numbers behind them."""
    now = now or datetime.now(timezone.utc)
    cutoff = _when(since) or _when(DEFAULT_SINCE)

    def rows(table: str) -> List[dict]:
        try:
            return list(repo.select(table) or [])
        except Exception:
            return []

    # --- the trade record, after the cutoff, closed only
    trades = []
    for t in rows("trades"):
        closed = _when(t.get("closed_at"))
        r = _f(t.get("r_multiple"))
        if closed is None or r is None or (cutoff and closed < cutoff):
            continue
        trades.append({**t, "_closed": closed, "_r": r})
    trades.sort(key=lambda t: t["_closed"])
    rs = [t["_r"] for t in trades]
    overall = _stats(rs)

    checks: List[Dict[str, Any]] = []

    # 1 -------------------------------------------------------------- enough trades to say anything
    checks.append(_check(
        "Enough finished trades", len(trades) >= min_trades,
        f"{len(trades)}", f"{min_trades}",
        "Below this, a good system and a lucky one look identical."))

    # 2 -------------------------------------------------- the edge survives its own margin of error
    checks.append(_check(
        "Edge clears its error bar", len(trades) >= 2 and overall["lo"] > min_edge,
        f"{overall['lo']:+.3f}R" if len(trades) >= 2 else "n/a",
        f"> {min_edge:+.3f}R",
        "The LOW end of the 95% interval, not the average. Live costs eat the difference."))

    # 3 ------------------------------------------------------------ it did not all happen one week
    first, second = rs[:len(rs) // 2], rs[len(rs) // 2:]
    h1, h2 = _stats(first), _stats(second)
    both = len(rs) >= 4 and h1["mean"] > 0 and h2["mean"] > 0
    checks.append(_check(
        "Positive in both halves", both,
        f"{h1['mean']:+.3f}R then {h2['mean']:+.3f}R" if len(rs) >= 4 else "n/a",
        "both above zero",
        "Split down the middle by time. One lucky week carrying the total shows up here."))

    # 4 & 5 ----------------------------------------------- not carried by one symbol or one strategy
    for field, label in (("symbol", "symbol"), ("strategy_id", "strategy")):
        groups: Dict[str, List[float]] = defaultdict(list)
        for t in trades:
            groups[str(t.get(field) or "?")].append(t["_r"])
        if len(groups) < 2:
            checks.append(_check(
                f"Not carried by one {label}", False,
                f"only {len(groups)} {label}" + ("" if len(groups) == 1 else "s"), "2 or more",
                f"Cannot test concentration with a single {label} in the record."))
            continue
        best = max(groups, key=lambda k: sum(groups[k]))
        without = [r for k, g in groups.items() if k != best for r in g]
        w = _stats(without)
        checks.append(_check(
            f"Not carried by one {label}", len(without) > 0 and w["mean"] > 0,
            f"{w['mean']:+.3f}R without {best}", "above zero",
            f"Drop the best {label} entirely. What is left must still make money."))

    # 6 ------------------------------------------------------- slippage measured, not assumed
    fills: Dict[str, int] = defaultdict(int)
    for e in rows("execution_events"):
        if _f(e.get("slippage_points")) is None:
            continue
        if _when(e.get("created_at")) and cutoff and _when(e["created_at"]) < cutoff:
            continue
        fills[str(e.get("symbol") or "?")] += 1
    traded = {str(t.get("symbol") or "?") for t in trades}
    thin = sorted(s for s in traded if fills.get(s, 0) < MIN_SLIPPAGE_SAMPLES)
    checks.append(_check(
        "Slippage measured on every symbol", len(traded) > 0 and not thin,
        f"{len(traded) - len(thin)} of {len(traded)} symbols" if traded else "no trades",
        f"{MIN_SLIPPAGE_SAMPLES} fills each",
        ("Still assuming the default for: " + ", ".join(thin[:6])) if thin else
        "The cost gate is using observed fills rather than a configured guess."))

    # 7 ------------------------------------------------------------- books agree with the broker
    stuck = [t for t in rows("trades")
             if str(t.get("state") or "").upper() == "RECONCILIATION_REQUIRED"]
    checks.append(_check(
        "No unreconciled trades", not stuck,
        f"{len(stuck)} stuck", "0",
        "A trade our books and the broker disagree about makes every statistic above suspect."))

    # 8 ------------------------------------------------------------------ nothing appeared from nowhere
    orphans = [t for t in trades if not t.get("signal_id")]
    checks.append(_check(
        "Every trade traces to a signal", not orphans,
        f"{len(orphans)} orphaned", "0",
        "A position with no signal behind it means something opened a trade outside the pipeline."))

    # 9 ------------------------------------------------- the risk machinery actually ran, not just existed
    seen = {str(r.get("state") or "").upper() for r in rows("risk_states")}
    exercised = seen - {"NORMAL", ""}
    checks.append(_check(
        "Risk controls have fired", bool(exercised),
        ", ".join(sorted(exercised)) if exercised else "never left NORMAL",
        "at least one",
        "A limit that has never triggered is a limit nobody has tested."))

    # 10 ---------------------------------------------------------------------- long enough on the clock
    days = 0.0
    if trades:
        days = (trades[-1]["_closed"] - trades[0]["_closed"]).total_seconds() / 86400.0
    checks.append(_check(
        "Weeks of continuous running", days >= 21,
        f"{days:.1f} days", "21 days",
        "Enough calendar for a news week, a quiet week and a weekend gap to have happened."))

    # 11 ------------------------------------------------------------ a strategy the system itself trusts
    validated = [s for s in rows("strategy_versions")
                 if str(s.get("status") or "").upper() == "VALIDATED"]
    checks.append(_check(
        "A strategy reached VALIDATED", bool(validated),
        f"{len(validated)}", "1 or more",
        "Your own promotion rule requires an evidence package. Live start-up enforces it anyway."))

    # 12-14 ------------------------------------------- configuration that must change before real money
    # These came out of an outside review in Oct 2026. Each is a setting that is defensible on demo and
    # wrong on a funded account, and each was invisible until something asserted it.
    checks.extend(_config_checks(cfgm))

    passed = sum(1 for c in checks if c["pass"])
    return {
        "ready": passed == len(checks),
        "passed": passed,
        "total": len(checks),
        "checks": checks,
        "since": since,
        "trades": len(trades),
        "edge": overall,
        "halves": {"first": h1, "second": h2},
        "days": days,
        "trades_needed_for": _needed(overall["sd"] or 0.8),
    }


def _needed(sd: float) -> Dict[str, int]:
    """How many trades it would take to resolve edges of various sizes, at 80% power."""
    out = {}
    for edge in (0.05, 0.10, 0.15, 0.20, 0.30):
        out[f"{edge:+.2f}R"] = int(round(((Z + 0.84) * sd / edge) ** 2))
    return out


# ----------------------------------------------------------------------------- printing
MANUAL = [
    ("Live fills compared against demo fills",
     "Run the smallest possible real account beside the demo one on identical signals for a few weeks "
     "and compare slippage and spread. This is the only way to measure the execution gap; nothing in "
     "the database can tell you."),
    ("Commission set for the account you will actually open",
     "cost.commission_per_lot_round_trip is 0.0, which is correct for a Standard account and wrong for "
     "Raw or Zero. Set it per symbol before the first live order, not after."),
    ("The deposit is money you can lose entirely",
     "Size it so that the 30% emergency drawdown tier is an inconvenience rather than an event. If "
     "hitting it would change your month, the deposit is too large."),
    ("You have run the daily routine for weeks without skipping",
     "session check before, session review after. Going live should not also be the day you start a "
     "new habit."),
]


def render(d: Dict[str, Any]) -> str:
    out = []
    head = "READY on every measurable check." if d["ready"] else \
           f"NOT READY: {d['total'] - d['passed']} of {d['total']} checks still failing."
    out.append(head)
    out.append(f"{d['trades']} finished trades since {d['since']}, "
               f"{d['edge']['mean']:+.3f}R each "
               f"(95% interval {d['edge']['lo']:+.3f} to {d['edge']['hi']:+.3f})")
    out.append("")
    for c in d["checks"]:
        mark = " ok " if c["pass"] else "!!! "
        out.append(f"  {mark} {c['name']:<34} {c['got']:>26}   needs {c['needs']}")
        if not c["pass"] and c["why"]:
            out.append(f"       {c['why']}")
    out.append("")
    out.append("  Trades needed to resolve an edge of this size, at the spread your record shows:")
    for edge, n in d["trades_needed_for"].items():
        out.append(f"       {edge:>7}   {n:>6} trades")
    out.append("")
    out.append("  Not measurable here. Confirm each one yourself:")
    for title, body in MANUAL:
        out.append(f"     - {title}")
        for line in _wrap(body, 86):
            out.append(f"         {line}")
    return "\n".join(out)


def _wrap(text: str, width: int) -> List[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def main() -> int:
    p = argparse.ArgumentParser(description="Measure whether there is enough evidence to risk real money.")
    p.add_argument("--since", default=DEFAULT_SINCE, help="ignore trades closed before this date")
    p.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES)
    p.add_argument("--min-edge", type=float, default=DEFAULT_MIN_EDGE)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    from app.config import load_secrets
    from app.storage.repo import PostgresRepo

    repo = PostgresRepo(load_secrets().database_url)
    d = assess(repo, since=a.since, min_trades=a.min_trades, min_edge=a.min_edge)
    print(json.dumps(d, indent=2, default=str) if a.json else render(d))
    return 0 if d["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
