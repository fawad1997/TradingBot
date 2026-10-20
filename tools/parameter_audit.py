#!/usr/bin/env python3
"""How many free parameters does each strategy carry, and is there enough data to justify them?

Chan ch.3 gives two rules of thumb:

  - No more than FIVE free parameters per strategy, counting entry and exit thresholds, holding
    periods and lookback windows.
  - Roughly 252 data points per parameter before the fit means anything.

A strategy with twelve tuned parameters is overfit by construction, whatever a walk-forward says about
it, because the walk-forward itself is choosing among too many degrees of freedom relative to the data.

This reads strategies.yaml and counts. It makes no judgement about whether any individual parameter is
sensible - only whether there are too many of them to be supported.

    python tools/parameter_audit.py
    python tools/parameter_audit.py --bars 100000 --json

Read only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MAX_FREE_PARAMETERS = 5
BARS_PER_PARAMETER = 252

# Keys that configure plumbing rather than the trading rule. These are not free parameters in Chan's
# sense: they are not tuned to improve backtest performance.
NOT_FREE = {
    "enabled", "id", "version", "status", "timeframe", "timeframes", "symbols", "symbol",
    "risk_multiplier", "signal_ttl_sec", "description", "notes", "name", "class", "sessions",
    "regimes", "max_concurrent", "exit_model", "stats", "evidence",
}


def _is_free(v) -> bool:
    """Is this value a parameter that can be TUNED, as opposed to a fixed setting?

    The config declares its own answer. A ParamSpec carries `minimum` and `maximum`; when both are
    present the optimiser is allowed to search that range, and that is exactly Chan's definition of a
    free parameter. A ParamSpec with no range is a fixed choice and costs no degrees of freedom.

    Using the config's own declaration rather than guessing from the Python type matters: every value
    in strategies.yaml arrives as a ParamSpec, so a type check sees no numbers at all and reports one
    parameter per strategy, which is both wrong and reassuring - the worst combination.
    """
    lo, hi = getattr(v, "minimum", None), getattr(v, "maximum", None)
    if lo is not None and hi is not None:
        return True
    if isinstance(v, bool):
        return False                            # a switch is a choice, not a tuned number
    return isinstance(v, (int, float))


def count_parameters(params: Any, prefix: str = "") -> List[str]:
    """Flatten a parameter block into dotted names of the TUNABLE values, skipping plumbing keys."""
    out: List[str] = []
    if isinstance(params, dict):
        for k, v in params.items():
            if k in NOT_FREE:
                continue
            name = f"{prefix}{k}"
            if _is_free(v):
                out.append(name)
            elif isinstance(v, (dict, list)):
                out.extend(count_parameters(v, f"{name}."))
    elif isinstance(params, list):
        for i, v in enumerate(params):
            out.extend(count_parameters(v, f"{prefix}{i}."))
    return out


def audit(strategies: List[dict], bars_available: int = 0) -> Dict[str, Any]:
    rows = []
    for s in strategies:
        sid = str(s.get("id") or s.get("name") or "?")
        names = count_parameters(s.get("params") or {})
        exit_names = count_parameters(s.get("exit_model") or {}, "exit.")
        total = len(names) + len(exit_names)
        needed = total * BARS_PER_PARAMETER
        rows.append({
            "strategy": sid,
            "status": str(s.get("status") or "?"),
            "free_parameters": total,
            "entry_parameters": len(names),
            "exit_parameters": len(exit_names),
            "over_budget": total > MAX_FREE_PARAMETERS,
            "bars_needed": needed,
            "bars_available": bars_available,
            "enough_data": (bars_available >= needed) if bars_available else None,
            "parameters": sorted(names + exit_names),
        })
    rows.sort(key=lambda r: -r["free_parameters"])
    over = [r["strategy"] for r in rows if r["over_budget"]]
    return {
        "max_free_parameters": MAX_FREE_PARAMETERS,
        "bars_per_parameter": BARS_PER_PARAMETER,
        "strategies": rows,
        "over_budget": over,
        "verdict": ("All strategies are within the five-parameter budget."
                    if not over else
                    f"Over budget: {', '.join(over)}. More parameters than the data can support means the "
                    f"walk-forward is choosing among too many degrees of freedom."),
    }


def render(d: Dict[str, Any]) -> str:
    out = [d["verdict"], "",
           f"  {'strategy':<22} {'status':<14} {'params':>7} {'bars needed':>12} {'enough?':>9}"]
    for r in d["strategies"]:
        mark = "!!!" if r["over_budget"] else " ok"
        enough = "-" if r["enough_data"] is None else ("yes" if r["enough_data"] else "NO")
        out.append(f"  {mark} {r['strategy']:<18} {r['status']:<14} {r['free_parameters']:>7} "
                   f"{r['bars_needed']:>12} {enough:>9}")
    out.append("")
    out.append("  Chan ch.3: at most five free parameters, and about 252 bars of data per parameter.")
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description="Count free parameters per strategy (Chan ch.3).")
    p.add_argument("--bars", type=int, default=0, help="bars of history available, to test the data rule")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    from app.config import ConfigManager
    cfgm = ConfigManager()
    strategies = []
    for s in cfgm.strategies.strategies:
        strategies.append({"id": s.id, "status": s.status, "params": dict(s.params or {}),
                           "exit_model": dict(s.exit_model or {})})
    d = audit(strategies, a.bars)
    print(json.dumps(d, indent=2, default=str) if a.json else render(d))
    return 1 if d["over_budget"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
