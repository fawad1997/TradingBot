"""Before the session: is it ready? After the session: what happened?

Two reports, both read-only. The important word is read-only: this looks at the system and says what it
sees, and it never changes a parameter, enables a rule or resumes anything. A routine that quietly retunes
production while reporting on it would turn every morning check into an unlogged configuration change.

The pre-session check answers one question - "if a setup appears in the next hour, can the bot actually take
it?" - by testing the things that silently stop trades: the broker link, the price feed, the database, how
stale the news calendar is, whether spreads are inside their ceilings, and how much risk budget is left.

The post-session review answers "what did it do, and what did it cost?" in R, in money, and in the things
that do not show up in either: refused orders, self-imposed waits, and trades that gave back most of what
they had.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def _epoch(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _num(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------- before the session
def readiness(state: Dict[str, Any], calendar_age_hours: Optional[float] = None) -> Dict[str, Any]:
    """Can the bot take a trade right now? One line per thing that could stop it.

    `state` is the published core state - the same dictionary the dashboard reads - so this check cannot
    disagree with what the screen shows.
    """
    checks: List[Dict[str, Any]] = []

    def check(name: str, ok: Optional[bool], detail: str, blocking: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "blocking": blocking})

    comp = state.get("components") or {}
    for label, key in (("MetaTrader link", "MT5"), ("Price feed", "DATA"), ("Database", "DATABASE"),
                       ("Order sending", "EXECUTION")):
        v = str(comp.get(key) or "UNKNOWN")
        check(label, v in ("HEALTHY", "MARKET_CLOSED"), v)

    acct = state.get("account") or {}
    bal = _num(acct.get("balance"))
    check("Account readable", bal is not None,
          f"balance {bal:,.2f}" if bal is not None else "cannot read the account")

    kill = state.get("kill_switch") or {}
    stopped = bool(kill.get("engaged") or (kill.get("volatile") or []))
    check("Emergency stop", not stopped,
          "off" if not stopped else f"ON - {kill.get('reason') or ', '.join(kill.get('volatile') or [])}")

    risk = state.get("risk") or {}
    rstate = str(risk.get("state") or "UNKNOWN")
    check("Risk state", rstate == "NORMAL", rstate, blocking=rstate in ("NO_NEW_TRADES", "EMERGENCY"))
    left = _num(risk.get("daily_loss_remaining"))
    check("Daily loss budget", left is None or left > 0,
          f"{left:,.2f} left today" if left is not None else "unknown",
          blocking=(left is not None and left <= 0))

    trading = state.get("symbol_trading") or {}
    on = [s for s, v in trading.items() if v.get("enabled")]
    check("Symbols running", bool(on), f"{len(on)} of {len(trading)}" if trading else "none started yet")

    market = state.get("symbol_market_status") or {}
    openish = [s for s, v in market.items() if str(v.get("status")) == "OPEN"]
    check("Markets open", bool(openish), f"{len(openish)} of {len(market)} quoting" if market else "unknown",
          blocking=False)

    if calendar_age_hours is not None:
        check("News calendar", calendar_age_hours <= 48,
              f"refreshed {calendar_age_hours:.0f}h ago", blocking=False)

    m = state.get("market") or {}
    sp, limit = _num(m.get("spread_points")), _num(m.get("spread_limit_points"))
    if sp is not None and limit:
        check("Buy/sell gap", sp <= limit, f"{sp:.0f} of {limit:.0f} allowed on {state.get('symbol')}",
              blocking=False)

    blockers = [c for c in checks if c["blocking"] and c["ok"] is False]
    warnings = [c for c in checks if not c["blocking"] and c["ok"] is False]
    return {
        "ready": not blockers,
        "checks": checks,
        "blockers": [c["name"] for c in blockers],
        "warnings": [c["name"] for c in warnings],
        "summary": ("Ready to trade." if not blockers else
                    "NOT ready: " + "; ".join(f"{c['name']} ({c['detail']})" for c in blockers))
        + (f" Worth noting: {', '.join(c['name'] for c in warnings)}." if warnings else ""),
    }


# -------------------------------------------------------------------------------- after the session
def review(repo, now: datetime, hours: float = 8.0) -> Dict[str, Any]:
    """What the last stretch of trading produced, and what it cost. Reads only."""
    since = (now.astimezone(timezone.utc) - timedelta(hours=hours)).timestamp()

    def recent(table, where=None, col="created_at", limit=4000):
        try:
            rows = repo.select(table, where, order_by=col, desc=True, limit=limit) or []
        except Exception:
            log.debug("session review could not read %s", table, exc_info=True)
            return []
        return [r for r in rows if _epoch(r.get(col)) >= since]

    trades = recent("trades", {"state": ["CLOSED", "BROKER_CLOSED"]}, "closed_at", 1000)
    rs = [r for r in (_num(t.get("r_multiple")) for t in trades) if r is not None]
    money = sum(_num(t.get("net_pnl")) or 0.0 for t in trades)
    wins = [r for r in rs if r > 0]

    execs = recent("execution_events", None, "created_at", 8000)
    fills = [e for e in execs if e.get("event_type") == "FILL"]
    slips = [s for s in (_num(e.get("slippage_points")) for e in fills) if s is not None]
    # Slippage in points is NOT comparable across instruments (Kissell ch.3): a point of gold and a
    # point of EURUSD are different quantities, so the pooled average of the two means nothing. Keep
    # the raw figure per symbol, where it is meaningful, and report basis points for the aggregate.
    slips_by_symbol: Dict[str, List[float]] = {}
    slips_bp: List[float] = []
    for e in fills:
        s, px = _num(e.get("slippage_points")), _num(e.get("price"))
        sym = str(e.get("symbol") or "?")
        if s is None:
            continue
        slips_by_symbol.setdefault(sym, []).append(s)
        point = _num(e.get("point")) or _num((e.get("detail") or {}).get("point") if isinstance(e.get("detail"), dict) else None)
        if point and px:
            slips_bp.append(s * point / px * 10_000.0)
    # our own stop-move spacing, which used to be reported as a broker throttle
    self_waits = [e for e in execs if str(e.get("retcode_text")) == "OWN_COOLDOWN"]
    broker_refused = [e for e in execs
                      if e.get("retcode") not in (None, 10009) and str(e.get("retcode_text")) != "OWN_COOLDOWN"]

    rejections = recent("rejections", None, "created_at", 4000)
    by_reason: Dict[str, int] = {}
    for r in rejections:
        key = str(r.get("gate") or r.get("reason") or "?")
        by_reason[key] = by_reason.get(key, 0) + 1

    # trades that were well ahead and finished behind - the thing worth looking at by eye
    gave_back = []
    for t in trades:
        mfe, r = _num(t.get("mfe_r")), _num(t.get("r_multiple"))
        if mfe is not None and r is not None and mfe >= 0.5 and r < 0:
            gave_back.append({"symbol": t.get("symbol"), "strategy": t.get("strategy_id"),
                              "peak_r": round(mfe, 2), "result_r": round(r, 2),
                              "position_id": t.get("position_id")})

    by_session: Dict[str, List[float]] = {}
    for t in trades:
        r = _num(t.get("r_multiple"))
        if r is not None:
            by_session.setdefault(str(t.get("session") or "UNKNOWN"), []).append(r)

    return {
        "hours": hours,
        "trades": len(trades),
        "total_r": round(sum(rs), 2) if rs else 0.0,
        "money": round(money, 2),
        "win_rate": round(len(wins) / len(rs), 3) if rs else None,
        "avg_slippage_points": round(sum(slips) / len(slips), 2) if slips else None,
        "avg_slippage_bp": round(sum(slips_bp) / len(slips_bp), 2) if slips_bp else None,
        "slippage_by_symbol": {k: round(sum(v) / len(v), 2) for k, v in sorted(slips_by_symbol.items())},
        "orders_refused_by_broker": len(broker_refused),
        "stop_moves_we_spaced_out": len(self_waits),
        "signals_not_taken": len(rejections),
        "top_reasons_not_taken": sorted(by_reason.items(), key=lambda kv: -kv[1])[:5],
        "gave_back_a_lead": gave_back,
        "by_session": {k: {"n": len(v), "mean_r": round(sum(v) / len(v), 3)} for k, v in by_session.items()},
    }


def review_message(d: Dict[str, Any]) -> str:
    """The review as a few lines someone will actually read on a phone."""
    if not d.get("trades"):
        return (f"Session review: no trades finished in the last {d.get('hours', 8):.0f} hours."
                + (f" {d['signals_not_taken']} signals were not taken."
                   if d.get("signals_not_taken") else ""))
    lines = [f"Session review: {d['trades']} trades, {d['total_r']:+.2f}R, {d['money']:+.2f}"]
    if d.get("win_rate") is not None:
        # Basis points where we can compute them: a pooled points figure across 13 instruments is
        # not a comparable quantity and should not be shown as though it were.
        if d.get("avg_slippage_bp") is not None:
            slip_text = f", average slippage {d['avg_slippage_bp']:.1f} bp"
        elif d.get("avg_slippage_points") is not None:
            slip_text = f", average slippage {d['avg_slippage_points']:.1f} pts (per-symbol figures below)"
        else:
            slip_text = ""
        lines.append(f"{d['win_rate']:.0%} won" + slip_text)
    if d.get("signals_not_taken"):
        top = ", ".join(f"{k.lower().replace('_', ' ')} {v}" for k, v in d["top_reasons_not_taken"][:3])
        lines.append(f"{d['signals_not_taken']} signals not taken — {top}")
    if d.get("orders_refused_by_broker"):
        lines.append(f"{d['orders_refused_by_broker']} orders refused by the broker")
    for g in d.get("gave_back_a_lead", [])[:3]:
        lines.append(f"Worth a look: {g['symbol']} reached {g['peak_r']:+.2f}R and finished "
                     f"{g['result_r']:+.2f}R ({g['strategy']})")
    return "\n".join(lines)
