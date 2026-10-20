#!/usr/bin/env python3
"""Print the numbers the documentation claims, straight from the code that defines them.

An outside reviewer noticed the Field Manual said the startup sequence has "fourteen checkpoints" while
listing fifteen state names. Neither number was right: STARTUP_SEQUENCE has 13 entries and the StartupState
enum has 15 members, because STOPPED and FAILED are exits, not steps. A hand-counted number in prose drifts
from the code the moment the code changes, and nothing fails when it does.

This tool is the fix the reviewer asked for: generate the counts rather than type them. Run it whenever the
docs are touched, and read the output into the prose. `tests/unit/test_doc_facts.py` asserts the published
Field Manual agrees with what this prints, so the drift now breaks a test instead of surviving a review.

    python tools/doc_facts.py            # human-readable
    python tools/doc_facts.py --json     # machine-readable, for the test suite
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.types import STARTUP_SEQUENCE, StartupState, TradeState, RiskState   # noqa: E402
from app.research.lab.gate import Thresholds                                       # noqa: E402


def collect() -> dict:
    sequence = [str(s) for s in STARTUP_SEQUENCE]
    members = [str(s) for s in StartupState]
    terminal = [m for m in members if m not in sequence]

    facts = {
        "startup": {
            "checkpoints": len(sequence),
            "sequence": sequence,
            "enum_members": len(members),
            "terminal_states": terminal,
            "prose": (f"{len(sequence)} sequential checkpoints (BOOT through ACTIVE); the StartupState enum "
                      f"has {len(members)} members because {' and '.join(terminal)} are exits, not steps"),
        },
        "trade_states": {"count": len(list(TradeState)), "names": [str(s) for s in TradeState]},
        "risk_states": {"count": len(list(RiskState)), "names": [str(s) for s in RiskState]},
    }

    # Config-derived facts, best effort: the live config may not be loadable in every environment.
    try:
        from app.config import ConfigManager
        cfgm = ConfigManager()
        risk, straddle = cfgm.app.risk, cfgm.app.news_straddle
        facts["risk_limits"] = {
            "max_risk_fraction_per_trade": getattr(risk, "max_risk_fraction", None),
            "max_total_open_risk_fraction": getattr(risk, "max_total_open_risk_fraction", None),
            "max_concurrent_positions": getattr(risk, "max_concurrent_positions", None),
            "correlation_threshold": getattr(risk, "correlation_threshold", None),
            "correlation_group_fraction": getattr(risk, "correlation_group_fraction", None),
        }
        facts["news_straddle"] = {
            "enabled": straddle.enabled,
            "configured_symbols": len(straddle.symbols),
            "max_armed_symbols": straddle.max_armed_symbols,
            "ceiling_binds": straddle.ceiling_binds(),
        }
        facts["strategies"] = {
            "count": len(cfgm.strategies.strategies),
            "ids": [s.id for s in cfgm.strategies.strategies],
            "enabled": [s.id for s in cfgm.strategies.strategies if getattr(s, "enabled", True)],
        }
        facts["cost"] = {
            "commission_per_lot_round_trip": cfgm.app.cost.commission_per_lot_round_trip,
            "per_symbol_overrides": len(cfgm.app.cost.symbol_commission_per_lot_round_trip),
        }
        lab = cfgm.app.research_lab
        all_ids = [s.id for s in cfgm.strategies.strategies]
        gated = [s for s in all_ids if s in set(lab.gate_strategies)]
        facts["research_gate"] = {
            "gate_live": lab.gate_live,
            "gate_require_evidence": lab.gate_require_evidence,
            "gate_max_age_hours": lab.gate_max_age_hours,
            "gate_min_trades": lab.gate_min_trades,
            # Researched and gated are DIFFERENT counts, and conflating them is what hid the fact that
            # seven of eight strategies had no research at all. Report both, always.
            "strategies_researched": len(all_ids) if lab.research_all_strategies else 1,
            "strategies_gated": len(gated),
            "gated": gated,
            "researched_not_gated": [s for s in all_ids if s not in set(lab.gate_strategies)],
            "per_strategy_thresholds": {
                sid: Thresholds(lab, sid).to_dict() for sid in all_ids},
            "strategies_on_global_bar": [sid for sid in all_ids
                                         if Thresholds(lab, sid).source == "global default"],
        }
    except Exception as exc:                                          # noqa: BLE001
        facts["config_error"] = f"{type(exc).__name__}: {exc}"

    return facts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    facts = collect()
    if args.json:
        print(json.dumps(facts, indent=2, default=str))
        return 0

    s = facts["startup"]
    print("STARTUP")
    print(f"  checkpoints (STARTUP_SEQUENCE) : {s['checkpoints']}")
    print(f"  enum members (StartupState)    : {s['enum_members']}")
    print(f"  terminal, not checkpoints      : {', '.join(s['terminal_states'])}")
    print(f"  -> write this in the docs      : {s['prose']}")
    print()
    print(f"TRADE STATES : {facts['trade_states']['count']}")
    print(f"RISK STATES  : {facts['risk_states']['count']}")

    if "config_error" in facts:
        print(f"\n(config not loadable here: {facts['config_error']})")
        return 0

    ns = facts["news_straddle"]
    print("\nNEWS STRADDLE")
    print(f"  enabled            : {ns['enabled']}")
    print(f"  configured symbols : {ns['configured_symbols']}")
    print(f"  max_armed_symbols  : {ns['max_armed_symbols']}")
    print(f"  ceiling binds?     : {ns['ceiling_binds']}")
    if not ns["ceiling_binds"]:
        print("  !!! the ceiling constrains nothing - every configured symbol can arm at once")

    rg = facts["research_gate"]
    print("\nRESEARCH GATE")
    print(f"  gate_live              : {rg['gate_live']}")
    print(f"  strategies researched  : {rg['strategies_researched']}")
    print(f"  strategies that may BLOCK a trade : {rg['strategies_gated']} "
          f"({', '.join(rg['gated']) or 'none'})")
    if rg["researched_not_gated"]:
        print(f"  researched, record-only: {', '.join(rg['researched_not_gated'])}")
        print("      These have research but cannot block. Add one to research_lab.gate_strategies to")
        print("      let it block - that changes live behaviour and restarts the trade record.")
    print(f"  gate_require_evidence  : {rg['gate_require_evidence']}")
    print("\n  per-strategy bar (min trades / no-edge line / source):")
    for sid, th in rg["per_strategy_thresholds"].items():
        mark = " " if th["source"] != "global default" else "!"
        print(f"   {mark} {sid:<22}{th['min_trades']:>5} trades  {th['block_below_r']:+.2f}R  {th['source']}")
    on_global = rg["strategies_on_global_bar"]
    if len(on_global) == len(rg["per_strategy_thresholds"]):
        print("      ! every strategy is judged against the SAME bar. They have different indicators,")
        print("        trade frequencies and R distributions, so one bar judges the busy ones and leaves")
        print("        the slow ones permanently unjudged. Run tools/gate_thresholds.py for per-strategy")
        print("        recommendations derived from each strategy's own record.")
    if not rg["gate_require_evidence"]:
        print("  !!! FAILS OPEN: missing, stale, incomplete or thin research PERMITS the trade.")
        print("      Required to be true before real money (tools/live_readiness.py enforces it).")

    c = facts["cost"]
    print("\nCOST MODEL")
    print(f"  commission_per_lot_round_trip : {c['commission_per_lot_round_trip']}")
    print(f"  per-symbol overrides          : {c['per_symbol_overrides']}")
    if not c["commission_per_lot_round_trip"] and not c["per_symbol_overrides"]:
        print("  !!! zero commission is correct ONLY for an Exness Standard account.")
        print("      On Raw Spread / Zero the cost model understates every trade. Confirm your account type.")

    st = facts["strategies"]
    print(f"\nSTRATEGIES : {st['count']} configured, {len(st['enabled'])} enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
