#!/usr/bin/env python3
"""Everything the bot recorded about one trade, as a timeline, so a surprising result can be explained.

The question this exists to answer: a trade reached +3.65R and still closed at -1R. Either the profit lock
never fired, or it fired and the broker refused it. MT5's own History tab cannot tell you which, because it
only shows what succeeded. This does, because the bot records every attempt AND the broker's reply.

The timeline interleaves three sources:

  trades            the final record: entry, stop, target, MFE, MAE, exit, P/L
  trade_events      every state change: APPROVED -> ORDER_SENT -> OPEN -> PROTECTION -> CLOSED
  execution_events  every message sent to the broker and its reply, including REJECTED stop moves

A stop modification that was attempted and refused appears ONLY in execution_events, with its retcode. That
is normally the whole answer.

    python tools/explain_trade.py 2053170792
    python tools/explain_trade.py 2053170792 2053170828
    python tools/explain_trade.py --today              # every trade that closed today
    python tools/explain_trade.py 2053170792 --json    # machine-readable, for pasting back

Accepts a position id, an order ticket, or the bot's own trade id.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_secrets                                   # noqa: E402
from app.storage.repo import PostgresRepo                             # noqa: E402

# Retcodes worth explaining in words, because the number alone tells you nothing.
RETCODE_PLAIN = {
    10004: "requote - the price moved while the request was in flight",
    10006: "request rejected",
    10013: "invalid request",
    10015: "invalid price",
    10016: "INVALID STOPS - the stop was too close to the current price for this broker",
    10018: "market closed",
    10019: "not enough money",
    10024: "too many requests - the broker throttled us",
    10027: "AutoTrading disabled in the terminal",
    10029: "FROZEN - the broker will not allow changes this close to the price",
    10009: "done",
    10036: "position already closed",
}


def _f(v, nd=5):
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return "-" if v is None else str(v)


def _t(v):
    if not v:
        return "-"
    s = str(v)
    return s[:19].replace("T", " ")


def digits_for(price) -> int:
    try:
        p = abs(float(price))
    except (TypeError, ValueError):
        return 2
    return 5 if p < 20 else (3 if p < 500 else 2)


def find_trades(repo, keys, today=False):
    """Resolve whatever the user typed into trade rows."""
    rows = repo.select("trades", order_by="created_at", desc=True, limit=20000) or []
    if today:
        cut = datetime.now(timezone.utc) - timedelta(days=1)
        out = []
        for t in rows:
            closed = t.get("closed_at")
            if not closed:
                continue
            try:
                when = closed if isinstance(closed, datetime) else datetime.fromisoformat(str(closed))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when >= cut:
                    out.append(t)
            except (TypeError, ValueError):
                continue
        return out
    wanted = {str(k).strip() for k in keys}
    return [t for t in rows
            if str(t.get("position_id") or "") in wanted
            or str(t.get("order_ticket") or "") in wanted
            or str(t.get("id") or "") in wanted
            or str(t.get("client_order_id") or "") in wanted]


def timeline(repo, trade) -> list:
    """Every recorded moment for this trade, oldest first."""
    tid, pos = str(trade.get("id")), trade.get("position_id")
    events = []

    for e in repo.select("trade_events", {"trade_id": tid}, order_by="created_at", limit=2000) or []:
        events.append({"at": e.get("created_at"), "kind": "state",
                       "what": f"{e.get('from_state') or '?'} -> {e.get('to_state') or '?'}",
                       "detail": e.get("event_type") or "", "extra": e.get("details")})

    ex = repo.select("execution_events", {"trade_id": tid}, order_by="created_at", limit=2000) or []
    if pos:      # reconciliation can attach events by position before the trade id is known
        for e in repo.select("execution_events", {"position_id": pos}, order_by="created_at", limit=2000) or []:
            if e.get("id") not in {x.get("id") for x in ex}:
                ex.append(e)
    for e in ex:
        rc = e.get("retcode")
        rtxt = e.get("retcode_text") or ""
        plain = RETCODE_PLAIN.get(rc)
        bits = [p for p in (rtxt, plain) if p]
        events.append({"at": e.get("created_at"), "kind": "broker",
                       "what": e.get("event_type") or "?",
                       "detail": " - ".join(bits) + (f"  [{rc}]" if rc else ""),
                       "extra": {"requested": e.get("requested_state"), "observed": e.get("observed_state"),
                                 "requested_price": e.get("requested_price"), "price": e.get("price"),
                                 "slippage_points": e.get("slippage_points"), "error": e.get("error")}})
    events.sort(key=lambda x: str(x["at"]))
    return events


def report(repo, trade, as_json=False):
    d = digits_for(trade.get("entry_price"))
    tl = timeline(repo, trade)
    if as_json:
        return {"trade": {k: str(v) if isinstance(v, datetime) else v for k, v in trade.items()},
                "timeline": [{**e, "at": str(e["at"])} for e in tl]}

    out = []
    a = out.append
    a("=" * 100)
    a(f"{trade.get('symbol')}  {trade.get('direction')}  {_f(trade.get('filled_volume'), 2)} lots"
      f"   position {trade.get('position_id')}   order {trade.get('order_ticket')}")
    a(f"strategy {trade.get('strategy_id')}   state {trade.get('state')}   "
      f"session {trade.get('session')}   regime {trade.get('regime')}")
    a("-" * 100)
    a(f"  opened        {_t(trade.get('opened_at'))}   at {_f(trade.get('entry_price'), d)}"
      f"   (wanted {_f(trade.get('entry_requested'), d)})")
    a(f"  first stop    {_f(trade.get('initial_sl'), d)}        final stop {_f(trade.get('sl'), d)}"
      f"        target {_f(trade.get('tp'), d)}")
    a(f"  closed        {_t(trade.get('closed_at'))}   at {_f(trade.get('exit_price'), d)}"
      f"   because {trade.get('exit_reason')}")
    a(f"  risked        {_f(trade.get('initial_risk_money'), 2)}"
      f"        made/lost {_f(trade.get('net_pnl'), 2)}"
      f"        = {_f(trade.get('r_multiple'), 2)}R")
    a(f"  best it got   {_f(trade.get('mfe_r'), 2)}R        worst {_f(trade.get('mae_r'), 2)}R"
      f"        given back {_f(trade.get('giveback_r'), 2)}R")
    a(f"  protected     {trade.get('protected')}        partial taken {trade.get('partial_done')}")

    # The headline question, answered up front.
    try:
        mfe, r = float(trade.get("mfe_r") or 0), float(trade.get("r_multiple") or 0)
        if mfe >= 0.5 and r < 0:
            a("")
            a(f"  >>> THIS IS THE CASE WORTH EXPLAINING: it reached +{mfe:.2f}R and still closed at {r:.2f}R.")
            a("      Look below for a stop move that was attempted and refused, or for no stop move at all.")
    except (TypeError, ValueError):
        pass

    a("")
    a(f"  TIMELINE ({len(tl)} recorded moments)")
    a("  " + "-" * 96)
    if not tl:
        a("  nothing recorded - the trade predates event logging, or the ids do not match")
    for e in tl:
        tag = "state " if e["kind"] == "state" else "broker"
        a(f"  {_t(e['at'])}  {tag}  {str(e['what'])[:34]:<34}  {str(e['detail'])[:44]}")
        x = e.get("extra") or {}
        if isinstance(x, str):
            try:
                x = json.loads(x)
            except ValueError:
                x = {"detail": x}
        if isinstance(x, dict):
            req, obs = x.get("requested"), x.get("observed")
            for label, blob in (("wanted", req), ("got", obs)):
                if isinstance(blob, str):
                    try:
                        blob = json.loads(blob)
                    except ValueError:
                        pass
                if isinstance(blob, dict):
                    keep = {k: blob[k] for k in ("sl", "tp", "price", "volume") if k in blob}
                    if keep:
                        a(f"{'':>23}         {label}: " + ", ".join(f"{k} {_f(v, d)}" for k, v in keep.items()))
            if x.get("error"):
                # Wrap rather than cut. Truncating at 70 characters silently turned
                # "...by at least 0.0132" into "...by at least 0.01", which makes a CORRECT
                # stop-distance rejection look arithmetically impossible and sends whoever reads
                # it hunting a bug that is not there. A diagnostic that loses digits off the end
                # of a number is worse than one that takes an extra line.
                err = str(x["error"])
                pad = f"{'':>23}         "
                first, width = True, 70
                while err:
                    a(f"{pad}{'error: ' if first else '       '}{err[:width]}")
                    err, first = err[width:], False
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickets", nargs="*", help="position id, order ticket, or trade id")
    ap.add_argument("--today", action="store_true", help="every trade that closed in the last 24 hours")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    if not args.tickets and not args.today:
        ap.error("give at least one ticket, or --today")

    secrets = load_secrets()
    if not secrets.database_url:
        print("DATABASE_URL is not set in .env")
        return 2
    repo = PostgresRepo(secrets.database_url)

    trades = find_trades(repo, args.tickets, args.today)
    if not trades:
        print(f"No trade found for {', '.join(args.tickets) or 'today'}.")
        print("Try the position id from MT5's History tab, or the order ticket.")
        return 1

    if args.json:
        print(json.dumps([report(repo, t, as_json=True) for t in trades], indent=2, default=str))
        return 0
    for t in sorted(trades, key=lambda x: str(x.get("opened_at") or "")):
        print(report(repo, t))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
