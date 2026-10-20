"""TradeBook: in-memory view of active trades + durable state transitions.

Every transition is written on the critical path (trades + trade_events) BEFORE
the next broker action, so after a crash the database always says what we were
about to do - and reconciliation can determine what actually happened.
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from app.alpha.exits import OpenTradeState
from app.core.types import (ACTIVE_TRADE_STATES, OPEN_POSITION_STATES, TERMINAL_TRADE_STATES, Side, TradeState,
                            new_id, utcnow)

log = logging.getLogger(__name__)


def as_epoch(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, datetime):
        return v.timestamp()
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class TradeBook:
    def __init__(self, ledger, emit: Callable[[str, dict], None] = lambda *_: None):
        self.ledger = ledger
        self.emit = emit
        self.active: Dict[str, dict] = {}
        self.mstate: Dict[str, OpenTradeState] = {}

    # ---------------------------------------------------------------- persistence
    def create(self, row: dict) -> dict:
        row = dict(row)
        row.setdefault("id", new_id())
        row.setdefault("created_at", utcnow())
        row["updated_at"] = row["created_at"]
        self.ledger.insert("trades", row)
        self.active[row["id"]] = row
        self.event(row, "CREATED", None, row["state"], {})
        return row

    def update(self, trade: dict, **changes) -> None:
        changes["updated_at"] = utcnow()
        trade.update(changes)
        self.ledger.update("trades", "id", trade["id"], changes)

    def transition(self, trade: dict, new_state: TradeState, event_type: str, details: Optional[dict] = None,
                   **changes) -> None:
        old = trade.get("state")
        changes["state"] = str(new_state)
        self.update(trade, **changes)
        self.event(trade, event_type, old, str(new_state), details or {})
        if new_state in TERMINAL_TRADE_STATES:
            self.active.pop(trade["id"], None)
            self.mstate.pop(trade["id"], None)
        self.emit("trade_update", {"trade_id": trade["id"], "state": str(new_state), "event": event_type,
                                   "symbol": trade.get("symbol"), "direction": trade.get("direction"),
                                   "net_pnl": trade.get("net_pnl"), "r": trade.get("r_multiple")})
        # Optional observer (per-trade notifications). Set by the core; absent in tests and simulation. It is
        # called on EVERY state change a trade makes, so it is wrapped: a notification must never be able to
        # interrupt an order, a close, or a reconciliation.
        hook = getattr(self, "on_transition", None)
        if hook is not None:
            try:
                hook(trade, str(new_state), event_type)
            except Exception:
                log.exception("trade transition observer failed; the trade itself is unaffected")

    def event(self, trade: dict, event_type: str, from_state, to_state, details: dict) -> None:
        self.ledger.insert("trade_events", {"created_at": utcnow(), "trade_id": trade["id"], "event_type": event_type,
                                            "from_state": from_state, "to_state": to_state, "details": details})

    def exec_event(self, trade: Optional[dict], signal_id: Optional[str], event_type: str, **fields) -> None:
        row = {"event_id": new_id(), "created_at": utcnow(), "trade_id": trade["id"] if trade else None,
               "signal_id": signal_id, "event_type": event_type}
        row.update(fields)
        self.ledger.insert("execution_events", row)
        self.emit("execution_event", {k: (v if isinstance(v, (int, float, str, type(None))) else str(v))
                                      for k, v in row.items() if k not in ("requested_state", "observed_state")})

    # ---------------------------------------------------------------- queries
    def load_active(self, rows: List[dict]) -> None:
        for r in rows:
            self.active[str(r["id"])] = dict(r)

    def open_positions(self) -> List[dict]:
        return [t for t in self.active.values() if t.get("state") in [str(s) for s in OPEN_POSITION_STATES]
                or t.get("state") == str(TradeState.RECONCILIATION_REQUIRED) and t.get("position_id")]

    def by_position(self, ticket: int) -> Optional[dict]:
        for t in self.active.values():
            if t.get("position_id") and int(t["position_id"]) == int(ticket):
                return t
        return None

    def by_client_id(self, cid: str) -> Optional[dict]:
        for t in self.active.values():
            if t.get("client_order_id") == cid:
                return t
        return None

    def ensure_mstate(self, trade: dict) -> Optional[OpenTradeState]:
        """Rebuild exit-management state for an open trade (also after restart)."""
        if trade["id"] in self.mstate:
            return self.mstate[trade["id"]]
        if not trade.get("entry_price") or not trade.get("initial_sl"):
            return None
        vol = float(trade.get("open_volume") or trade.get("filled_volume") or 0.0)
        init_vol = float(trade.get("filled_volume") or vol)
        st = OpenTradeState(side=Side(trade["direction"]), entry=float(trade["entry_price"]),
                            initial_sl=float(trade["initial_sl"]), sl=float(trade.get("sl") or trade["initial_sl"]),
                            tp=float(trade["tp"]) if trade.get("tp") else None, volume=vol, initial_volume=init_vol,
                            entry_time=int((trade.get("extra") or {}).get("entry_broker_time")
                                           or as_epoch(trade.get("opened_at"))),
                            best_price=float(trade.get("best_price") or trade["entry_price"]),
                            worst_price=float(trade.get("worst_price") or trade["entry_price"]),
                            bars_held=int(trade.get("bars_held") or 0),
                            partial_done=bool(trade.get("partial_done")), protected=bool(trade.get("protected")),
                            profit_lock_active=bool((trade.get("extra") or {}).get("profit_lock_active")),
                            profit_lock_stop=(float((trade.get("extra") or {})["profit_lock_stop_price"])
                                              if (trade.get("extra") or {}).get("profit_lock_stop_price") is not None
                                              else None),
                            profit_lock_target_money=(
                                float((trade.get("extra") or {})["profit_lock_target_money"])
                                if (trade.get("extra") or {}).get("profit_lock_target_money") is not None
                                else None),
                            profit_high_watermark_money=float(
                                (trade.get("extra") or {}).get("profit_high_watermark_money") or 0.0
                            ))
        self.mstate[trade["id"]] = st
        return st
