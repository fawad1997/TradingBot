"""Pure helpers behind the dashboard "Analysis" page: why trades lost, and which good signals were blocked."""
from __future__ import annotations

import json
import math
from typing import Dict, Iterable, List

# gates that mean "the system, not the market or the risk rules, stopped a signal"
SYSTEM_GATES = {"HEALTH", "KILL_SWITCH", "STALE", "EXECUTION", "ENVIRONMENT", "SYMBOL_DISABLED"}
RISK_GATES = {"RISK", "SIZE", "MARGIN", "PORTFOLIO"}
COST_GATES = {"SPREAD", "COST", "EDGE"}


def classify_block(gate: str, reason: str) -> str:
    gate, reason = (gate or "").upper(), (reason or "").upper()
    if gate == "RESEARCH":
        return "RESEARCH_LAB"
    if gate == "ML_FILTER":
        return "LEARNED_FILTER"
    if gate == "VISION":
        # RETIRED gate. Historical rejection rows written before the vision gate was removed still carry it, and
        # the analysis page must keep classifying them rather than dumping them all into "OTHER".
        return "SYSTEM_UNRESPONSIVE" if reason.startswith("VISION_UNAVAILABLE") else "VISION_REJECTED_HISTORICAL"
    if gate in SYSTEM_GATES or "UNAVAILABLE" in reason or "UNKNOWN" in reason:
        return "SYSTEM"
    if gate in RISK_GATES:
        return "RISK_LIMIT"
    if gate in COST_GATES:
        return "COST_OR_SPREAD"
    if gate in {"REGIME", "SESSION", "EVENT", "STRATEGY_STATUS"}:
        return "MARKET_CONDITION"
    return "OTHER"


def _features(sig: dict) -> dict:
    f = sig.get("features")
    if isinstance(f, str):
        try:
            f = json.loads(f)
        except ValueError:
            f = {}
    return f if isinstance(f, dict) else {}


def _num(v):
    try:
        v = float(v)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def signal_view(sig: dict) -> dict:
    """What was known when the signal fired: setup and calculations.

    vision_* keys are still filtered out of `features`: signals recorded before the vision gate was removed
    carry them, and they would otherwise appear as mystery columns in the losing-trades table.
    """
    if not sig:
        return {}
    f = _features(sig)
    return {"quality": _num(sig.get("quality")), "edge_r": _num(sig.get("edge_r")), "cost_r": _num(sig.get("cost_r")),
            "regime": sig.get("regime"), "session": sig.get("session"), "event_state": sig.get("event_state"),
            "setup_key": sig.get("setup_key"), "entry": _num(sig.get("entry")), "sl": _num(sig.get("sl")),
            "tp": _num(sig.get("tp")),
            "trigger": f.get("trigger_pattern") or f.get("pattern"),
            "microstructure": f.get("microstructure"),
            "features": {k: v for k, v in f.items() if not str(k).startswith("vision_") and k != "microstructure"}}


def exit_outcome(exit_reason, net_pnl, r_multiple=None) -> str:
    """What the exit MEANT, which is not what exit_reason says.

    `exit_reason` comes from the broker's deal reason: "SL" means the stop ORDER filled, and carries no
    information at all about profit or loss. Once the profit ladder has trailed a stop past the entry, a
    winning trade also closes with reason "SL" - on 16 Oct, 7 of 12 "SL" exits were profits of +0.37R to
    +0.41R. Grouping by the raw reason therefore files protected wins alongside full losses and makes the
    stop look like the thing losing the money.

    The money decides; the reason only adds detail.
    """
    try:
        pnl = float(net_pnl)
    except (TypeError, ValueError):
        return "unknown"
    reason = str(exit_reason or "").upper()
    if pnl > 0:
        if reason in ("TP", "TARGET_REACHED"):
            return "reached target"
        if reason in ("SL", "STOP_OUT"):
            return "protected win (trailed stop)"
        return "protected win"
    if pnl == 0:
        return "break even"
    if reason in ("SL", "STOP_OUT"):
        try:
            if r_multiple is not None and float(r_multiple) > -0.9:
                return "partial loss"
        except (TypeError, ValueError):
            pass
        return "full loss (first stop)"
    return "closed at a loss"


