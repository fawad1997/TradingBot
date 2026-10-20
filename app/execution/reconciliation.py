"""Broker reconciliation (sections 44, 46, 68).

Recovery invariant: broker state > our assumptions. After any restart, and
periodically, every active trade is matched against the broker's positions and
deal history. Ambiguity is scoped per ticket (with exponential backoff and
escalation); only a failure to read broker state at all freezes the portfolio.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from app.broker.base import BrokerAdapter
from app.core.types import DEAL_REASON_NAMES, Deal, Position, Side, TradeState, utcnow
from app.execution.book import TradeBook, as_epoch

log = logging.getLogger(__name__)

PENDING_STATES = {str(TradeState.APPROVED), str(TradeState.ORDER_SENT), str(TradeState.RECONCILIATION_REQUIRED)}
OPEN_STATES = {str(TradeState.OPEN), str(TradeState.PROTECTION), str(TradeState.PARTIAL), str(TradeState.RUNNER),
               str(TradeState.USER_MODIFIED)}


class Reconciler:
    def __init__(self, broker: BrokerAdapter, book: TradeBook, symbol: str, magic: int, env: str,
                 alert: Callable[[str, str], None], health, wall=time.time,
                 ambiguity_timeout_sec: float = 60.0, escalate_after: int = 5):
        self.broker = broker
        self.book = book
        self.symbol = symbol
        self.magic = magic
        self.env = env
        self.alert = alert
        self.health = health
        self.wall = wall
        self.ambiguity_timeout = ambiguity_timeout_sec
        self.escalate_after = escalate_after
        self.attempts: Dict[str, int] = {}
        self.next_try: Dict[str, float] = {}
        self.foreign_seen: set = set()
        self.last_run: Optional[float] = None
        self._resting = None      # resting pending orders at the broker, read once per run (None = unknown)

    # ------------------------------------------------------------------ helpers
    def _deals_for(self, trade: dict) -> Optional[List[Deal]]:
        pid = trade.get("position_id")
        if pid:
            d = self.broker.deals_for_position(int(pid))
            if d is not None:
                return d
        start = datetime.fromtimestamp(max(0.0, as_epoch(trade.get("created_at")) - 3600), tz=timezone.utc)
        allp = self.broker.history_deals(start, utcnow() + timedelta(hours=1))
        if allp is None:
            return None
        cid = trade.get("client_order_id") or "~"
        ids = {d.position_id for d in allp if d.magic == self.magic and d.comment.startswith(cid)}
        if pid:
            ids.add(int(pid))
        return [d for d in allp if d.position_id in ids]

    def _backoff_ok(self, tid: str) -> bool:
        return self.wall() >= self.next_try.get(tid, 0.0)

    def _fail_attempt(self, trade: dict, why: str) -> None:
        tid = trade["id"]
        n = self.attempts.get(tid, 0) + 1
        self.attempts[tid] = n
        self.next_try[tid] = self.wall() + min(300.0, 2.0 ** n)
        self.health.recon_uncertain_tickets.add(trade.get("position_id") or trade.get("client_order_id"))
        if n == self.escalate_after:
            self.alert("CRITICAL", f"Reconciliation uncertainty for trade {tid[:8]} ({why}) after {n} attempts")

    def _clear(self, trade: dict) -> None:
        self.attempts.pop(trade["id"], None)
        self.next_try.pop(trade["id"], None)
        self.health.recon_uncertain_tickets.discard(trade.get("position_id"))
        self.health.recon_uncertain_tickets.discard(trade.get("client_order_id"))

    def finalize_closed(self, trade: dict, deals: List[Deal], terminal: TradeState = TradeState.CLOSED,
                        reason_override: Optional[str] = None) -> bool:
        outs = [d for d in deals if d.entry in ("OUT", "OUT_BY", "INOUT")]
        if not outs:
            return False
        vol = sum(d.volume for d in outs)
        exit_px = sum(d.price * d.volume for d in outs) / vol if vol else outs[-1].price
        gross = sum(d.profit for d in deals)
        comm = sum(d.commission + d.fee for d in deals)
        swap = sum(d.swap for d in deals)
        net = gross + comm + swap
        last = max(outs, key=lambda d: (d.time, d.ticket))
        reason = reason_override or trade.get("pending_exit_reason") or DEAL_REASON_NAMES.get(last.reason, str(last.reason))
        risk_money = float(trade.get("initial_risk_money") or 0.0)
        entry = float(trade.get("entry_price") or 0.0)
        init_sl = float(trade.get("initial_sl") or 0.0)
        r_mult = None
        if risk_money > 0:
            r_mult = net / risk_money
        elif entry and init_sl and entry != init_sl:
            r_mult = (exit_px - entry) * Side(trade["direction"]).sign / abs(entry - init_sl)
        st = self.book.mstate.get(trade["id"])
        mfe = st.mfe_r if st else trade.get("mfe_r")
        mae = st.mae_r if st else trade.get("mae_r")
        give = (max(0.0, mfe - r_mult) if (mfe is not None and r_mult is not None) else None)
        if last.reason in (4, 5, 6) and terminal == TradeState.CLOSED:
            terminal = TradeState.BROKER_CLOSED
        self.book.transition(trade, terminal, "POSITION_CLOSED",
                             {"deals": [d.ticket for d in deals], "deal_reason": DEAL_REASON_NAMES.get(last.reason)},
                             closed_at=utcnow(), exit_price=exit_px, exit_reason=reason, gross_pnl=gross,
                             commission=comm, swap=swap, net_pnl=net, r_multiple=r_mult, mfe_r=mfe, mae_r=mae,
                             giveback_r=give, open_volume=0.0,
                             bars_held=st.bars_held if st else trade.get("bars_held"),
                             best_price=st.best_price if st else trade.get("best_price"),
                             worst_price=st.worst_price if st else trade.get("worst_price"))
        self._clear(trade)
        return True

    # ------------------------------------------------------------------ main
    def run(self, startup: bool = False) -> dict:
        """Returns a summary. Never raises for a single bad ticket."""
        self.last_run = self.wall()
        positions = self.broker.positions(self.symbol)
        if positions is None:
            self.health.portfolio_uncertain = True
            self.alert("CRITICAL", "Cannot read broker positions - portfolio frozen (no new trades)")
            return {"status": "PORTFOLIO_UNCERTAIN"}
        self.health.portfolio_uncertain = False
        self._resting = None
        if getattr(self.broker, "supports_pending", False):
            try:
                orders = self.broker.pending_orders(self.symbol)
                self._resting = None if orders is None else [o for o in orders if o.magic == self.magic]
            except Exception as e:
                log.warning("pending order listing failed: %s", e)
        ours = {p.ticket: p for p in positions if p.magic == self.magic}
        foreign = [p for p in positions if p.magic != self.magic]
        matched: set = set()
        summary = {"status": "OK", "opened": 0, "closed": 0, "cancelled": 0, "adopted": 0, "modified": 0,
                   "uncertain": 0, "foreign": len(foreign)}

        for trade in list(self.book.active.values()):
            try:
                self._reconcile_trade(trade, ours, matched, startup, summary)
            except Exception as e:  # scoped: one ticket cannot break the rest
                log.exception("reconcile trade %s failed: %s", trade.get("id"), e)
                self._fail_attempt(trade, f"exception {e}")
                summary["uncertain"] += 1

        # positions with our magic that we do not know -> adopt and alert
        for ticket, p in ours.items():
            if ticket in matched:
                continue
            row = self.book.create({
                "strategy_id": "UNKNOWN", "symbol": p.symbol, "direction": str(p.side), "environment": self.env,
                "state": str(TradeState.USER_MODIFIED), "client_order_id": f"ADOPT{ticket}",
                "position_id": ticket, "requested_volume": p.volume, "filled_volume": p.volume,
                "open_volume": p.volume, "entry_price": p.price_open, "initial_sl": p.sl or None, "sl": p.sl or None,
                "tp": p.tp or None, "opened_at": utcnow(), "exit_model": {"type": "fixed"},
                "extra": {"adopted": True, "comment": p.comment, "entry_broker_time": p.time}})
            self.book.event(row, "UNEXPECTED_POSITION_ADOPTED", None, str(TradeState.USER_MODIFIED),
                            {"ticket": ticket, "comment": p.comment})
            self.alert("CRITICAL", f"Unexpected position {ticket} ({p.side} {p.volume} {p.symbol}) adopted for monitoring")
            summary["adopted"] += 1
        for p in foreign:
            if p.ticket not in self.foreign_seen:
                self.foreign_seen.add(p.ticket)
                self.alert("WARNING", f"Manual/foreign position {p.ticket} {p.side} {p.volume} {p.symbol} "
                                      f"detected - counted for risk, NOT managed")
        return summary

    def _reconcile_trade(self, trade: dict, ours: Dict[int, Position], matched: set, startup: bool,
                         summary: dict) -> None:
        state = trade.get("state")
        cid = trade.get("client_order_id") or "~"
        pos: Optional[Position] = None
        if trade.get("position_id") and int(trade["position_id"]) in ours:
            pos = ours[int(trade["position_id"])]
        else:
            for p in ours.values():
                if p.comment.startswith(cid):
                    pos = p
                    break
        if pos is not None:
            matched.add(pos.ticket)

        if state in PENDING_STATES:
            if not self._backoff_ok(trade["id"]) and not startup:
                return
            if pos is not None:
                self.book.transition(trade, TradeState.OPEN, "RECONCILED_FILL",
                                     {"position": pos.ticket, "price": pos.price_open, "volume": pos.volume},
                                     position_id=pos.ticket, filled_volume=pos.volume, open_volume=pos.volume,
                                     entry_price=pos.price_open, sl=pos.sl or None, tp=pos.tp or None,
                                     opened_at=trade.get("opened_at") or utcnow(),
                                     best_price=pos.price_open, worst_price=pos.price_open,
                                     extra={**(trade.get("extra") or {}), "entry_broker_time": pos.time})
                self.book.exec_event(trade, trade.get("signal_id"), "RECONCILED", position_id=pos.ticket,
                                     observed_state={"source": "positions", "volume": pos.volume})
                self._clear(trade)
                summary["opened"] += 1
                return
            if state == str(TradeState.ORDER_SENT) and (trade.get("extra") or {}).get("straddle_id"):
                # a news-straddle leg: the order may still be RESTING at the broker - that is not "not filled"
                if self._resting is None:
                    self._fail_attempt(trade, "pending orders unreadable")
                    summary["uncertain"] += 1
                    return
                ticket = (trade.get("extra") or {}).get("pending_order_ticket")
                if any((ticket and o.ticket == int(ticket)) or o.comment.startswith(cid) for o in self._resting):
                    self._clear(trade)
                    return
            if state == str(TradeState.APPROVED):
                # nothing was ever sent (intent is written before order_send)
                self.book.transition(trade, TradeState.CANCELLED, "NOT_SENT", {"startup": startup},
                                     exit_reason="NOT_SENT")
                summary["cancelled"] += 1
                return
            deals = self._deals_for(trade)
            if deals is None:
                self._fail_attempt(trade, "deal history unavailable")
                summary["uncertain"] += 1
                return
            if deals:
                ins = [d for d in deals if d.entry == "IN"]
                if ins and not trade.get("position_id"):
                    self.book.update(trade, position_id=ins[0].position_id, entry_price=ins[0].price,
                                     filled_volume=sum(d.volume for d in ins), opened_at=utcnow())
                if self.finalize_closed(trade, deals):
                    summary["closed"] += 1
                    return
            age = self.wall() - as_epoch(trade.get("updated_at") or trade.get("created_at"))
            if startup or age >= self.ambiguity_timeout:
                self.book.transition(trade, TradeState.CANCELLED, "CONFIRMED_NOT_FILLED",
                                     {"checked": "positions+deals", "age_sec": age}, exit_reason="NOT_FILLED")
                self._clear(trade)
                summary["cancelled"] += 1
            else:
                self._fail_attempt(trade, "waiting for broker history")
                summary["uncertain"] += 1
            return

        if state in OPEN_STATES:
            if pos is not None:
                changes = {}
                if abs(pos.volume - float(trade.get("open_volume") or 0.0)) > 1e-9:
                    changes["open_volume"] = pos.volume
                if abs((pos.sl or 0.0) - float(trade.get("sl") or 0.0)) > 1e-9:
                    changes["sl"] = pos.sl or None
                if abs((pos.tp or 0.0) - float(trade.get("tp") or 0.0)) > 1e-9:
                    changes["tp"] = pos.tp or None
                if changes:
                    # broker is authoritative; we never made an un-recorded change, so this is external
                    old = {k: trade.get(k) for k in changes}
                    self.book.transition(trade, TradeState.USER_MODIFIED, "BROKER_STATE_DIFFERS",
                                         {"old": old, "new": changes}, **changes)
                    st = self.book.mstate.get(trade["id"])
                    if st is not None:
                        if "sl" in changes:
                            st.sl = pos.sl or st.sl
                        if "tp" in changes:
                            st.tp = pos.tp or None
                        if "open_volume" in changes:
                            st.volume = pos.volume
                    summary["modified"] += 1
                self._clear(trade)
                return
            deals = self._deals_for(trade)
            if deals is None:
                self._fail_attempt(trade, "position gone, history unavailable")
                summary["uncertain"] += 1
                return
            if self.finalize_closed(trade, deals):
                summary["closed"] += 1
            else:
                self._fail_attempt(trade, "position gone, closing deal not visible yet")
                summary["uncertain"] += 1
