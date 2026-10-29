#!/usr/bin/env python3
"""One trading day written down for review: what the bot traded, what it refused and why, what the refused signals
would have made, what the strategy selector changed, and each strategy's record so far.

The point is a daily habit - read the day, pick one thing to look into, change it, and watch the effect in the
next reports. Account trades are net of the costs actually paid; paper-followed signals carry the modelled spread.

    python tools/daily_review.py                    # yesterday (UTC), plus any missing day of the last week
    python tools/daily_review.py --day 2026-10-20   # one day
    python tools/daily_review.py --day today        # today so far

Writes research/reports/daily/<day>.md and <day>.json. Reads the database and the log; changes nothing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.types import ACTIVE_TRADE_STATES  # noqa: E402
from app.execution.book import as_epoch  # noqa: E402
from tools.live_breakdown import summarise  # noqa: E402

OUT_DIR = ROOT / "research" / "reports" / "daily"
LOG = ROOT / "logs" / "quantara.log"
CLOSED = ["CLOSED", "BROKER_CLOSED"]
NOT_RECORDING_GAP_SEC = 15 * 60
ALERT_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*ALERT\[(\w+)\] (.*)$")
NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def day_bounds(day: date) -> tuple:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def rows_between(repo, table: str, col: str, start: float, end: float, where: Optional[dict] = None) -> List[dict]:
    """Rows whose `col` falls in [start, end): SQL on PostgreSQL, a filtered scan on the in-memory test repo."""
    try:
        clauses = [f"{col} >= %s", f"{col} < %s"]
        params: list = [datetime.fromtimestamp(start, timezone.utc), datetime.fromtimestamp(end, timezone.utc)]
        for k, v in (where or {}).items():
            clauses.append(f"{k} = ANY(%s)" if isinstance(v, list) else f"{k} = %s")
            params.append(v)
        return repo.query(f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} ORDER BY {col}", params)
    except NotImplementedError:
        rows = [r for r in repo.select(table, where) if start <= as_epoch(r.get(col)) < end]
        return sorted(rows, key=lambda r: as_epoch(r.get(col)))


def _by(rows: Iterable[dict], key: str, value: str) -> Dict[str, dict]:
    groups: Dict[str, list] = defaultdict(list)
    for r in rows:
        groups[str(r.get(key) or "unknown")].append(_num(r.get(value)))
    return {k: summarise(v) for k, v in sorted(groups.items())}


def alerts_from_log(start: float, end: float, paths: Sequence[Path] = (LOG,)) -> Dict[str, int]:
    """WARNING/CRITICAL alerts raised in the window, grouped with their numbers masked (the same alert with a new
    figure is the same alert); INFO alerts are only counted. Log times are local wall-clock time."""
    out: Counter = Counter()
    info = 0
    for path in paths:
        if not path.exists():
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = ALERT_LINE.match(line)
                if not m:
                    continue
                at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").astimezone(timezone.utc).timestamp()
                if not start <= at < end:
                    continue
                if m.group(2) == "INFO":
                    info += 1
                else:
                    masked = NUMBER.sub("#", m.group(3))[:110]
                    out[f"{m.group(2)}: {masked}"] += 1
    result = dict(out.most_common())
    if info:
        result["INFO messages (start-ups, reconnects, routine notices)"] = info
    return result


def build(repo, day: date, environment: str, now: Optional[datetime] = None,
          log_paths: Sequence[Path] = (LOG,)) -> dict:
    start, end = day_bounds(day)
    now_ts = (now or datetime.now(timezone.utc)).timestamp()
    in_env = lambda r: not environment or r.get("environment") == environment  # noqa: E731

    snaps = [s for s in rows_between(repo, "account_snapshots", "created_at", start, end) if in_env(s)]
    account = None
    if snaps:
        times = [as_epoch(s["created_at"]) for s in snaps]
        edges = [times[0] - start, min(end, now_ts) - times[-1]] + [b - a for a, b in zip(times, times[1:])]
        account = {"start_equity": _num(snaps[0]["equity"]), "end_equity": _num(snaps[-1]["equity"]),
                   "start_balance": _num(snaps[0]["balance"]), "end_balance": _num(snaps[-1]["balance"]),
                   "equity_change": round(_num(snaps[-1]["equity"]) - _num(snaps[0]["equity"]), 2),
                   "lowest_equity": min(_num(s["equity"]) for s in snaps),
                   "not_recording_minutes": round(sum(g for g in edges if g > NOT_RECORDING_GAP_SEC) / 60)}

    risk_rows = rows_between(repo, "risk_states", "created_at", start, end)
    risk = {"states": dict(Counter(str(r.get("state")) for r in risk_rows)),
            "max_drawdown": max((_num(r.get("drawdown")) or 0.0 for r in risk_rows), default=None)}

    closed = [t for t in repo.select("trades", {"state": CLOSED}) if in_env(t)]
    today = sorted((t for t in closed if start <= as_epoch(t.get("closed_at")) < end),
                   key=lambda t: as_epoch(t.get("closed_at")))
    trades = {"summary": summarise([_num(t.get("r_multiple")) for t in today]),
              "net_pnl": round(sum(_num(t.get("net_pnl")) or 0.0 for t in today), 2),
              "by_strategy": _by(today, "strategy_id", "r_multiple"), "by_symbol": _by(today, "symbol", "r_multiple"),
              "list": [{k: t.get(k) for k in ("closed_at", "symbol", "strategy_id", "direction", "r_multiple",
                                              "net_pnl", "exit_reason", "bars_held", "session", "regime")}
                       for t in today]}
    if account is not None and account.get("start_balance") is not None and account.get("end_balance") is not None:
        account["deposit"] = unexplained_balance(account["start_balance"], account["end_balance"], trades["net_pnl"])
    open_now = [{k: t.get(k) for k in ("opened_at", "symbol", "strategy_id", "direction", "entry_price", "sl", "tp")}
                for t in repo.select("trades", {"state": list(ACTIVE_TRADE_STATES)}) if in_env(t)]

    recent_signals = [s for s in rows_between(repo, "signals", "created_at", start - 2 * 86400, end) if in_env(s)]
    env_ids = {str(s["id"]) for s in recent_signals}
    day_signals = [s for s in recent_signals if as_epoch(s.get("created_at")) >= start]
    signals = {"total": len(day_signals), "by_status": dict(Counter(str(s.get("status")) for s in day_signals)),
               "by_strategy": dict(Counter(str(s.get("strategy_id")) for s in day_signals).most_common())}

    rej = [r for r in rows_between(repo, "rejections", "created_at", start, end) if str(r.get("signal_id")) in env_ids]
    rejections = {"total": len(rej), "by_gate": dict(Counter(str(r.get("gate")) for r in rej).most_common()),
                  "top_reasons": [{"gate": g, "reason": why, "count": n} for (g, why), n in
                                  Counter((str(r.get("gate")), str(r.get("reason"))[:120]) for r in rej).most_common(10)]}

    cfs = [c for c in rows_between(repo, "counterfactuals", "resolved_at", start, end, {"status": "RESOLVED"})
           if str(c.get("signal_id")) in env_ids]
    shadow = {"summary": summarise([_num(c.get("hypothetical_r")) for c in cfs]),
              "by_strategy": _by(cfs, "strategy_id", "hypothetical_r"), "by_gate": _by(cfs, "gate", "hypothetical_r")}

    selector = {"states": {}, "benched": [], "changes": [], "first_scored": 0}
    for f in repo.select("system_flags"):
        if not str(f.get("key") or "").startswith("selector:") or not isinstance(f.get("value"), dict):
            continue
        for a in (f["value"].get("allocations") or {}).values():
            selector["states"][a["state"]] = selector["states"].get(a["state"], 0) + 1
            if a["state"] == "BENCHED" or a.get("vetoed_regimes"):
                selector["benched"].append({"strategy": a["strategy_id"], "symbol": a.get("symbol"),
                                            "state": a["state"], "regimes": a.get("vetoed_regimes") or [],
                                            "posterior_r": a.get("posterior_r"), "reason": a.get("reason")})
    for h in rows_between(repo, "selector_history", "created_at", start, end):
        if h.get("previous_state") is None:
            selector["first_scored"] += 1
        else:
            selector["changes"].append({k: h.get(k) for k in ("created_at", "symbol", "strategy_id",
                                                               "previous_state", "state", "reason", "applied")})

    lab = repo.get_flag("research_lab_latest") or {}
    research = {"last_cycle": lab.get("created_at"), "per_strategy_research": lab.get("strategies_created_at"),
                "verdict_counts": lab.get("strategy_verdict_counts") or {},
                "symbol_verdicts": dict(Counter(str((e or {}).get("verdict")) for e in (lab.get("symbols") or {}).values())),
                "evidence": research_evidence(lab), "replay": replay_summary()}

    audit = [{k: a.get(k) for k in ("created_at", "actor", "action", "path", "reason")}
             for a in rows_between(repo, "config_versions", "created_at", start, end)]
    alerts = alerts_from_log(start, end, log_paths)

    all_time = {"live": _by(closed, "strategy_id", "r_multiple"),
                "shadow": _by(repo.select("counterfactuals", {"status": "RESOLVED"}), "strategy_id", "hypothetical_r")}

    d = {"day": day.isoformat(), "environment": environment, "partial": now_ts < end,
         "generated_at": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(), "account": account, "risk": risk,
         "trades": trades, "open_positions": open_now, "signals": signals, "rejections": rejections,
         "shadow": shadow, "selector": selector, "research": research, "audit": audit, "alerts": alerts,
         "all_time": all_time}
    d["look_at"] = look_at(d)
    return d


def unexplained_balance(start_balance: float, end_balance: float, closed_net_pnl: float) -> Optional[float]:
    """The part of the day's balance change that closed trades do not explain: a deposit, a demo top-up or a
    withdrawal. None when it is within rounding (the larger of $1 and 0.2% of the balance), so a top-up is never
    read as trading profit and ordinary swap rounding never looks like a deposit."""
    gap = float(end_balance) - float(start_balance) - float(closed_net_pnl or 0.0)
    return round(gap, 2) if abs(gap) >= max(1.0, 0.002 * abs(float(end_balance))) else None


def replay_summary(report: Optional[dict] = None) -> Optional[dict]:
    """The latest replay report (tools/backtest_report.py), one line per rule: how good, how bad."""
    if report is None:
        try:
            from app.research.lab import replay
            report = replay.load()
        except Exception:                                               # noqa: BLE001
            report = None
    if not isinstance(report, dict) or not report.get("strategies"):
        return None
    rows = []
    for sid, v in report["strategies"].items():
        p = v.get("pooled") or {}
        if p.get("n"):
            rows.append({"strategy": sid, "n": p["n"], "avg_r": p.get("avg_r"), "lo": p.get("lo"), "hi": p.get("hi"),
                         "total_r": p.get("total_r"), "max_drawdown_r": p.get("max_drawdown_r"),
                         "worst_month": p.get("worst_month"), "unseen": p.get("unseen")})
    rows.sort(key=lambda r: -(r["total_r"] or 0))
    return {"created_at": report.get("created_at"), "rows": rows}


def research_evidence(lab: dict) -> dict:
    """The lab's evidence grades in one place: how many cells it looked at, how many show an edge after counting
    every attempt, the best of them by the bottom of their range, and what the live exits do on the same trades."""
    cells = []
    for sym, e in (lab.get("symbols") or {}).items():
        if not isinstance(e, dict):
            continue

        def add(rule, tf, x):
            st = (x or {}).get("stats") if isinstance(x, dict) else None
            if st:
                cells.append({"symbol": sym, "rule": rule, "tf": tf, **st})
        add("micro_scalper", "M1", e)
        for tf, x in (e.get("timeframes") or {}).items():
            if tf != "M1":
                add("micro_scalper", tf, x)
        for name, per in (e.get("hypotheses") or {}).items():
            for tf, x in ((per or {}).items() if isinstance(per, dict) else []):
                add(name, tf, x)
        add("intraday_momentum", "M5", e.get("intraday_momentum"))
    pairs = [(c["exits"]["as_traded"], c["exits"]["bracket"]) for c in cells
             if isinstance(c.get("exits"), dict) and c["exits"].get("as_traded") is not None
             and c["exits"].get("bracket") is not None]
    strategies = {}
    for sid, per in (lab.get("strategies") or {}).items():
        for sym, x in (per or {}).items():
            st = (x or {}).get("stats") if isinstance(x, dict) else None
            if st:
                strategies.setdefault(sid, {})[sym] = st
    trend = {sym: e["trend_daily"] for sym, e in (lab.get("symbols") or {}).items()
             if isinstance(e, dict) and isinstance(e.get("trend_daily"), dict)}
    return {"cycle": lab.get("evidence") or {}, "cells": len(cells),
            "grades": dict(Counter(str(c.get("grade")) for c in cells)),
            "best": sorted((c for c in cells if c.get("lo") is not None), key=lambda c: -c["lo"])[:5],
            "exit_effect_r": (round(sum(a - b for a, b in pairs) / len(pairs), 4) if pairs else None),
            "exit_pairs": len(pairs), "strategies": strategies, "strategy_cycle": lab.get("strategy_evidence") or {},
            "trend_pooled": lab.get("trend_daily") or {}, "trend": trend}


def look_at(d: dict) -> List[str]:
    """Plain statements of what stood out. Facts to investigate, not instructions."""
    out = []
    t, rej = d["trades"]["summary"], d["rejections"]
    if not t.get("n"):
        out.append("No trades closed on this day.")
    if rej["total"]:
        gate, n = next(iter(rej["by_gate"].items()))
        if n / rej["total"] >= 0.5:
            top = next((r for r in rej["top_reasons"] if r["gate"] == gate), None)
            out.append(f"{n} of {rej['total']} refused signals ({n / rej['total']:.0%}) stopped at {gate}"
                       + (f" - mostly: {top['reason']}" if top else "") + ".")
    for sid, s in d["all_time"]["live"].items():
        if s.get("verdict") in ("POSITIVE", "NEGATIVE"):
            out.append(f"{sid}: account record is confidently {s['verdict'].lower()} "
                       f"({s['mean_r']:+.3f}R per trade over {s['n']} trades).")
    acct = d["account"]
    if acct and acct.get("deposit"):
        out.append(f"Balance changed by {acct['deposit']:+.2f} that no closed trade explains - a deposit, demo "
                   f"top-up or withdrawal, not trading profit.")
    ev = (d.get("research") or {}).get("evidence") or {}
    if ev.get("cells"):
        n_edge = (ev.get("grades") or {}).get("EDGE", 0)
        trials = (ev.get("cycle") or {}).get("n_trials") or ev["cells"]
        out.append(f"Research: {n_edge} of {ev['cells']} rule cells show an edge after counting all {trials} "
                   f"looked at." if n_edge else
                   f"Research: none of {ev['cells']} rule cells shows an edge once all {trials} looked at are "
                   f"counted.")
    if acct is None:
        out.append(f"No {d['environment']} account snapshots - the bot was not running in this environment.")
    elif acct["not_recording_minutes"] >= 30:
        out.append(f"The bot was not recording for about {acct['not_recording_minutes']} minutes.")
    drops = sum(n for k, n in d["alerts"].items() if "Lost the connection to MetaTrader" in k)
    if drops:
        out.append(f"MetaTrader connection dropped {drops} time(s).")
    stops = {s: n for s, n in d["risk"]["states"].items() if s in ("NO_NEW_TRADES", "EMERGENCY")}
    if stops:
        out.append(f"Risk state reached {', '.join(stops)} (daily loss limit or drawdown).")
    if d["selector"]["changes"]:
        out.append(f"{len(d['selector']['changes'])} strategy selector change(s).")
    for a in d["audit"]:
        out.append(f"Operator action: {a['action']} by {a['actor']} - {a.get('reason') or ''}".rstrip(" -"))
    return out


# ------------------------------------------------------------------------------------------------ markdown
def _r(v, nd=3) -> str:
    return "–" if v is None else f"{v:+.{nd}f}R"


def _table(rows: Dict[str, dict], first: str) -> List[str]:
    if not rows:
        return ["_none_", ""]
    out = [f"| {first} | trades | won | per trade | total | 95% range | verdict |", "|---|---|---|---|---|---|---|"]
    for k, s in sorted(rows.items(), key=lambda kv: -(kv[1].get("n") or 0)):
        rng = "–" if s.get("lo") is None else f"{s['lo']:+.2f} to {s['hi']:+.2f}"
        won = "–" if s.get("win_rate") is None else f"{s['win_rate']:.0%}"
        out.append(f"| {k} | {s.get('n', 0)} | {won} | {_r(s.get('mean_r'))} | {_r(s.get('total_r'), 2)} | "
                   f"{rng} | {s.get('verdict', '')} |")
    return out + [""]


def render(d: dict) -> str:
    L = [f"# Daily review - {d['day']} (UTC){' - PARTIAL, day not finished' if d['partial'] else ''}", "",
         f"Environment: **{d['environment']}** · generated {d['generated_at'][:16].replace('T', ' ')} UTC", ""]
    L += ["## Look at", ""] + ([f"- {x}" for x in d["look_at"]] or ["- Nothing stood out."]) + [""]
    a = d["account"]
    L += ["## Account", ""]
    if a:
        L += [f"- Equity {a['start_equity']:.2f} → {a['end_equity']:.2f} ({a['equity_change']:+.2f}), "
              f"lowest {a['lowest_equity']:.2f}, balance {a['end_balance']:.2f}"]
        if a.get("deposit"):
            L += [f"- **Deposit / top-up {a['deposit']:+.2f}** - balance change no closed trade explains; "
                  f"not trading profit"]
        L += [
              f"- Risk states: {', '.join(f'{k} {v}' for k, v in d['risk']['states'].items()) or '–'}; "
              f"max drawdown {(d['risk']['max_drawdown'] or 0):.1%}", ""]
    else:
        L += ["- No account snapshots for this environment.", ""]
    t = d["trades"]
    s = t["summary"]
    L += ["## Trades closed", "",
          f"{s.get('n', 0)} trades · {('–' if s.get('win_rate') is None else format(s['win_rate'], '.0%'))} won · "
          f"{_r(s.get('mean_r'))} per trade · {_r(s.get('total_r'), 2)} total · net {t['net_pnl']:+.2f}", ""]
    L += ["### By strategy", ""] + _table(t["by_strategy"], "strategy")
    L += ["### By symbol", ""] + _table(t["by_symbol"], "symbol")
    if t["list"]:
        L += ["### Every trade", "", "| closed (UTC) | symbol | strategy | side | R | net | exit | candles |",
              "|---|---|---|---|---|---|---|---|"]
        for x in t["list"]:
            L.append(f"| {str(x['closed_at'])[:16]} | {x['symbol']} | {x['strategy_id']} | {x['direction']} | "
                     f"{_r(_num(x['r_multiple']), 2)} | {(_num(x['net_pnl']) or 0):+.2f} | {x['exit_reason']} | "
                     f"{x['bars_held']} |")
        L.append("")
    L += ["## Open now", ""] + ([f"- {p['symbol']} {p['strategy_id']} {p['direction']} from {p['entry_price']} "
                                 f"(SL {p['sl']}, TP {p['tp']})" for p in d["open_positions"]] or ["- none"]) + [""]
    sg, rj = d["signals"], d["rejections"]
    L += ["## Signals and why they were refused", "",
          f"{sg['total']} signals: {', '.join(f'{k} {v}' for k, v in sg['by_status'].items()) or '–'}", "",
          f"Refused {rj['total']}, by gate: {', '.join(f'{k} {v}' for k, v in rj['by_gate'].items()) or '–'}", ""]
    if rj["top_reasons"]:
        L += ["| gate | reason | count |", "|---|---|---|"]
        L += [f"| {r['gate']} | {r['reason']} | {r['count']} |" for r in rj["top_reasons"]] + [""]
    sh = d["shadow"]
    L += ["## Followed on paper (refused signals, resolved this day)", "",
          f"{sh['summary'].get('n', 0)} resolved · {_r(sh['summary'].get('mean_r'))} per signal", ""]
    L += ["### By strategy", ""] + _table(sh["by_strategy"], "strategy")
    L += ["### By the gate that refused them", ""] + _table(sh["by_gate"], "gate")
    se = d["selector"]
    L += ["## Strategy selector", "",
          f"Now: {', '.join(f'{k} {v}' for k, v in sorted(se['states'].items())) or 'not scored'}"
          + (f" · first scored this day: {se['first_scored']}" if se["first_scored"] else ""), ""]
    L += [f"- {b['strategy']} on {b['symbol']}: {b['state']}"
          + (f", benched in {', '.join(b['regimes'])}" if b["regimes"] else "") + f" ({b['reason']})"
          for b in se["benched"]]
    L += [f"- change {str(c['created_at'])[:16]}: {c['strategy_id']} on {c['symbol']} {c['previous_state']} → "
          f"{c['state']} ({c['reason']})" for c in se["changes"]] + [""]
    r = d["research"]
    L += ["## Research lab", "", f"- Last cycle {r['last_cycle'] or '–'}; per-strategy research "
          f"{r['per_strategy_research'] or 'not yet run'}",
          f"- Per-strategy verdicts: {json.dumps(r['verdict_counts']) if r['verdict_counts'] else '–'}",
          f"- micro_scalper replay by symbol: {', '.join(f'{k} {v}' for k, v in r['symbol_verdicts'].items()) or '–'}",
          ""]
    L += render_evidence(r.get("evidence") or {})
    L += render_replay(r.get("replay"))
    L += ["## Alerts", ""] + ([f"- {n}× {k}" for k, n in d["alerts"].items()] or ["- none"]) + [""]
    L += ["## Record so far (all days)", "", "### Account trades", ""] + _table(d["all_time"]["live"], "strategy")
    L += ["### Followed on paper", ""] + _table(d["all_time"]["shadow"], "strategy")
    L += ["_A 95% range that includes zero means the result could still be luck. Win rate alone is not the goal - "
          "R per trade after costs is._", ""]
    return "\n".join(L)


def _rng(st: dict) -> str:
    return "–" if st.get("lo") is None else f"{st['lo']:+.2f} to {st['hi']:+.2f}"


def render_replay(rp: Optional[dict]) -> List[str]:
    if not rp or not rp.get("rows"):
        return ["## Full-history replay", "", "- None yet (python tools/backtest_report.py).", ""]
    L = ["## Full-history replay", "",
         f"Every rule over all broker history, pooled across symbols ({str(rp['created_at'])[:16]}). "
         "Dashboard: Analysis, card 'Every rule replayed'.", "",
         "| rule | trades | per trade | 95% range | total | worst drawdown | worst month | unseen 30% |",
         "|---|---|---|---|---|---|---|---|"]
    for r in rp["rows"]:
        u = r.get("unseen") or {}
        wm = r.get("worst_month")
        worst = f"{wm[0]} {wm[1]:+.1f}R" if wm else "–"
        unseen = f"{u['n']} tr {_r(u.get('avg_r'))}" if u.get("n") else "–"
        L.append(f"| {r['strategy']} | {r['n']} | {_r(r['avg_r'])} | {_rng(r)} | {r['total_r']:+.1f}R | "
                 f"{r['max_drawdown_r']:.1f}R | {worst} | {unseen} |")
    return L + [""]


def render_evidence(ev: dict) -> List[str]:
    if not ev or not (ev.get("cells") or ev.get("strategies") or ev.get("trend_pooled")):
        return ["## Research evidence", "", "- No graded results yet (the lab grades from its next cycle).", ""]
    cyc = ev.get("cycle") or {}
    L = ["## Research evidence", "",
         f"Rule cells graded: {ev.get('cells', 0)} · "
         f"{', '.join(f'{k} {v}' for k, v in sorted((ev.get('grades') or {}).items())) or '–'} · "
         f"trials counted {cyc.get('n_trials', '–')}, best-of-noise Sharpe {cyc.get('benchmark_sr', '–')}", "",
         "_EDGE needs all three: a 95% range above zero, deflated Sharpe at least 0.95, and beating the same "
         "entries taken with random sides (p < 0.05)._", ""]
    if ev.get("best"):
        L += ["| symbol | rule | tf | trades | per trade | 95% range | DSR | overfit (PBO) | vs random | grade |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for c in ev["best"]:
            vr = "–" if c.get("edge_vs_random") is None else format(c["edge_vs_random"], "+.3f")
            L.append(f"| {c['symbol']} | {c['rule']} | {c['tf']} | {c.get('n')} | {_r(c.get('mean'))} | {_rng(c)} | "
                     f"{c.get('dsr') if c.get('dsr') is not None else '–'} | "
                     f"{c.get('pbo') if c.get('pbo') is not None else '–'} | {vr} (p {c.get('random_p', '–')}) | "
                     f"{c.get('grade')} |")
        L.append("")
    if ev.get("exit_pairs"):
        L += [f"- Breakeven + profit lock vs plain stop/target on the same entries: "
              f"{ev['exit_effect_r']:+.3f}R per trade on average over {ev['exit_pairs']} cells.", ""]
    strat = ev.get("strategies") or {}
    if strat:
        L += ["### Live strategies at their live settings (unseen window, as traded)", "",
              "| strategy | symbols graded | EDGE | NEGATIVE | best symbol (per trade, range) |", "|---|---|---|---|---|"]
        for sid, per in sorted(strat.items()):
            grades = Counter(str(x.get("grade")) for x in per.values())
            top = max(per.items(), key=lambda kv: (kv[1].get("lo") if kv[1].get("lo") is not None else -9))
            L.append(f"| {sid} | {len(per)} | {grades.get('EDGE', 0)} | {grades.get('NEGATIVE', 0)} | "
                     f"{top[0]} {_r(top[1].get('mean'))} ({_rng(top[1])}) |")
        L.append("")
    tp = ev.get("trend_pooled") or {}
    if tp:
        ci = tp.get("ci") or {}
        rnd = tp.get("random") or {}
        vr = "–" if rnd.get("edge_vs_random") is None else format(rnd["edge_vs_random"], "+.3f")
        L += ["### Daily trend following (decade replay, all symbols pooled)", "",
              f"- {tp.get('n_trades')} trades on {tp.get('symbols')} symbols: {_r(ci.get('mean'))} per trade, "
              f"95% range {_rng(ci)}, PSR {tp.get('dsr', '–')}, vs random sides {vr}R "
              f"(p {rnd.get('p_value', '–')}) - {tp.get('grade')}"]
        for sym, e in sorted((ev.get("trend") or {}).items()):
            st = e.get("stats") or {}
            if st.get("n"):
                L.append(f"  - {sym}: {st.get('n')} trades {_r(st.get('mean'))} ({_rng(st)}), {st.get('grade')}"
                         + (f", swap {e['avg_swap_r']:.2f}R a trade" if e.get("avg_swap_r") else ""))
        L.append("")
    return L


# ------------------------------------------------------------------------------------------------ cli
def days_to_write(today: date, existing: Sequence[date], catch_up: int) -> List[date]:
    """Yesterday always (it may hold a partial report), plus missing days back to the first report ever written."""
    yesterday = today - timedelta(days=1)
    first = min(existing) if existing else yesterday
    have = set(existing)
    missing = [yesterday - timedelta(days=i) for i in range(catch_up, 0, -1)]
    return [d for d in missing if d >= first and d not in have] + [yesterday]


def _parse_day(text: str) -> date:
    today = datetime.now(timezone.utc).date()
    return {"today": today, "yesterday": today - timedelta(days=1)}.get(text) or date.fromisoformat(text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", help="YYYY-MM-DD, 'today' or 'yesterday'. Default: yesterday plus any missing days.")
    ap.add_argument("--catch-up", type=int, default=7, help="days back to fill missing reports (default 7)")
    ap.add_argument("--env", help="environment to report (default: the configured one)")
    args = ap.parse_args()

    from app.config import ConfigManager, load_secrets
    from app.storage.repo import PostgresRepo
    env = args.env or ConfigManager().app.environment
    repo = PostgresRepo(load_secrets().database_url)
    if args.day:
        days = [_parse_day(args.day)]
    else:
        existing = [date.fromisoformat(p.stem) for p in OUT_DIR.glob("????-??-??.md")]
        days = days_to_write(datetime.now(timezone.utc).date(), existing, args.catch_up)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for day in days:
        d = build(repo, day, env)
        (OUT_DIR / f"{day}.md").write_text(render(d), encoding="utf-8")
        (OUT_DIR / f"{day}.json").write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
        print(f"{day}: {OUT_DIR / f'{day}.md'}")
        for line in d["look_at"]:
            print(f"   - {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