def build_loss_rows(trades: Iterable[dict], signals: Dict[str, dict]) -> List[dict]:
    rows = []
    for t in trades:
        pnl = _num(t.get("net_pnl"))
        if pnl is None or pnl >= 0:
            continue
        rows.append({"closed_at": t.get("closed_at"), "symbol": t.get("symbol"), "strategy": t.get("strategy_id"),
                     "direction": t.get("direction"), "net_pnl": pnl, "r_multiple": _num(t.get("r_multiple")),
                     "exit_reason": t.get("exit_reason"),
                     "outcome": exit_outcome(t.get("exit_reason"), pnl, t.get("r_multiple")),
                     "mfe_r": _num(t.get("mfe_r")), "mae_r": _num(t.get("mae_r")),
                     "giveback_r": _num(t.get("giveback_r")), "bars_held": t.get("bars_held"),
                     "risk_money": _num(t.get("initial_risk_money")), "session": t.get("session"),
                     "regime": t.get("regime"), "volume": _num(t.get("filled_volume") or t.get("requested_volume")),
                     "signal": signal_view(signals.get(t.get("signal_id")))})
    return rows


def loss_summary(rows: List[dict]) -> dict:
    by_strategy: Dict[str, dict] = {}
    by_exit: Dict[str, dict] = {}
    for r in rows:
        # grouped by what the exit MEANT, not by the broker's deal reason - see exit_outcome
        for bucket, key in ((by_strategy, r["strategy"]), (by_exit, r.get("outcome") or "unknown")):
            b = bucket.setdefault(key, {"trades": 0, "loss": 0.0})
            b["trades"] += 1
            b["loss"] = round(b["loss"] + r["net_pnl"], 4)
    return {"losing_trades": len(rows), "total_loss": round(sum(r["net_pnl"] for r in rows), 4),
            "by_strategy": by_strategy, "by_exit_reason": by_exit}


def build_blocked_rows(rejections: Iterable[dict], signals: Dict[str, dict]) -> List[dict]:
    rows = []
    for r in rejections:
        sv = signal_view(signals.get(r.get("signal_id")))
        edge = sv.get("edge_r")
        category = classify_block(r.get("gate"), r.get("reason"))
        rows.append({"created_at": r.get("created_at"), "symbol": r.get("symbol"), "strategy": r.get("strategy_id"),
                     "direction": r.get("direction"), "gate": r.get("gate"), "reason": r.get("reason"),
                     "category": category, "calc_positive": bool(edge > 0) if edge is not None else bool(sv.get("quality")),
                     "calc_basis": "net edge" if edge is not None else ("strategy setup valid (edge not yet computed)" if sv else "no signal record"),
                     "proposed_entry": _num(r.get("proposed_entry")), "proposed_sl": _num(r.get("proposed_sl")),
                     "proposed_tp": _num(r.get("proposed_tp")), "proposed_size": _num(r.get("proposed_size")),
                     "spread_points": _num(r.get("spread_points")), "signal": sv})
    return rows


def blocked_summary(rows: List[dict]) -> dict:
    cats: Dict[str, int] = {}
    for r in rows:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    return {"blocked": len(rows), "by_category": cats,
            "positive_calc_blocked_by_system": sum(1 for r in rows if r["calc_positive"]
                                                    and r["category"] in ("SYSTEM", "SYSTEM_UNRESPONSIVE"))}


