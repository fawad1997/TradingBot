"""Order execution (section 41) and routing (section 42).

APPROVED SIGNAL -> revalidation (age, price drift, spread) -> re-size at the
current price -> broker validation (stops level) -> order_check -> durable
ORDER_SENT intent -> order_send -> immediate deal/position query -> actual state.

Rules:
* the intent (ORDER_SENT + client_order_id) is written BEFORE order_send;
* ambiguous results (timeout / connection / no response) are NEVER retried -
  the trade goes to RECONCILIATION_REQUIRED and the broker decides the truth;
* one re-validated retry is allowed for definite requotes only;
* a filled position without a broker-side stop is protected or closed immediately.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Callable, Optional

from app.broker.base import BrokerAdapter, REQUOTE_CODES
from app.config import AppConfig
from app.core.types import (Environment, OrderRequest, OrderResult, Position, RouteDecision, Side, Signal,
                            SymbolSpec, Tick, TradeState, utcnow)
from app.execution.book import TradeBook
from app.execution.protection import minimum_protection_distance, protection_distance_error
from app.risk.sizing import SizingResult

log = logging.getLogger(__name__)


def new_client_order_id() -> str:
    return "ATA" + secrets.token_hex(6).upper()      # 15 chars, fits MT5's 31-char comment


@dataclass
class ExecOutcome:
    trade: Optional[dict]
    status: str            # FILLED / CANCELLED / REJECTED / RECONCILIATION_REQUIRED / PAPER_SKIPPED
    reason: str = ""


class OrderRouter:
    """MARKET / WAIT / NO_TRADE. LIMIT routing is defined but disabled until a
    fill-probability model has been researched (config execution.allow_limit_orders)."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def decide(self, signal: Signal, now_broker: float, spread_ok: bool) -> RouteDecision:
        if now_broker > signal.valid_until_broker:
            return RouteDecision.CANCEL
        if not spread_ok:
            return RouteDecision.WAIT if self.cfg.spread_gate.wait_for_spread else RouteDecision.NO_TRADE
        return RouteDecision.MARKET


