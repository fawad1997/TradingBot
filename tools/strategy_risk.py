#!/usr/bin/env python3
"""What Sharpe ratio does the live record actually imply, and how fragile is it?

Lopez de Prado ch.15 gives a closed form for the annualised Sharpe ratio of a strategy with asymmetric
payouts, from three numbers this system already records: win rate, average win, average loss.

    theta = ((pi_plus - pi_minus) * p + pi_minus) / ((pi_plus - pi_minus) * sqrt(p(1-p))) * sqrt(n)

That turns "23 trades is not enough" from an opinion into arithmetic. On the 16-17 Oct record - 78.3%
precision, +0.672R wins, -1.0R losses - the implied annual Sharpe at 3,000 trades per year is about 24.
Medallion runs around 2.5. A Sharpe of 24 is not a result; it is a measurement error, and the formula
is what makes that quantitative rather than a matter of taste.

The sensitivity table is the part worth acting on. Precision enters non-linearly while frequency enters
as sqrt(n), so trade QUALITY dominates trade COUNT mathematically: doubling the number of trades
multiplies Sharpe by 1.41, while precision slipping from 78% to 70% halves it.

    python tools/strategy_risk.py
    python tools/strategy_risk.py --since 2026-10-17 --trades-per-year 3000
    python tools/strategy_risk.py --json

Read only.
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

Z_POWER = 0.84          # 80% power
Z_ALPHA = 1.96          # 5% significance
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


def sharpe(p: float, n: float, pi_minus: float, pi_plus: float) -> Optional[float]:
    """Annualised Sharpe for n bets a year at precision p with the given payouts."""
    if not (0 < p < 1) or n <= 0 or pi_plus <= pi_minus:
        return None
    num = (pi_plus - pi_minus) * p + pi_minus
    den = (pi_plus - pi_minus) * math.sqrt(p * (1 - p))
    if den == 0:
        return None
    return num / den * math.sqrt(n)


def implied_precision(sl: float, pt: float, freq: float, target_sharpe: float) -> Optional[float]:
    """The precision needed to hit a target Sharpe, given the payouts and bet frequency (ch.15)."""
    a = (freq + target_sharpe ** 2) * (pt - sl) ** 2
    b = (2 * freq * sl - target_sharpe ** 2 * (pt - sl)) * (pt - sl)
    c = freq * sl ** 2
    disc = b * b - 4 * a * c
    if a == 0 or disc < 0:
        return None
    return (-b + math.sqrt(disc)) / (2.0 * a)


def breakeven_precision(pi_minus: float, pi_plus: float) -> Optional[float]:
    """Below this win rate the strategy loses money at these payouts, whatever the frequency."""
    if pi_plus <= pi_minus:
        return None
    return -pi_minus / (pi_plus - pi_minus)


def trades_needed(sd: float, edge: float) -> Optional[int]:
    """Trades to distinguish a true edge of this size from zero, at 80% power and 5% significance."""
    if edge <= 0 or sd <= 0:
        return None
    return int(round(((Z_ALPHA + Z_POWER) * sd / edge) ** 2))


def assess(trades: List[dict], trades_per_year: Optional[float] = None) -> Dict[str, Any]:
    rs = [r for r in (_f(t.get("r_multiple")) for t in trades) if r is not None]
    n = len(rs)
    if n == 0:
        return {"trades": 0, "note": "no finished trades in the window"}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    p = len(wins) / n
    pi_plus = sum(wins) / len(wins) if wins else 0.0
    pi_minus = sum(losses) / len(losses) if losses else -1.0
    mean = sum(rs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) if n > 1 else 0.0

    # Observed trade rate, annualised, unless overridden
    span_days = None
    whens = sorted(w for w in (_when(t.get("closed_at")) for t in trades) if w is not None)
    if len(whens) >= 2:
        span_days = (whens[-1] - whens[0]).total_seconds() / 86400.0
    if trades_per_year is None:
        trades_per_year = (n / span_days * 365.0) if span_days and span_days > 0 else 252.0

    be = breakeven_precision(pi_minus, pi_plus)
    by_freq = []
    for f in (250, 500, 1000, 2000, 3000):
        by_freq.append({"trades_per_year": f, "sharpe": round(sharpe(p, f, pi_minus, pi_plus) or 0.0, 2)})
    sensitivity = []
    for pp in (p, 0.75, 0.70, 0.65, be if be else 0.60, 0.55):
        if pp is None or not (0 < pp < 1):
            continue
        s = sharpe(pp, trades_per_year, pi_minus, pi_plus)
        sensitivity.append({"precision": round(pp, 4), "sharpe": round(s, 2) if s is not None else None})

    implied = sharpe(p, trades_per_year, pi_minus, pi_plus)
    return {
        "trades": n,
        "span_days": round(span_days, 1) if span_days else None,
        "precision": round(p, 4),
        "avg_win_r": round(pi_plus, 4),
        "avg_loss_r": round(pi_minus, 4),
        "expectancy_r": round(mean, 4),
        "sd_r": round(sd, 4),
        "trades_per_year_assumed": round(trades_per_year, 0),
        "implied_annual_sharpe": round(implied, 2) if implied is not None else None,
        "breakeven_precision": round(be, 4) if be else None,
        "margin_above_breakeven": round(p - be, 4) if be else None,
        "sharpe_by_frequency": by_freq,
        "sensitivity_to_precision": sensitivity,
        "trades_needed_to_prove": {f"{e:+.2f}R": trades_needed(sd or 0.8, e)
                                   for e in (0.05, 0.10, 0.15, 0.20, 0.30)},
        "plausibility": _plausibility(implied),
    }


def _plausibility(s: Optional[float]) -> str:
    if s is None:
        return "Not computable."
    if s > 5:
        return (f"An implied Sharpe of {s:.1f} is far beyond anything that has ever been sustained "
                "(Medallion is around 2.5). This is a sample too small to measure, not a discovery.")
    if s > 3:
        return f"An implied Sharpe of {s:.1f} is exceptional and should be treated as unproven until the sample is large."
    if s > 1:
        return f"An implied Sharpe of {s:.1f} is plausible for a real edge. Worth continuing to measure."
    if s > 0:
        return f"An implied Sharpe of {s:.1f} is weak. Live costs could erase it."
    return "Negative. The payouts and precision do not combine into a profitable strategy."


def render(d: Dict[str, Any]) -> str:
    if not d.get("trades"):
        return "No finished trades in the window."
    out = [f"{d['trades']} trades over {d['span_days'] or '?'} days: "
           f"{d['precision']:.1%} precision, +{d['avg_win_r']:.3f}R wins, {d['avg_loss_r']:.3f}R losses",
           f"expectancy {d['expectancy_r']:+.3f}R per trade", ""]
    out.append(f"  Implied annual Sharpe at {d['trades_per_year_assumed']:.0f} trades/year: "
               f"{d['implied_annual_sharpe']}")
    out.append(f"  {d['plausibility']}")
    out.append("")
    out.append("  By trade frequency:")
    for row in d["sharpe_by_frequency"]:
        out.append(f"       {row['trades_per_year']:>6} /yr   Sharpe {row['sharpe']:>7.2f}")
    out.append("")
    out.append(f"  Break-even precision at these payouts: {d['breakeven_precision']:.1%} "
               f"(currently {d['margin_above_breakeven']:+.1%} above it)")
    out.append("")
    out.append("  How fast it falls apart if precision slips:")
    for row in d["sensitivity_to_precision"]:
        out.append(f"       {row['precision']:>7.1%}   Sharpe {row['sharpe']:>7.2f}")
    out.append("")
    out.append("  Trades needed to prove an edge of each size (80% power):")
    for k, v in d["trades_needed_to_prove"].items():
        out.append(f"       {k:>7}   {v:>6} trades")
    out.append("")
    out.append("  Frequency enters as sqrt(n); precision does not. Doubling trade count multiplies")
    out.append("  Sharpe by 1.41. Trade quality dominates trade quantity.")
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description="Implied Sharpe and fragility of the live record (Lopez de Prado ch.15).")
    p.add_argument("--since", default=DEFAULT_SINCE)
    p.add_argument("--trades-per-year", type=float, default=None)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    from app.config import load_secrets
    from app.storage.repo import PostgresRepo
    repo = PostgresRepo(load_secrets().database_url)
    cutoff = _when(a.since)
    trades = [t for t in repo.select("trades") or []
              if _when(t.get("closed_at")) and (not cutoff or _when(t["closed_at"]) >= cutoff)]
    d = assess(trades, a.trades_per_year)
    print(json.dumps(d, indent=2, default=str) if a.json else render(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