def build_scoreboard(trades: Iterable[dict], counterfactuals: Iterable[dict], statuses: Dict[str, str]) -> List[dict]:
    """One row per strategy: how it did on real trades, and what its unexecuted signals would have done.

    A SHADOW strategy has no trades, so its evidence is the counterfactual column: every signal it produced is
    followed to the stop or the target it asked for. That is the number to compare candidates on - alongside the
    research lab's walk-forward, never instead of it."""
    agg: Dict[str, dict] = {}

    def slot(sid: str) -> dict:
        return agg.setdefault(sid or "?", {"strategy": sid or "?", "status": statuses.get(sid, ""),
                                           "live_trades": 0, "live_r": [], "shadow_signals": 0, "shadow_r": []})

    # Seed every configured strategy, so one that has not produced anything yet is VISIBLE as "no data yet"
    # rather than silently missing from a card that claims to list every strategy. A rule that never fires is
    # itself a finding, and an absent row looks identical to a rule that was never installed.
    for sid in statuses:
        slot(sid)

    for t in trades:
        r = _num(t.get("r_multiple"))
        if r is None:
            continue
        s = slot(t.get("strategy_id"))
        s["live_trades"] += 1
        s["live_r"].append(r)
    for c in counterfactuals:
        s = slot(c.get("strategy_id"))
        s["shadow_signals"] += 1
        r = _num(c.get("hypothetical_r"))
        if r is not None and str(c.get("status", "")).upper() == "RESOLVED":
            s["shadow_r"].append(r)

    def stats(values: List[float]) -> dict:
        if not values:
            return {"n": 0, "mean_r": None, "total_r": None, "win_rate": None}
        wins = len([v for v in values if v > 0])
        return {"n": len(values), "mean_r": round(sum(values) / len(values), 4),
                "total_r": round(sum(values), 2), "win_rate": round(wins / len(values), 3)}

    out = []
    for sid, s in agg.items():
        live, shadow = stats(s["live_r"]), stats(s["shadow_r"])
        if live["n"] >= 30:
            evidence = "live"
        elif shadow["n"] >= 30:
            evidence = "shadow"
        elif live["n"] == 0 and s["shadow_signals"] == 0:
            evidence = "no signals yet"        # installed and evaluating, but has not fired in this window
        else:
            evidence = "too few"
        out.append({"strategy": sid, "status": s["status"], "live": live, "shadow": shadow,
                    "shadow_signals": s["shadow_signals"], "evidence": evidence})
    key = lambda r: (r["live"]["mean_r"] if r["live"]["n"] >= 30 else
                     (r["shadow"]["mean_r"] if r["shadow"]["n"] >= 30 else None))
    out.sort(key=lambda r: (key(r) is None, -(key(r) or 0)))
    return out


# ---------------------------------------------------------------------------------------- by session
# Sessions are already tagged on every trade, signal and evaluation, and the BACKTESTER has reported
# by_session for a long time. Nothing had ever grouped the REAL trades that way, so the one question worth
# asking of live results - "does this system work better at some hours than others?" - had no answer on any
# screen. This is the cheapest remaining idea with a real chance of finding an edge, because it filters rules
# that already exist rather than inventing another one.
#
# The honest caution lives in the output, not in a comment: `trustworthy` is False until a bucket has enough
# trades to mean anything, and the dashboard says so rather than letting a 3-trade session look like a
# finding.
MIN_TRADES_FOR_A_VERDICT = 30

SESSION_ORDER = ["ASIA", "LONDON", "LONDON_NY_OVERLAP", "NEW_YORK", "ROLLOVER"]


def _bucket_stats(rows: List[dict]) -> dict:
    """Expectancy and the costs behind it, for one bucket of closed trades."""
    rs = [_num(t.get("r_multiple")) for t in rows]
    rs = [r for r in rs if r is not None]
    pnls = [_num(t.get("net_pnl")) or 0.0 for t in rows]
    if not rs:
        return {"n": 0, "trustworthy": False}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    # a "full stop" is a loss at or near the original risk: the stop was never moved into profit
    full_stops = [r for r in rs if r <= -0.9]
    mfes = [m for m in (_num(t.get("mfe_r")) for t in rows) if m is not None]
    return {
        "n": len(rs),
        "trustworthy": len(rs) >= MIN_TRADES_FOR_A_VERDICT,
        "mean_r": round(sum(rs) / len(rs), 4),
        "total_r": round(sum(rs), 2),
        "money": round(sum(pnls), 2),
        "win_rate": round(len(wins) / len(rs), 3),
        "avg_win_r": round(sum(wins) / len(wins), 3) if wins else None,
        "avg_loss_r": round(sum(losses) / len(losses), 3) if losses else None,
        "full_stop_rate": round(len(full_stops) / len(rs), 3),
        "avg_peak_r": round(sum(mfes) / len(mfes), 3) if mfes else None,
    }