class ExecutionEngine:
    def __init__(self, broker: BrokerAdapter, book: TradeBook, cfg: AppConfig, env: Environment, magic: int,
                 symbol: str, spec_fn: Callable[[], SymbolSpec], on_latency: Callable[[float], None],
                 on_failure: Callable[[], None], on_slippage: Callable[[float], None],
                 on_uncertain: Callable[[dict], None]):
        self.broker = broker
        self.book = book
        self.cfg = cfg
        self.env = env
        self.magic = magic
        self.symbol = symbol
        self.spec_fn = spec_fn
        self.on_latency = on_latency
        self.on_failure = on_failure
        self.on_slippage = on_slippage
        self.on_uncertain = on_uncertain
        self.reserve_risk: Optional[Callable[[dict, float], tuple[bool, str]]] = None

    # ------------------------------------------------------------------ helpers
    def _req_state(self, req: OrderRequest) -> dict:
        return req.to_dict()

    def _res_state(self, res: OrderResult) -> dict:
        return {"ok": res.ok, "retcode": res.retcode, "text": res.retcode_text, "comment": res.comment,
                "order": res.order, "deal": res.deal, "volume": res.volume, "price": res.price,
                "ambiguous": res.ambiguous}

    def locate_position(self, client_id: str, res: Optional[OrderResult]) -> Optional[Position]:
        positions = self.broker.positions(self.symbol)
        if positions is None:
            return None
        for p in positions:
            if p.magic == self.magic and p.comment.startswith(client_id):
                return p
        if res is not None and res.order:
            for p in positions:
                if p.magic == self.magic and (p.ticket == res.order or p.identifier == res.order):
                    return p
        return None

    # ------------------------------------------------------------------ main
    def execute(self, signal: Signal, tick: Tick, now_broker: float,
                resize: Callable[[float, float], SizingResult], spread_ok: Callable[[Tick], bool],
                session: str, regime: str) -> ExecOutcome:
        if self.env is Environment.REAL:
            return ExecOutcome(None, "REJECTED", "REAL_EXECUTION_DISABLED")
        plan = signal.plan
        spec = self.spec_fn()
        side = plan.direction
        tp = plan.tp
        if tp is None:
            target_r = (plan.exit_model or {}).get("target_r")
            if target_r is not None and float(target_r) > 0:
                tp = plan.entry + side.sign * float(target_r) * abs(plan.entry - plan.sl)
        trade = self.book.create({
            "signal_id": signal.id, "strategy_id": signal.strategy_id, "strategy_version": signal.strategy_version,
            "symbol": signal.symbol, "direction": str(side), "environment": str(self.env),
            "state": str(TradeState.APPROVED), "client_order_id": new_client_order_id(),
            "entry_requested": plan.entry, "initial_sl": plan.sl, "sl": plan.sl, "tp": tp,
            "session": session, "regime": regime, "exit_model": plan.exit_model,
            # features = the indicator values the strategy saw when it fired; carried so a per-trade alert can
            # say WHY it fired without a second lookup on the execution path
            "extra": {"setup_key": plan.setup_key, "quality": plan.quality,
                      "features": dict(list((plan.features or {}).items())[:24])}})
        if plan.sl <= 0 or tp is None:
            self.book.transition(trade, TradeState.CANCELLED, "MISSING_PROTECTIVE_BRACKET",
                                 {"stop_loss": plan.sl, "take_profit": tp},
                                 exit_reason="MISSING_PROTECTIVE_BRACKET")
            return ExecOutcome(trade, "CANCELLED", "MISSING_PROTECTIVE_BRACKET")

        # ---------------- revalidation
        age = now_broker - signal.created_broker_time
        if age > self.cfg.execution.max_signal_age_sec or now_broker > signal.valid_until_broker:
            self.book.transition(trade, TradeState.CANCELLED, "STALE_SIGNAL", {"age_sec": age},
                                 exit_reason="STALE_SIGNAL")
            return ExecOutcome(trade, "CANCELLED", "STALE_SIGNAL")
        if not spread_ok(tick):
            self.book.transition(trade, TradeState.CANCELLED, "SPREAD_AT_EXECUTION",
                                 {"spread": tick.spread}, exit_reason="SPREAD_AT_EXECUTION")
            return ExecOutcome(trade, "CANCELLED", "SPREAD_AT_EXECUTION")
        price = tick.ask if side is Side.BUY else tick.bid
        drift = (price - plan.entry) * side.sign
        if drift > self.cfg.execution.max_entry_drift_frac_of_stop * plan.stop_distance:
            self.book.transition(trade, TradeState.CANCELLED, "ENTRY_DRIFT",
                                 {"drift": drift, "stop": plan.stop_distance}, exit_reason="ENTRY_DRIFT")
            return ExecOutcome(trade, "CANCELLED", "ENTRY_DRIFT")
        # stop must still be on the correct side and respect the broker minimum distance
        min_d = minimum_protection_distance(spec, tick.spread, self.cfg.execution.protection_safety_buffer_points)
        distance_error = protection_distance_error(side, tick, plan.sl, tp, min_d)
        if distance_error and distance_error.startswith("SL_TOO_CLOSE"):
            self.book.transition(trade, TradeState.CANCELLED, "STOP_INVALID_AT_PRICE",
                                 {"price": price, "sl": plan.sl, "min_distance": min_d,
                                  "detail": distance_error}, exit_reason="STOP_INVALID")
            return ExecOutcome(trade, "CANCELLED", "STOP_INVALID_AT_PRICE")
        if distance_error:
            self.book.transition(trade, TradeState.CANCELLED, "TARGET_ALREADY_REACHED", {"price": price, "tp": tp},
                                 exit_reason="TARGET_REACHED")
            return ExecOutcome(trade, "CANCELLED", "TARGET_ALREADY_REACHED")

        # ---------------- re-size at the actual price (risk can veto, broker constraints can veto)
        sizing = resize(price, plan.sl)
        if not sizing.feasible:
            self.book.transition(trade, TradeState.CANCELLED, "SIZE_INFEASIBLE_AT_EXECUTION", sizing.to_dict(),
                                 exit_reason=sizing.reason)
            return ExecOutcome(trade, "CANCELLED", sizing.reason)
        self.book.update(trade, requested_volume=sizing.volume, initial_risk_money=sizing.risk_money,
                         risk_fraction=sizing.risk_fraction)

        req = OrderRequest("DEAL", self.symbol, side=side, volume=sizing.volume, price=price,
                           sl=spec.round_price(plan.sl), tp=spec.round_price(tp) if tp else 0.0,
                           deviation=self.cfg.execution.deviation_points, magic=self.magic,
                           comment=trade["client_order_id"])
        return self._check_and_send(trade, signal, req, spec, allow_retry=True, resize=resize,
                                    risk_money=sizing.risk_money)

    def _check_and_send(self, trade: dict, signal: Signal, req: OrderRequest, spec: SymbolSpec, allow_retry: bool,
                        resize, risk_money: float) -> ExecOutcome:
        spec = self.spec_fn()
        live_tick = self.broker.tick(self.symbol)
        if live_tick is None:
            reason = "No live quote available for broker-protection preflight"
            self.book.exec_event(trade, signal.id, "PROTECTION_PREFLIGHT", order_type="MARKET",
                                 requested_state=self._req_state(req), error=reason)
            self.book.transition(trade, TradeState.CANCELLED, "PROTECTION_PREFLIGHT_NO_QUOTE",
                                 {"detail": reason}, exit_reason="PROTECTION_PREFLIGHT")
            return ExecOutcome(trade, "CANCELLED", "PROTECTION_PREFLIGHT_NO_QUOTE")
        req.sl = spec.round_price(req.sl) if req.sl else 0.0
        req.tp = spec.round_price(req.tp) if req.tp else 0.0
        minimum_distance = minimum_protection_distance(
            spec, live_tick.spread, self.cfg.execution.protection_safety_buffer_points
        )
        distance_error = protection_distance_error(req.side, live_tick, req.sl, req.tp, minimum_distance)
        if distance_error:
            reason_code = ("STOP_INVALID_AT_PRICE" if distance_error.startswith("SL_TOO_CLOSE")
                           else "TARGET_ALREADY_REACHED")
            self.book.exec_event(trade, signal.id, "PROTECTION_PREFLIGHT", order_type="MARKET",
                                 requested_state=self._req_state(req),
                                 observed_state={"minimum_distance": minimum_distance},
                                 error=distance_error)
            self.book.transition(trade, TradeState.CANCELLED, reason_code,
                                 {"minimum_distance": minimum_distance, "detail": distance_error},
                                 exit_reason=reason_code)
            return ExecOutcome(trade, "CANCELLED", reason_code)
        chk = self.broker.order_check(req)
        self.book.exec_event(trade, signal.id, "ORDER_CHECK", order_type="MARKET", requested_state=self._req_state(req),
                             observed_state={**self._res_state(chk), **chk.raw}, retcode=chk.retcode,
                             retcode_text=chk.retcode_text, requested_volume=req.volume, requested_price=req.price,
                             latency_ms=chk.latency_ms)
        if not chk.ok:
            self.on_failure()
            self.book.transition(trade, TradeState.REJECTED, "ORDER_CHECK_FAILED",
                                 {"retcode": chk.retcode, "text": chk.retcode_text, "comment": chk.comment},
                                 exit_reason=f"ORDER_CHECK_{chk.retcode_text}")
            return ExecOutcome(trade, "REJECTED", chk.retcode_text)

        if self.reserve_risk is not None:
            allowed, reason = self.reserve_risk(trade, risk_money)
            if not allowed:
                reason = reason or "PORTFOLIO_RISK_RESERVATION_REJECTED"
                self.book.exec_event(trade, signal.id, "RISK_RESERVATION_REJECTED", error=reason)
                self.book.transition(trade, TradeState.CANCELLED, reason,
                                     {"risk_money": risk_money}, exit_reason=reason)
                return ExecOutcome(trade, "REJECTED", reason)

        # durable intent BEFORE the irreversible action
        self.book.transition(trade, TradeState.ORDER_SENT, "ORDER_SENT", {"request": self._req_state(req)},
                             requested_volume=req.volume)
        self.book.exec_event(trade, signal.id, "ORDER_SENT", order_type="MARKET",
                             requested_state=self._req_state(req), requested_volume=req.volume,
                             requested_price=req.price)
        res = self.broker.order_send(req)
        self.on_latency(res.latency_ms)
        self.book.exec_event(trade, signal.id, "ORDER_RESULT", order_type="MARKET",
                             requested_state=self._req_state(req), observed_state=self._res_state(res),
                             retcode=res.retcode, retcode_text=res.retcode_text, order_ticket=res.order or None,
                             deal_ticket=res.deal or None, requested_volume=req.volume, filled_volume=res.volume,
                             requested_price=req.price, price=res.price or None, latency_ms=res.latency_ms,
                             error=None if res.ok else res.comment)

        if res.ok:
            return self._confirm_fill(trade, signal, req, res, spec)
        if res.ambiguous:
            self.on_failure()
            self.book.transition(trade, TradeState.RECONCILIATION_REQUIRED, "AMBIGUOUS_ORDER_RESULT",
                                 {"retcode": res.retcode, "text": res.retcode_text}, order_ticket=res.order or None)
            self.on_uncertain(trade)
            return ExecOutcome(trade, "RECONCILIATION_REQUIRED", res.retcode_text)
        if allow_retry and self.cfg.execution.revalidated_retry_on_requote and res.retcode in REQUOTE_CODES:
            tick = self.broker.tick(self.symbol)
            if tick is not None:
                price = tick.ask if req.side is Side.BUY else tick.bid
                drift = (price - signal.plan.entry) * req.side.sign
                if drift <= self.cfg.execution.max_entry_drift_frac_of_stop * signal.plan.stop_distance:
                    sizing = resize(price, signal.plan.sl)
                    if sizing.feasible:
                        self.book.transition(trade, TradeState.APPROVED, "REVALIDATED_RETRY",
                                             {"previous_retcode": res.retcode, "new_price": price})
                        self.book.update(trade, requested_volume=sizing.volume,
                                         initial_risk_money=sizing.risk_money, risk_fraction=sizing.risk_fraction)
                        req2 = OrderRequest(**{**req.__dict__, "price": price, "volume": sizing.volume})
                        return self._check_and_send(
                            trade, signal, req2, spec, allow_retry=False, resize=resize,
                            risk_money=sizing.risk_money)
        self.on_failure()
        self.book.transition(trade, TradeState.REJECTED, "ORDER_REJECTED",
                             {"retcode": res.retcode, "text": res.retcode_text, "comment": res.comment},
                             exit_reason=f"BROKER_{res.retcode_text}")
        return ExecOutcome(trade, "REJECTED", res.retcode_text)

    def _confirm_fill(self, trade: dict, signal: Signal, req: OrderRequest, res: OrderResult,
                      spec: SymbolSpec) -> ExecOutcome:
        pos = self.locate_position(trade["client_order_id"], res)
        if pos is None:
            # broker said OK but we cannot see the position yet -> reconcile, do NOT resend
            self.book.transition(trade, TradeState.RECONCILIATION_REQUIRED, "FILL_NOT_VISIBLE",
                                 {"order": res.order, "deal": res.deal}, order_ticket=res.order or None)
            self.on_uncertain(trade)
            return ExecOutcome(trade, "RECONCILIATION_REQUIRED", "FILL_NOT_VISIBLE")
        slip_pts = (pos.price_open - req.price) * req.side.sign / spec.point if spec.point else 0.0
        self.on_slippage(slip_pts)
        filled = pos.volume
        # recompute R on the ACTUAL fill
        self.book.transition(trade, TradeState.OPEN, "FILLED",
                             {"position": pos.ticket, "price": pos.price_open, "volume": filled,
                              "slippage_points": slip_pts},
                             position_id=pos.ticket, order_ticket=res.order or None, filled_volume=filled,
                             open_volume=filled, cancelled_volume=max(0.0, round(req.volume - filled, 8)),
                             entry_price=pos.price_open, sl=pos.sl or None, tp=pos.tp or None,
                             opened_at=utcnow(), best_price=pos.price_open, worst_price=pos.price_open,
                             bars_held=0, extra={**(trade.get("extra") or {}), "entry_broker_time": pos.time})
        self.book.exec_event(trade, signal.id, "FILL", position_id=pos.ticket, order_ticket=res.order or None,
                             deal_ticket=res.deal or None, requested_volume=req.volume, filled_volume=filled,
                             requested_price=req.price, price=pos.price_open, slippage_points=slip_pts,
                             observed_state={"sl": pos.sl, "tp": pos.tp, "comment": pos.comment})
        if filled + 1e-9 < req.volume:
            self.book.event(trade, "PARTIAL_FILL", "OPEN", "OPEN", {"requested": req.volume, "filled": filled})
        # broker-resident protection must exist
        missing_sl = bool(req.sl and not pos.sl)
        missing_tp = bool(req.tp and not pos.tp)
        if missing_sl or missing_tp:
            mod_sl = req.sl or pos.sl
            mod_tp = req.tp or pos.tp
            spec = self.spec_fn()
            attach_tick = self.broker.tick(self.symbol)
            if attach_tick is None:
                mod = OrderResult(ok=False, retcode=10021, retcode_text="PRICE_OFF",
                                  comment="No live quote available to validate protective bracket")
            else:
                minimum_distance = minimum_protection_distance(
                    spec, attach_tick.spread, self.cfg.execution.protection_safety_buffer_points
                )
                distance_error = protection_distance_error(
                    req.side, attach_tick, mod_sl, mod_tp, minimum_distance
                )
                mod = (OrderResult(ok=False, retcode=10016, retcode_text="LOCAL_STOP_DISTANCE",
                                   comment=distance_error)
                       if distance_error else
                       self.broker.modify_sltp(self.symbol, pos.ticket, mod_sl, mod_tp))
            self.book.exec_event(trade, signal.id, "ATTACH_SL", position_id=pos.ticket, retcode=mod.retcode,
                                 retcode_text=mod.retcode_text, latency_ms=mod.latency_ms,
                                 requested_state={"sl": mod_sl, "tp": mod_tp, "missing_sl": missing_sl,
                                                  "missing_tp": missing_tp},
                                 error=None if mod.ok else mod.comment)
            if mod.ok:
                self.book.update(trade, sl=mod_sl, tp=mod_tp or None)
            elif self.cfg.execution.close_unprotected_position:
                log.error("Could not attach protective bracket to %s - closing position", pos.ticket)
                cr = self.broker.close_position(pos, pos.volume, self.cfg.execution.deviation_points, self.magic,
                                                "unprotected")
                self.book.exec_event(trade, signal.id, "CLOSE_UNPROTECTED", position_id=pos.ticket,
                                     retcode=cr.retcode, retcode_text=cr.retcode_text, price=cr.price or None)
                self.book.event(trade, "UNPROTECTED_CLOSE_REQUESTED", trade["state"], trade["state"],
                                {"ok": cr.ok})
                self.on_uncertain(trade)   # reconciliation computes the final P/L from deals
        self.book.exec_event(trade, signal.id, "RECONCILED", position_id=pos.ticket,
                             observed_state={"position": pos.ticket, "volume": pos.volume, "sl": pos.sl, "tp": pos.tp})
        return ExecOutcome(trade, "FILLED")
