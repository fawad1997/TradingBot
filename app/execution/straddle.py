"""News straddle: Dual Pending Orders + OCO + calendar-synchronised arming.

For a high-impact event (calendar: config/economic_events.csv or the optional calendar sync) the manager
  1. ARMS shortly before the release: one BUY STOP above and one SELL STOP below the market,
     both with broker-resident SL and TP, sized through the SAME risk machinery as every other trade
     (risk budget, margin, daily loss, portfolio reservation, cost gate) with the risk split between the legs;
  2. runs OCO: the moment one leg becomes a position the other resting order is cancelled;
  3. STANDS DOWN (cancels resting legs) on a spread shock before the release, kill switch, symbol STOP,
     risk emergency, broker/data trouble or low margin level, and always at the expiry time.

Filled legs are ordinary managed trades (TradeBook / TradeManager / Reconciler), so breakeven, profit
protection, reconciliation and the audit trail work exactly as for any other position.

Safety properties
* never one-sided on purpose: if the second order cannot be placed the first is cancelled;
* the durable trade rows (ORDER_SENT) are written BEFORE the orders are sent, so open risk is counted
  from the moment an order rests at the broker;
* the broker-side order expiration is set, so even a dead bot leaves no order resting past the window.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from app.config import AppConfig, NewsStraddleCfg
from app.core.types import (Environment, PendingOrderRequest, Side, Signal, SymbolSpec, Tick, TradePlan, TradeState,
                            new_id, utcnow)
from app.execution.book import TradeBook
from app.execution.engine import new_client_order_id
from app.execution.protection import minimum_protection_distance

log = logging.getLogger("straddle")

STRATEGY_ID = "news_straddle"
STRATEGY_VERSION = "1.0.0"
MIN_LEAD_SECONDS = 15            # do not arm when the release is closer than this
CLEANUP_INTERVAL_SEC = 30.0
CANCEL_ALERT_AFTER = 3           # consecutive failed cancels before a CRITICAL alert
UNKNOWN_ALERT_AFTER = 30         # consecutive cycles without readable broker state before a CRITICAL alert
LEG_SIDES = ("BUY", "SELL")

# straddle states
ARMED = "ARMED"
ARMED_ONE_SIDED = "ARMED_ONE_SIDED"
ONE_FILLED = "ONE_FILLED"
BOTH_FILLED = "BOTH_FILLED"
ACTIVE_STATES = {ARMED, ARMED_ONE_SIDED, ONE_FILLED, BOTH_FILLED}


@dataclass
class StraddleLevels:
    buy_price: float
    buy_sl: float
    buy_tp: float
    sell_price: float
    sell_sl: float
    sell_tp: float
    trigger_distance: float
    sl_distance: float
    tp_distance: float
    min_distance: float


def compute_levels(spec: SymbolSpec, tick: Tick, atr: float, cfg: NewsStraddleCfg,
                   safety_buffer_points: float) -> StraddleLevels:
    """ATR-scaled stop-order levels. Every distance is at least the broker minimum (stops + freeze level,
    spread and a safety buffer), so the orders are valid by construction."""
    min_d = minimum_protection_distance(spec, tick.spread, safety_buffer_points)
    trigger = max(cfg.entry_distance_atr * atr, min_d)
    sl_d = max(cfg.sl_atr * atr, min_d)
    tp_d = max(cfg.tp_atr * atr, sl_d * cfg.tp_atr / cfg.sl_atr)
    buy_price = spec.round_price(tick.ask + trigger)
    sell_price = spec.round_price(tick.bid - trigger)
    return StraddleLevels(
        buy_price=buy_price, buy_sl=spec.round_price(buy_price - sl_d), buy_tp=spec.round_price(buy_price + tp_d),
        sell_price=sell_price, sell_sl=spec.round_price(sell_price + sl_d),
        sell_tp=spec.round_price(sell_price - tp_d),
        trigger_distance=trigger, sl_distance=sl_d, tp_distance=tp_d, min_distance=min_d)


def event_key(ev) -> str:
    return f"{ev.time.astimezone(timezone.utc).strftime('%Y%m%dT%H%MZ')}|{ev.title}"


class StraddleManager:
    def __init__(self, *, cfg_fn: Callable[[], AppConfig], env: Environment, broker, book: TradeBook, repo,
                 magic: int, symbol: str, spec_fn: Callable[[], SymbolSpec], calendar,
                 now_utc: Callable[[], datetime], server_now: Callable[[], float],
                 tick_fn: Callable[[], Optional[Tick]], atr_fn: Callable[[], Optional[float]],
                 block_fn: Callable[[bool], Optional[str]], spread_ok_fn: Callable[[Tick], tuple],
                 spread_shock_fn: Callable[[], bool], size_fn: Callable, cost_fn: Callable,
                 reserve_risk: Callable[[dict, float], tuple], release_risk: Callable[[str], None],
                 insert_signal: Callable[[Signal, str], None], update_signal: Callable[[str, str], None],
                 context_fn: Callable[[], dict], alert: Callable[[str, str], None],
                 audit: Callable[[str, str, dict], None], emit: Callable[[str, dict], None],
                 on_uncertain: Callable[[dict], None], on_slippage: Callable[[float], None],
                 protection_deviation: Callable[[], int], close_unprotected: Callable[[], bool],
                 wall: Callable[[], float] = time.time, vol_scale_fn: Callable[[], float] = lambda: 1.0):
        self.cfg_fn = cfg_fn
        self.env = env
        self.broker = broker
        self.book = book
        self.repo = repo
        self.magic = magic
        self.symbol = symbol
        self.spec_fn = spec_fn
        self.calendar = calendar
        self.now_utc = now_utc
        self.server_now = server_now
        self.tick_fn = tick_fn
        self.atr_fn = atr_fn
        self.block_fn = block_fn
        self.spread_ok_fn = spread_ok_fn
        self.spread_shock_fn = spread_shock_fn
        self.size_fn = size_fn
        self.cost_fn = cost_fn
        self.reserve_risk = reserve_risk
        self.release_risk = release_risk
        self.insert_signal = insert_signal
        self.update_signal = update_signal
        self.context_fn = context_fn
        self.alert = alert
        self.audit = audit
        self.emit = emit
        self.on_uncertain = on_uncertain
        self.on_slippage = on_slippage
        self.protection_deviation = protection_deviation
        self.close_unprotected = close_unprotected
        self.wall = wall
        self.vol_scale_fn = vol_scale_fn
        self.cur: Optional[dict] = None
        self.done_keys: List[str] = []
        self.last_outcome: Optional[dict] = None
        self.last_skip: Optional[dict] = None
        self._logged: set = set()
        self._cancel_failures: Dict[str, int] = {}
        self._unknown_cycles = 0
        self._last_cleanup = 0.0

    # ------------------------------------------------------------------ config / switches
    @property
    def cfg(self) -> NewsStraddleCfg:
        return self.cfg_fn().news_straddle

    @property
    def flag_key(self) -> str:
        return f"news_straddle:{self.symbol}"

    def disabled_reason(self) -> Optional[str]:
        cfg = self.cfg
        if not cfg.enabled:
            return "DISABLED_IN_CONFIG"
        if self.symbol not in cfg.symbols:
            return "SYMBOL_NOT_IN_STRADDLE_LIST"
        if self.env is Environment.SHADOW or self.env is Environment.REAL:
            return f"NOT_AVAILABLE_IN_{self.env}"
        if not getattr(self.broker, "supports_pending", False):
            return "BROKER_HAS_NO_PENDING_ORDER_SUPPORT"
        return None

    # ------------------------------------------------------------------ persistence / recovery
    def _persist(self) -> None:
        try:
            self.repo.set_flag(self.flag_key, {"current": self.cur, "done": self.done_keys[-30:]}, "straddle")
        except Exception as exc:
            log.error("could not persist straddle state: %s", exc)

    def recover(self) -> None:
        """Call after the TradeBook and the startup reconciliation are loaded."""
        try:
            saved = self.repo.get_flag(self.flag_key)
        except Exception as exc:
            log.warning("straddle state unreadable: %s", exc)
            return
        if not isinstance(saved, dict):
            return
        self.done_keys = list(saved.get("done") or [])
        cur = saved.get("current")
        if isinstance(cur, dict) and cur.get("state") in ACTIVE_STATES:
            self.cur = cur
            log.warning("news straddle %s recovered in state %s", cur.get("id"), cur.get("state"))
            self.alert("WARNING", f"News straddle on {self.symbol} recovered after restart ({cur.get('state')})")

    # ------------------------------------------------------------------ main loop hook
    def step(self) -> None:
        if self.cur is not None:
            self._manage()
        else:
            self._cleanup_orphans()
            if self.disabled_reason() is None:
                self._maybe_arm()

    # ------------------------------------------------------------------ arming
    def _skip(self, ev, key: str, reason: str, final: bool = False) -> None:
        self.last_skip = {"event": ev.title, "time": ev.time.isoformat(), "reason": reason, "final": final}
        marker = (key, reason)
        if marker not in self._logged:
            self._logged.add(marker)
            log.info("news straddle on %s waiting/skipped for %s: %s", self.symbol, ev.title, reason)
            self.audit("SKIPPED" if final else "WAITING", f"{ev.title}: {reason}", {"event": key, "reason": reason})
            self.emit("straddle_event", {"symbol": self.symbol, "status": "SKIPPED" if final else "WAITING",
                                         "event": ev.title, "reason": reason})
        if final:
            self._mark_done(key)

    def _mark_done(self, key: str) -> None:
        if key not in self.done_keys:
            self.done_keys.append(key)
        self._persist()

    def _maybe_arm(self) -> None:
        cfg = self.cfg
        now = self.now_utc()
        if not self.calendar.coverage_ok(now):
            return
        ev = self.calendar.next_event(now, getattr(cfg, "currencies", None) or None)
        if ev is None:
            return
        secs = (ev.time - now).total_seconds()
        if secs <= 0 or secs > cfg.arm_minutes_before * 60.0:
            return
        key = event_key(ev)
        if key in self.done_keys:
            return
        if secs < MIN_LEAD_SECONDS:
            self._skip(ev, key, (self.last_skip or {}).get("reason") or "TOO_CLOSE_TO_RELEASE", final=True)
            return
        reason = self._preflight(secs)
        if reason:
            self._skip(ev, key, reason)
            return
        tick = self.tick_fn()
        atr = self.atr_fn()
        failure = self._arm(ev, key, secs, tick, atr)
        if failure:
            self._skip(ev, key, failure, final=failure.startswith("ABORTED"))

    def _preflight(self, secs_to_event: float) -> Optional[str]:
        block = self.block_fn(True)
        if block:
            return block
        tick = self.tick_fn()
        if tick is None:
            return "NO_QUOTE"
        age = self.server_now() - tick.time
        if age > self.cfg.max_atr_quote_age_sec:
            return f"STALE_QUOTE ({age:.0f}s)"
        ok, why = self.spread_ok_fn(tick)
        if not ok:
            return why
        atr = self.atr_fn()
        if atr is None or not math.isfinite(atr) or atr <= 0:
            return "ATR_UNAVAILABLE"
        return None

    def _exit_model(self) -> dict:
        cfg = self.cfg
        return {"type": "fixed", "target_r": round(cfg.tp_atr / cfg.sl_atr, 4),
                "breakeven": {"enabled": True, "activate_r": 0.5, "cover_spread": True},
                "time_stop": {"enabled": True, "max_bars": 12, "min_r": 0.0},
                "thesis_invalidation": False}

    def _arm(self, ev, key: str, secs_to_event: float, tick: Tick, atr: float) -> Optional[str]:
        cfg = self.cfg
        spec = self.spec_fn()
        scale = float(self.vol_scale_fn())      # hourly adaptive volatility scale (1.0 when adaptation is off)
        lv = compute_levels(spec, tick, atr * scale, cfg, self.cfg_fn().execution.protection_safety_buffer_points)
        legs_in = {"BUY": (lv.buy_price, lv.buy_sl, lv.buy_tp), "SELL": (lv.sell_price, lv.sell_sl, lv.sell_tp)}
        exit_model = self._exit_model()
        total_life = secs_to_event + cfg.expire_after_event_minutes * 60.0
        expires_broker = int(self.server_now() + total_life)
        plans, sizes = {}, {}
        for side_name, (price, sl, tp) in legs_in.items():
            side = Side(side_name)
            plans[side_name] = TradePlan(
                direction=side, entry=price, sl=sl, tp=tp, exit_model=exit_model,
                setup_key=f"{STRATEGY_ID}:{key}:{side_name}", quality=0.0, ttl_seconds=int(total_life),
                features={"event": ev.title, "event_time": ev.time.isoformat(), "atr_m15": atr,
                          "vol_scale": scale, "trigger_distance": lv.trigger_distance, "sl_distance": lv.sl_distance,
                          "tp_distance": lv.tp_distance})
            sz = self.size_fn(side, price, sl, cfg.leg_risk_share)
            if not sz.feasible:
                return f"SIZE_{sz.reason}"
            sizes[side_name] = sz
            cost_reason = self.cost_fn(plans[side_name], tick)
            if cost_reason:
                return cost_reason

        sid = new_id()
        ctx = self.context_fn()
        trades: Dict[str, dict] = {}
        signals: Dict[str, Signal] = {}
        for side_name in LEG_SIDES:
            plan = plans[side_name]
            sig = Signal(id=new_id(), strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION,
                         symbol=self.symbol, created_at=utcnow(), created_broker_time=self.server_now(),
                         valid_until_broker=expires_broker, snapshot_id=new_id(), plan=plan,
                         regime=ctx.get("regime", ""), session=ctx.get("session", ""),
                         event_state=ctx.get("event_state", ""), meta={"straddle_id": sid})
            self.insert_signal(sig, "CANDIDATE")
            signals[side_name] = sig
            sz = sizes[side_name]
            trades[side_name] = self.book.create({
                "signal_id": sig.id, "strategy_id": STRATEGY_ID, "strategy_version": STRATEGY_VERSION,
                "symbol": self.symbol, "direction": side_name, "environment": str(self.env),
                "state": str(TradeState.APPROVED), "client_order_id": new_client_order_id(),
                "entry_requested": plan.entry, "initial_sl": plan.sl, "sl": plan.sl, "tp": plan.tp,
                "requested_volume": sz.volume, "initial_risk_money": sz.risk_money,
                "risk_fraction": sz.risk_fraction, "session": ctx.get("session"), "regime": ctx.get("regime"),
                "exit_model": exit_model,
                "extra": {"straddle_id": sid, "leg": side_name, "event": ev.title,
                          "event_time": ev.time.isoformat(), "setup_key": plan.setup_key}})

        def abort(reason: str, state: TradeState = TradeState.CANCELLED) -> str:
            for name in LEG_SIDES:
                self.book.transition(trades[name], state, "STRADDLE_ABORTED", {"reason": reason},
                                     exit_reason=reason)
                self.update_signal(signals[name].id, "REJECTED")
                self.release_risk(signals[name].id)
            return "ABORTED:" + reason

        for name in LEG_SIDES:
            allowed, why = self.reserve_risk(trades[name], sizes[name].risk_money)
            if not allowed:
                return abort(why or "PORTFOLIO_RISK_RESERVATION_REJECTED")

        reqs: Dict[str, PendingOrderRequest] = {}
        for name in LEG_SIDES:
            price, sl, tp = legs_in[name]
            reqs[name] = PendingOrderRequest(
                symbol=self.symbol, side=Side(name), volume=sizes[name].volume, price=price, sl=sl, tp=tp,
                magic=self.magic, comment=trades[name]["client_order_id"], expiration=expires_broker,
                deviation=self.protection_deviation())
            chk = self.broker.pending_check(reqs[name])
            self.book.exec_event(trades[name], signals[name].id, "ORDER_CHECK", order_type="STOP",
                                 requested_state=reqs[name].to_dict(), retcode=chk.retcode,
                                 retcode_text=chk.retcode_text, requested_volume=reqs[name].volume,
                                 requested_price=price, latency_ms=chk.latency_ms,
                                 error=None if chk.ok else chk.comment)
            if not chk.ok:
                return abort(f"ORDER_CHECK_{chk.retcode_text}", TradeState.REJECTED)

        # durable intent BEFORE the irreversible action
        for name in LEG_SIDES:
            self.book.transition(trades[name], TradeState.ORDER_SENT, "STRADDLE_ORDER_SENT",
                                 {"request": reqs[name].to_dict()}, requested_volume=reqs[name].volume)
        cur = {"id": sid, "symbol": self.symbol, "event_key": key, "event_title": ev.title,
               "event_time": ev.time.isoformat(), "state": ARMED, "armed_at": self.wall(),
               "expires_broker": expires_broker, "legs": {}, "note": ""}
        for name in LEG_SIDES:
            cur["legs"][name] = {"side": name, "client_id": trades[name]["client_order_id"],
                                 "trade_id": trades[name]["id"], "signal_id": signals[name].id,
                                 "volume": reqs[name].volume, "price": reqs[name].price, "sl": reqs[name].sl,
                                 "tp": reqs[name].tp, "risk_money": sizes[name].risk_money,
                                 "order_ticket": None, "position_id": None, "state": "PENDING"}
        self.cur = cur
        self._persist()

        placed: List[str] = []
        for name in LEG_SIDES:
            res = self.broker.place_pending(reqs[name])
            self.book.exec_event(trades[name], signals[name].id, "ORDER_RESULT", order_type="STOP",
                                 requested_state=reqs[name].to_dict(),
                                 observed_state={"ok": res.ok, "retcode": res.retcode, "text": res.retcode_text,
                                                 "order": res.order, "ambiguous": res.ambiguous},
                                 retcode=res.retcode, retcode_text=res.retcode_text, order_ticket=res.order or None,
                                 requested_volume=reqs[name].volume, requested_price=reqs[name].price,
                                 latency_ms=res.latency_ms, error=None if res.ok else res.comment)
            leg = cur["legs"][name]
            if res.ok:
                leg["order_ticket"] = int(res.order)
                self.book.update(trades[name], order_ticket=int(res.order),
                                 extra={**trades[name]["extra"], "pending_order_ticket": int(res.order)})
                self.update_signal(signals[name].id, "PENDING_ORDER")
                placed.append(name)
                self._persist()
                continue
            # ---- placement failed
            if res.ambiguous:
                leg["state"] = "UNKNOWN"
                self.book.transition(trades[name], TradeState.RECONCILIATION_REQUIRED, "AMBIGUOUS_ORDER_RESULT",
                                     {"retcode": res.retcode, "text": res.retcode_text})
                self.on_uncertain(trades[name])
            else:
                leg["state"] = "REJECTED"
                self.book.transition(trades[name], TradeState.REJECTED, "ORDER_REJECTED",
                                     {"retcode": res.retcode, "text": res.retcode_text, "comment": res.comment},
                                     exit_reason=f"BROKER_{res.retcode_text}")
                self.update_signal(signals[name].id, "REJECTED")
                self.release_risk(signals[name].id)
            remaining = [n for n in LEG_SIDES if n != name and cur["legs"][n]["order_ticket"] is None]
            for other in remaining:       # never send the second order after the first failed
                cur["legs"][other]["state"] = "CANCELLED"
                self.book.transition(trades[other], TradeState.CANCELLED, "NOT_SENT", {"after": name},
                                     exit_reason="STRADDLE_PARTNER_FAILED")
                self.update_signal(signals[other].id, "CANCELLED")
                self.release_risk(signals[other].id)
            for done in placed:           # a one-sided straddle is not a straddle: pull the order already resting
                self._cancel_leg(cur["legs"][done], "STRADDLE_PARTNER_FAILED")
            self.alert("WARNING", f"News straddle on {self.symbol} aborted: {name} order failed "
                                  f"({res.retcode_text})")
            self._finish("ABORTED_PLACEMENT_FAILED")
            return f"ABORTED:PLACEMENT_FAILED_{res.retcode_text}"

        spread_pts = tick.spread / spec.point if spec.point else None
        self.alert("INFO", f"News straddle ARMED on {self.symbol} for '{ev.title}' in {secs_to_event:.0f}s: "
                           f"BUY STOP {lv.buy_price} / SELL STOP {lv.sell_price}, "
                           f"{reqs['BUY'].volume} + {reqs['SELL'].volume} lots")
        self.audit("ARMED", f"{ev.title}", {"straddle_id": sid, "event": key, "levels": lv.__dict__,
                                            "volumes": {n: reqs[n].volume for n in LEG_SIDES},
                                            "spread_points": spread_pts, "expires_broker": expires_broker})
        self.emit("straddle_event", {"symbol": self.symbol, "status": ARMED, "event": ev.title,
                                     "buy": lv.buy_price, "sell": lv.sell_price})
        return None

    # ------------------------------------------------------------------ management (OCO, stand-down, expiry)
    def _pending_tickets(self) -> Optional[Dict[int, object]]:
        orders = self.broker.pending_orders(self.symbol)
        if orders is None:
            return None
        return {o.ticket: o for o in orders if o.magic == self.magic}

    def _legs(self) -> List[dict]:
        return [self.cur["legs"][n] for n in LEG_SIDES if n in self.cur["legs"]]

    def _trade(self, leg: dict) -> Optional[dict]:
        t = self.book.active.get(leg["trade_id"])
        return t

    def _find_fill(self, leg: dict, positions) -> Optional[dict]:
        for p in positions:
            if p.magic == self.magic and p.comment.startswith(leg["client_id"]):
                return {"position": p, "position_id": p.ticket, "price": p.price_open, "volume": p.volume}
        ticket = leg.get("order_ticket")
        if not ticket:
            return None
        start = datetime.fromtimestamp(max(0.0, self.cur["armed_at"] - 3600), tz=timezone.utc)
        deals = self.broker.history_deals(start, utcnow())
        for d in deals or []:
            if d.order == ticket and d.entry == "IN" and d.magic == self.magic:
                match = next((p for p in positions if p.ticket == d.position_id), None)
                return {"position": match, "position_id": d.position_id, "price": d.price, "volume": d.volume}
        return None

    def _mark_filled(self, leg: dict, fill: dict) -> None:
        leg["state"] = "FILLED"
        leg["position_id"] = int(fill["position_id"])
        trade = self._trade(leg)
        pos = fill.get("position")
        spec = self.spec_fn()
        slip_pts = ((fill["price"] - leg["price"]) * (1 if leg["side"] == "BUY" else -1) / spec.point
                    if spec.point else 0.0)
        self.on_slippage(slip_pts)
        if trade is not None and trade.get("state") in (str(TradeState.ORDER_SENT), str(TradeState.APPROVED),
                                                        str(TradeState.RECONCILIATION_REQUIRED)):
            self.book.transition(
                trade, TradeState.OPEN, "STRADDLE_LEG_FILLED",
                {"position": leg["position_id"], "price": fill["price"], "volume": fill["volume"],
                 "slippage_points": slip_pts},
                position_id=leg["position_id"], filled_volume=fill["volume"], open_volume=fill["volume"],
                entry_price=fill["price"], sl=(pos.sl if pos is not None and pos.sl else leg["sl"]),
                tp=(pos.tp if pos is not None and pos.tp else leg["tp"]), opened_at=utcnow(),
                best_price=fill["price"], worst_price=fill["price"], bars_held=0,
                extra={**(trade.get("extra") or {}),
                       "entry_broker_time": pos.time if pos is not None else int(self.server_now())})
            self.book.ensure_mstate(trade)
            self.book.exec_event(trade, leg["signal_id"], "FILL", position_id=leg["position_id"],
                                 order_ticket=leg.get("order_ticket"), requested_volume=leg["volume"],
                                 filled_volume=fill["volume"], requested_price=leg["price"], price=fill["price"],
                                 slippage_points=slip_pts, observed_state={"leg": leg["side"]})
        self.update_signal(leg["signal_id"], "EXECUTED")
        self.emit("straddle_event", {"symbol": self.symbol, "status": "LEG_FILLED", "leg": leg["side"],
                                     "price": fill["price"]})
        if pos is not None:
            self._ensure_protection(leg, trade, pos)

    def _ensure_protection(self, leg: dict, trade: Optional[dict], pos) -> None:
        if pos.sl and pos.tp:
            return
        res = self.broker.modify_sltp(self.symbol, pos.ticket, pos.sl or leg["sl"], pos.tp or leg["tp"])
        if res.ok:
            return
        self.alert("CRITICAL", f"Straddle leg {leg['side']} on {self.symbol} filled without a full SL/TP and "
                               f"the bracket could not be attached ({res.retcode_text})")
        if self.close_unprotected():
            self.broker.close_position(pos, pos.volume, self.protection_deviation(), self.magic, "unprotected")
            if trade is not None:
                self.on_uncertain(trade)

    def _cancel_leg(self, leg: dict, reason: str) -> bool:
        """Cancel one resting order. Returns True when the leg is confirmed gone-and-unfilled."""
        ticket = leg.get("order_ticket")
        if ticket:
            res = self.broker.cancel_pending(int(ticket), self.symbol, self.magic)
            if not res.ok:
                # the order may have vanished because it just TRIGGERED: re-evaluate before declaring it cancelled
                n = self._cancel_failures.get(leg["client_id"], 0) + 1
                self._cancel_failures[leg["client_id"]] = n
                if n == CANCEL_ALERT_AFTER:
                    self.alert("CRITICAL", f"Cannot cancel straddle {leg['side']} order #{ticket} on "
                                           f"{self.symbol}: {res.retcode_text}. Check MT5 manually.")
                return False
        leg["state"] = "CANCELLED"
        trade = self._trade(leg)
        if trade is not None and trade.get("state") in (str(TradeState.ORDER_SENT), str(TradeState.APPROVED),
                                                        str(TradeState.RECONCILIATION_REQUIRED)):
            self.book.transition(trade, TradeState.CANCELLED, "STRADDLE_LEG_CANCELLED", {"reason": reason},
                                 exit_reason=reason)
        self.update_signal(leg["signal_id"], "CANCELLED")
        self.release_risk(leg["signal_id"])
        self.emit("straddle_event", {"symbol": self.symbol, "status": "LEG_CANCELLED", "leg": leg["side"],
                                     "reason": reason})
        return True

    def cancel_resting(self, reason: str) -> int:
        """Cancel every resting leg now (symbol STOP, shutdown). Filled legs are left to normal management."""
        if self.cur is None:
            return 0
        n = 0
        for leg in self._legs():
            if leg["state"] == "PENDING" and self._cancel_leg(leg, reason):
                n += 1
        if not any(l["state"] == "PENDING" for l in self._legs()):
            self._finish(f"CANCELLED_{reason}" if n else self.cur.get("note") or reason)
        else:
            self._persist()
        return n

    def _stand_down_reason(self) -> Optional[str]:
        block = self.block_fn(False)
        if block:
            return block
        before_release = self.now_utc() < datetime.fromisoformat(self.cur["event_time"])
        if before_release and self.spread_shock_fn():
            return "SPREAD_SHOCK_BEFORE_RELEASE"
        return None

    def _manage(self) -> None:
        cur = self.cur
        pend = self._pending_tickets()
        positions = self.broker.positions(self.symbol)
        if pend is None or positions is None:
            self._unknown_cycles += 1
            if self._unknown_cycles == UNKNOWN_ALERT_AFTER:
                self.alert("CRITICAL", f"News straddle on {self.symbol}: broker state unreadable for "
                                       f"{UNKNOWN_ALERT_AFTER} cycles")
            return
        self._unknown_cycles = 0

        for leg in self._legs():
            if leg["state"] != "PENDING":
                continue
            if leg.get("order_ticket") in pend:
                continue                                 # still resting
            fill = self._find_fill(leg, positions)
            if fill is not None:
                self._mark_filled(leg, fill)
            elif self.server_now() >= cur["expires_broker"]:
                self._mark_gone(leg, "EXPIRED")
            else:
                self._mark_gone(leg, "REMOVED_EXTERNALLY")

        filled = [l for l in self._legs() if l["state"] == "FILLED"]
        resting = [l for l in self._legs() if l["state"] == "PENDING"]
        if len(filled) == 2:
            if cur["state"] != BOTH_FILLED:
                cur["state"] = BOTH_FILLED
                self.alert("WARNING", f"News straddle on {self.symbol}: BOTH legs filled (whipsaw). Both carry "
                                      f"broker-side SL/TP; risk was split between the legs.")
        elif filled and resting:
            if cur["state"] not in (ONE_FILLED,):
                cur["state"] = ONE_FILLED
            for leg in resting:                          # OCO: one-cancels-the-other
                if self._cancel_leg(leg, "OCO_OTHER_LEG_FILLED"):
                    self.alert("INFO", f"OCO on {self.symbol}: {filled[0]['side']} filled, cancelled opposite "
                                       f"{leg['side']} order #{leg.get('order_ticket')}")
        elif filled and not resting and cur["state"] == ARMED:
            cur["state"] = ONE_FILLED
        elif resting:
            reason = self._stand_down_reason()
            if reason is None and self.server_now() >= cur["expires_broker"]:
                reason = "EXPIRED"
            if reason:
                for leg in resting:
                    self._cancel_leg(leg, reason)
                cur["note"] = reason

        if not any(l["state"] == "PENDING" for l in self._legs()):
            self._finish(cur.get("note") or ("FILLED_" + "+".join(l["side"] for l in filled) if filled
                                              else "NO_FILL"))
        else:
            self._persist()

    def _mark_gone(self, leg: dict, reason: str) -> None:
        leg["state"] = "EXPIRED" if reason == "EXPIRED" else "CANCELLED"
        trade = self._trade(leg)
        if trade is not None and trade.get("state") in (str(TradeState.ORDER_SENT), str(TradeState.APPROVED),
                                                        str(TradeState.RECONCILIATION_REQUIRED)):
            self.book.transition(trade, TradeState.CANCELLED, "STRADDLE_LEG_" + reason, {}, exit_reason=reason)
        self.update_signal(leg["signal_id"], "EXPIRED" if reason == "EXPIRED" else "CANCELLED")
        self.release_risk(leg["signal_id"])

    def _finish(self, outcome: str) -> None:
        cur = self.cur
        if cur is None:
            return
        self.last_outcome = {"event": cur["event_title"], "event_time": cur["event_time"], "outcome": outcome,
                             "legs": {l["side"]: l["state"] for l in self._legs()}}
        self.audit("FINISHED", f"{cur['event_title']}: {outcome}", {"straddle_id": cur["id"],
                                                                     "legs": self.last_outcome["legs"]})
        self.emit("straddle_event", {"symbol": self.symbol, "status": "FINISHED", "event": cur["event_title"],
                                     "outcome": outcome})
        if cur["event_key"] not in self.done_keys:
            self.done_keys.append(cur["event_key"])
        self.cur = None
        self._cancel_failures.clear()
        self._persist()

    # ------------------------------------------------------------------ orphan clean-up
    def _cleanup_orphans(self) -> None:
        """No straddle is active, so no resting order of ours should exist (only the straddle places any)."""
        if not getattr(self.broker, "supports_pending", False):
            return
        now = self.wall()
        if now - self._last_cleanup < CLEANUP_INTERVAL_SEC:
            return
        self._last_cleanup = now
        try:
            pend = self._pending_tickets()
        except Exception as exc:
            log.warning("pending order listing failed: %s", exc)
            return
        for ticket, order in (pend or {}).items():
            if not order.comment.startswith("ATA"):
                continue
            res = self.broker.cancel_pending(int(ticket), self.symbol, self.magic)
            self.alert("WARNING", f"Orphan pending order #{ticket} ({order.side} {order.symbol} @ {order.price}) "
                                  f"found with no active straddle - {'cancelled' if res.ok else 'CANCEL FAILED'}")
            trade = self.book.by_client_id(order.comment[:15])
            if trade is not None and trade.get("state") in (str(TradeState.ORDER_SENT), str(TradeState.APPROVED),
                                                            str(TradeState.RECONCILIATION_REQUIRED)):
                if res.ok:
                    self.book.transition(trade, TradeState.CANCELLED, "ORPHAN_PENDING_CANCELLED", {},
                                         exit_reason="ORPHAN_PENDING_CANCELLED")

    # ------------------------------------------------------------------ dashboard
    def is_armed(self) -> bool:
        """True while this symbol holds straddle legs the broker has not finished with.

        Used by the engine to cap how many SYMBOLS may be armed at once, separately from the ordinary
        open-position limit. Never raises: a straddle that cannot report its own state counts as armed,
        which is the conservative direction (it withholds a new arming rather than permitting an extra one).
        """
        try:
            return bool(self.cur) and str(self.cur.get("state")) in (ARMED, ARMED_ONE_SIDED, ONE_FILLED)
        except Exception:
            log.debug("could not read straddle state for %s", self.symbol, exc_info=True)
            return True

    def status(self) -> dict:
        reason = self.disabled_reason()
        out = {"symbol": self.symbol, "active": reason is None, "disabled_reason": reason,
               "state": self.cur["state"] if self.cur else "IDLE", "last_outcome": self.last_outcome,
               "last_skip": self.last_skip}
        if self.cur:
            out.update({"event": self.cur["event_title"], "event_time": self.cur["event_time"],
                        "legs": {l["side"]: {"state": l["state"], "price": l["price"], "volume": l["volume"],
                                             "ticket": l.get("order_ticket")} for l in self._legs()}})
        try:
            nxt = self.calendar.next_event(self.now_utc(), getattr(self.cfg, "currencies", None) or None)
        except Exception:
            nxt = None
        out["next_event"] = None if nxt is None else {"time": nxt.time.isoformat(), "title": nxt.title}
        return out