def session_breakdown(trades: Iterable[dict], slippage_by_trade: Dict[str, float] = None,
                      symbol: str = None, exclude_strategies: Iterable[str] = None) -> dict:
    """Live expectancy per session, optionally for one symbol and excluding chosen strategies.

    Answers the question the plan asks of the New York session, without assuming the answer: it reports what
    each session actually produced and refuses to call anything a finding below MIN_TRADES_FOR_A_VERDICT.

    `exclude_strategies` exists because a session table is only ever a weighted average of whatever was
    trading. When one strategy supplies seven of every ten trades, this table describes THAT STRATEGY and
    not the hours - and acting on it after disabling that strategy would be acting on a picture of
    something that no longer trades. Excluding it turns a question that needs a month of new data into one
    the existing record already answers.
    """
    slip = slippage_by_trade or {}
    drop = {str(x) for x in (exclude_strategies or ())}
    buckets: Dict[str, List[dict]] = {}
    for t in trades:
        if symbol and t.get("symbol") != symbol:
            continue
        if drop and str(t.get("strategy_id")) in drop:
            continue
        if _num(t.get("r_multiple")) is None:
            continue
        buckets.setdefault(str(t.get("session") or "UNKNOWN"), []).append(t)

    out = []
    for name in SESSION_ORDER + sorted(k for k in buckets if k not in SESSION_ORDER):
        rows = buckets.get(name)
        if not rows:
            continue
        stats = _bucket_stats(rows)
        slips = [slip[str(t.get("id"))] for t in rows if str(t.get("id")) in slip]
        stats.update(session=name,
                     avg_slippage_points=round(sum(slips) / len(slips), 2) if slips else None,
                     symbols=sorted({str(t.get("symbol")) for t in rows}),
                     strategies=sorted({str(t.get("strategy_id")) for t in rows}))
        out.append(stats)

    judged = [b for b in out if b["trustworthy"]]
    best = max(judged, key=lambda b: b["mean_r"], default=None)
    worst = min(judged, key=lambda b: b["mean_r"], default=None)
    total_n = sum(b["n"] for b in out)
    return {
        "sessions": out,
        "total_trades": total_n,
        "min_trades_for_a_verdict": MIN_TRADES_FOR_A_VERDICT,
        "best_session": best["session"] if best else None,
        "worst_session": worst["session"] if worst else None,
        # the headline sentence the dashboard shows, written here so it cannot drift from the numbers
        "verdict": (
            f"Not enough finished trades to judge any session yet - "
            f"{total_n} so far, and a session needs {MIN_TRADES_FOR_A_VERDICT} before its number means anything."
            if not judged else
            f"{best['session'].replace('_', ' ').title()} is the strongest session so far at "
            f"{best['mean_r']:+.3f}R per trade over {best['n']} trades"
            + (f", against {worst['session'].replace('_', ' ').title()} at {worst['mean_r']:+.3f}R."
               if worst and worst is not best else ".")
            + (" Sessions below the trade threshold are shown but not judged."
               if len(judged) < len(out) else "")),
    }


def hour_breakdown(trades: Iterable[dict], symbol: str = None,
                   exclude_strategies: Iterable[str] = None) -> List[dict]:
    """The same question at a finer grain: expectancy by hour of the day the trade was OPENED (UTC).

    Takes the same `exclude_strategies` filter, and for the same reason - see session_breakdown."""
    from datetime import datetime, timezone
    drop = {str(x) for x in (exclude_strategies or ())}
    buckets: Dict[int, List[dict]] = {}
    for t in trades:
        if symbol and t.get("symbol") != symbol:
            continue
        if drop and str(t.get("strategy_id")) in drop:
            continue
        if _num(t.get("r_multiple")) is None:
            continue
        opened = t.get("opened_at")
        if not opened:
            continue
        try:
            when = opened if isinstance(opened, datetime) else datetime.fromisoformat(str(opened))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        buckets.setdefault(when.astimezone(timezone.utc).hour, []).append(t)
    return [{"hour_utc": h, **_bucket_stats(buckets[h])} for h in sorted(buckets)]
