"""Live exit management (Exit A-G) on top of broker-resident protection.

The broker always holds a stop. Python only TIGHTENS it, takes partials, or
closes on time-stop / thesis invalidation. If the broker already closed the
position, we never retry: we reconcile and record the attribution.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Dict, Optional

from app.alpha.exits import (breakeven_sl, improves, on_bar_close, partial_level, profit_lock_enabled,
                             profit_lock_target)
from app.broker.base import BrokerAdapter
from app.core.types import Bar, OrderResult, Position, Side, SymbolSpec, Tick, TradeState
from app.execution.book import TradeBook
from app.execution.protection import minimum_protection_distance, protection_distance_error
from app.execution.reconciliation import Reconciler
from app.features.frame import FeatureFrame

log = logging.getLogger(__name__)

# A retcode of our own, outside MT5's range (10000-10046), so a self-imposed wait can never be mistaken for
# something the broker said. MT5's own 10024 means the BROKER is rate-limiting; this means we are.
RETCODE_SELF_COOLDOWN = 90024
# Floor on the gap between stop moves. 5s was hard-coded; it is now a floor on the CONFIGURED value, so the
# cooldown can be shortened if measurement ever justifies it - but not to zero, because each attempt consumes
# the window and a storm of trivial nudges would crowd out the move that matters.
MIN_SL_COOLDOWN_SEC = 1.0


class TradeManager:
    def __init__(self, broker: BrokerAdapter, book: TradeBook, reconciler: Reconciler, symbol: str, magic: int,
                 deviation: int, spec_fn: Callable[[], SymbolSpec], on_uncertain: Callable[[dict], None],
                 quick_profit_close_money: float = 0.0, profit_lock_floor_money: float = 0.05,
                 commission_per_lot_round_trip: float = 0.0, slippage_points: float = 5.0,
                 profit_lock_stop_buffer_points: float = 3.0, hard_profit_target_money: float = 1.0, profit_giveback_fraction: float = 0.0,
                 small_win_activation_money: float = 0.20, small_win_floor_money: float = 0.05,
                 ambition_activation_money: float = 0.90, ambition_floor_money: float = 0.50,
                 protection_safety_buffer_points: float = 5.0, sl_modification_cooldown_sec: float = 5.0,
                 close_when_profit_lock_unprotectable: bool = False,
                 clock: Optional[Callable[[], float]] = None):
        self.broker = broker
        self.book = book
        self.recon = reconciler
        self._clock = clock or time.monotonic
        # last (mfe_r, mae_r) written to the database per trade, so _persist_extremes can skip a write when
        # the extremes have not moved far enough to be worth one
        self._last_persisted_extremes: Dict[str, tuple] = {}
        self.symbol = symbol
        self.magic = magic
        self.deviation = deviation
        self.spec_fn = spec_fn
        self.on_uncertain = on_uncertain
        self.profit_trail_activation_money = max(0.0, float(quick_profit_close_money))
        self.profit_lock_floor_money = max(0.0, float(profit_lock_floor_money))
        self.commission_per_lot_round_trip = max(0.0, float(commission_per_lot_round_trip))
        self.slippage_points = max(0.0, float(slippage_points))
        self.profit_lock_stop_buffer_points = max(0.0, float(profit_lock_stop_buffer_points))
        self.hard_profit_target_money = max(0.0, float(hard_profit_target_money))
        self.profit_giveback_fraction = min(0.9, max(0.0, float(profit_giveback_fraction)))
        self.small_win_activation_money = max(0.0, float(small_win_activation_money))
        self.small_win_floor_money = max(0.0, float(small_win_floor_money))
        self.ambition_activation_money = max(0.0, float(ambition_activation_money))
        self.ambition_floor_money = max(0.0, float(ambition_floor_money))
        # fractions of the trade's initial risk; the larger of money and R is used (set by the core)
        self.floor_r: dict = {}
        # smallest stop improvement worth a broker round trip, as a fraction of initial risk (set by the core)
        self.min_sl_step_r: float = 0.0
        # the protected floor never drops below this multiple of estimated round-trip cost (set by the core)
        self.profit_lock_cost_multiple: float = 1.5
        self.protection_safety_buffer_points = max(0.0, float(protection_safety_buffer_points))
        self.sl_modification_cooldown_sec = max(5.0, float(sl_modification_cooldown_sec))
        self.close_when_profit_lock_unprotectable = bool(close_when_profit_lock_unprotectable)
        self._last_sl_modification_at: dict[int, float] = {}
        self._last_frozen_close_at: dict[int, float] = {}

    def _level(self, trade: dict, name: str, default_money: float) -> float:
        """A profit-floor level in money: max(configured money, configured R x this trade's initial risk)."""
        money = float(getattr(self, f"{name}_money", default_money))
        r = float((getattr(self, "floor_r", None) or {}).get(name, 0.0) or 0.0)
        try:
            risk = float(trade.get("initial_risk_money") or 0.0)
        except (TypeError, ValueError):
            risk = 0.0
        return max(money, r * risk) if r > 0 and risk > 0 else money

    def _position(self, trade: dict):
        positions = self.broker.positions(self.symbol)
        if positions is None:
            return None, False
        for p in positions:
            if trade.get("position_id") and p.ticket == int(trade["position_id"]):
                return p, True
        return None, True

    def _handle_gone(self, trade: dict) -> None:
        deals = self.recon._deals_for(trade)
        if not deals or not self.recon.finalize_closed(trade, deals):
            self.book.transition(trade, TradeState.RECONCILIATION_REQUIRED, "ALREADY_CLOSED_UNCONFIRMED", {})
            self.on_uncertain(trade)

    def close(self, trade: dict, fraction: float, reason: str) -> bool:
        pos, known = self._position(trade)
        if not known:
            self.on_uncertain(trade)
            return False
        if pos is None:
            self._handle_gone(trade)
            return False
        spec = self.spec_fn()
        vol = pos.volume if fraction >= 0.999 else spec.floor_volume(pos.volume * fraction)
        if fraction < 0.999:
            remaining = round(pos.volume - vol, 8)
            if vol < spec.volume_min or remaining < spec.volume_min:
                log.info("partial close skipped for %s: volume %.2f cannot be split", trade["id"][:8], pos.volume)
                return False
        else:
            trade["pending_exit_reason"] = reason
        ticket = int(pos.ticket)
        frozen_attempts = getattr(self, "_last_frozen_close_at", {})
        now = getattr(self, "_clock", time.monotonic)()
        cooldown = max(MIN_SL_COOLDOWN_SEC, float(getattr(self, "sl_modification_cooldown_sec", 5.0)))
        last_frozen = frozen_attempts.get(ticket)
        if last_frozen is not None and now - last_frozen < cooldown:
            remaining = cooldown - (now - last_frozen)
            self.book.exec_event(      # again ours, not the broker's - see the note in _modify_sl
                trade, trade.get("signal_id"), "CLOSE_REQUEST", position_id=ticket,
                requested_volume=vol, retcode=RETCODE_SELF_COOLDOWN, retcode_text="OWN_COOLDOWN",
                requested_state={"reason": reason, "fraction": fraction, "cooldown_remaining_sec": remaining},
                error="Previous close request was rejected as FROZEN; retry deferred",
            )
            log.warning("close %s retry deferred for %.1fs after broker FROZEN response", ticket, remaining)
            return False
        res = self.broker.close_position(pos, vol, self.deviation, self.magic, reason[:20])
        self.book.exec_event(trade, trade.get("signal_id"), "CLOSE_REQUEST", position_id=pos.ticket,
                             requested_volume=vol, filled_volume=res.volume or None, retcode=res.retcode,
                             retcode_text=res.retcode_text, price=res.price or None, latency_ms=res.latency_ms,
                             requested_state={"reason": reason, "fraction": fraction},
                             error=None if res.ok else res.comment)
        if res.already_closed:
            self._handle_gone(trade)
            return False
        if res.ambiguous:
            self.book.transition(trade, TradeState.RECONCILIATION_REQUIRED, "AMBIGUOUS_CLOSE", {"reason": reason})
            self.on_uncertain(trade)
            return False
        if not res.ok:
            if res.retcode == 10029:
                frozen_attempts[ticket] = now
                self._last_frozen_close_at = frozen_attempts
            log.warning("close %s failed: %s", pos.ticket, res.retcode_text)
            return False
        if fraction >= 0.999:
            deals = self.recon._deals_for(trade) or []
            if not self.recon.finalize_closed(trade, deals, reason_override=reason):
                self.book.transition(trade, TradeState.RECONCILIATION_REQUIRED, "CLOSE_DEAL_NOT_VISIBLE", {})
                self.on_uncertain(trade)
            return True
        st = self.book.mstate.get(trade["id"])
        new_open = round(pos.volume - (res.volume or vol), 8)
        self.book.transition(trade, TradeState.PARTIAL, "PARTIAL_CLOSE",
                             {"volume": res.volume or vol, "price": res.price, "reason": reason},
                             open_volume=new_open, partial_done=True)
        if st is not None:
            st.volume = new_open
        return True

    def modify_sl(self, trade: dict, new_sl: float, reason: str) -> bool:
        return self._modify_sl_result(trade, new_sl, reason).ok

    def _modify_sl_result(self, trade: dict, new_sl: float, reason: str,
                          event_type: str = "SL_CHANGE", new_tp: Optional[float] = None) -> OrderResult:
        ticket = int(trade["position_id"])
        tp = float(new_tp if new_tp is not None else (trade.get("tp") or 0.0))
        last_attempts = getattr(self, "_last_sl_modification_at", {})
        now = getattr(self, "_clock", time.monotonic)()
        # OUR cooldown, not the broker's. It used to be reported as retcode 10024 / "THROTTLED", which is
        # MT5's code for the broker rate-limiting us - so the logs and the dashboard blamed Exness for a
        # limit this code imposes on itself. On 16 Oct that mislabel hid the fact that 120 of 217 stop moves
        # (55%) were refused here, by us. The reason for the cooldown is sound: an attempt consumes the
        # window, so a trivial nudge can block the meaningful move behind it. Only the name was wrong.
        cooldown = max(MIN_SL_COOLDOWN_SEC, float(getattr(self, "sl_modification_cooldown_sec", 5.0)))
        last_attempt = last_attempts.get(ticket)
        if last_attempt is not None and now - last_attempt < cooldown:
            remaining = cooldown - (now - last_attempt)
            res = OrderResult(ok=False, retcode=RETCODE_SELF_COOLDOWN, retcode_text="OWN_COOLDOWN",
                              comment=f"our own stop-move cooldown, {remaining:.2f}s of {cooldown:.0f}s left")
        else:
            last_attempts[ticket] = now
            self._last_sl_modification_at = last_attempts
            spec = self.spec_fn()
            new_sl = spec.round_price(new_sl)
            live_tick = (self.broker.tick(self.symbol) if hasattr(self.broker, "tick")
                         else getattr(self, "_last_tick", None))
            if live_tick is None:
                res = OrderResult(ok=False, retcode=10021, retcode_text="PRICE_OFF",
                                  comment="No live quote available for protective-stop validation")
            else:
                minimum_distance = minimum_protection_distance(
                    spec, live_tick.spread, getattr(self, "protection_safety_buffer_points", 5.0)
                )
                distance_error = protection_distance_error(
                    Side(trade["direction"]), live_tick, new_sl, tp, minimum_distance
                )
                if distance_error:
                    res = OrderResult(ok=False, retcode=10016, retcode_text="LOCAL_STOP_DISTANCE",
                                      comment=distance_error)
                else:
                    res = self.broker.modify_sltp(self.symbol, ticket, new_sl, tp)
        self.book.exec_event(trade, trade.get("signal_id"), event_type, position_id=trade.get("position_id"),
                             retcode=res.retcode, retcode_text=res.retcode_text, latency_ms=res.latency_ms,
                             requested_state={"sl": new_sl, "tp": tp, "reason": reason},
                             error=None if res.ok else res.comment)
        if res.already_closed:
            self._handle_gone(trade)
            return res
        if res.ambiguous:
            self.book.transition(trade, TradeState.RECONCILIATION_REQUIRED, "AMBIGUOUS_SL_CHANGE",
                                 {"reason": reason, "sl": new_sl})
            self.on_uncertain(trade)
            return res
        if not res.ok:
            return res
        st = self.book.mstate.get(trade["id"])
        if st is not None:
            st.sl = new_sl
        protected = (new_sl - float(trade["entry_price"])) * Side(trade["direction"]).sign >= 0
        new_state = trade["state"]
        if protected and trade["state"] in (str(TradeState.OPEN),):
            new_state = str(TradeState.PROTECTION)
        elif trade["state"] == str(TradeState.PARTIAL):
            new_state = str(TradeState.RUNNER)
        self.book.transition(trade, TradeState(new_state), "SL_MOVED", {"sl": new_sl, "reason": reason},
                             sl=new_sl, protected=bool(protected or trade.get("protected")))
        return res

    def _ensure_bracket(self, trade: dict) -> Optional[Position]:
        positions = self.broker.positions(self.symbol)
        if positions is None:
            self.on_uncertain(trade)
            return None
        pos = next((p for p in positions if trade.get("position_id")
                    and p.ticket == int(trade["position_id"])), None)
        if pos is None:
            self.on_uncertain(trade)
            return None
        sl = float(trade.get("sl") or trade.get("initial_sl") or pos.sl or 0.0)
        tp = float(trade.get("tp") or pos.tp or 0.0)
        if pos.sl > 0 and pos.tp > 0:
            return pos
        if sl <= 0 or tp <= 0:
            log.error("Trade %s is missing a recorded protective bracket; closing position %s",
                      trade["id"][:8], pos.ticket)
            self.close(trade, 1.0, "MISSING_PROTECTIVE_BRACKET")
            return None
        result = self._modify_sl_result(trade, sl, "PROTECTIVE_BRACKET_REPAIR",
                                        event_type="ATTACH_SL", new_tp=tp)
        if result.ok:
            self.book.update(trade, sl=sl, tp=tp)
            return pos
        log.error("Could not restore protective bracket on position %s: %s; closing position",
                  pos.ticket, result.retcode_text)
        self.close(trade, 1.0, "PROTECTIVE_BRACKET_REPAIR_FAILED")
        return None

    def _profit_lock_cost_estimate(self, pos: Position) -> Optional[float]:
        """Estimate configured round-trip commission and adverse exit slippage."""
        spec = self.spec_fn()
        slip_distance = getattr(self, "slippage_points", 5.0) * spec.point
        commission = getattr(self, "commission_per_lot_round_trip", 0.0) * float(pos.volume)
        if slip_distance <= 0:
            return commission
        side = Side(pos.side)
        adverse_exit = float(pos.price_open) - side.sign * slip_distance
        slippage_pnl = self.broker.calc_profit(
            pos.symbol, side, pos.volume, pos.price_open, adverse_exit
        )
        if slippage_pnl is None:
            log.error("Broker could not estimate profit-lock execution costs for %s", pos.symbol)
            return None
        return commission + max(0.0, -float(slippage_pnl))

    def _profit_floor_price(self, pos: Position, floor_money: float,
                            estimated_exit_costs: float = 0.0) -> Optional[float]:
        """Find a broker-valid price whose estimated P/L reaches the configured floor."""
        spec = self.spec_fn()
        side = Side(pos.side)
        target = max(0.0, floor_money + estimated_exit_costs - float(pos.swap))
        step = max(spec.tick_size, spec.point)
        if step <= 0:
            log.error("Cannot calculate profit-lock stop for %s: invalid symbol price step", pos.symbol)
            return None

        def pnl(price: float) -> Optional[float]:
            return self.broker.calc_profit(pos.symbol, side, pos.volume, pos.price_open, price)

        entry = float(pos.price_open)
        if side is Side.BUY:
            low, high = entry, entry + step
            for _ in range(40):
                value = pnl(high)
                if value is None:
                    log.error("Broker could not estimate profit-lock stop for %s", pos.symbol)
                    return None
                if value >= target:
                    break
                high = entry + (high - entry) * 2.0
            else:
                log.error("Could not bracket profit-lock stop for %s", pos.symbol)
                return None
            for _ in range(48):
                mid = (low + high) / 2.0
                value = pnl(mid)
                if value is None:
                    log.error("Broker could not estimate profit-lock stop for %s", pos.symbol)
                    return None
                if value >= target:
                    high = mid
                else:
                    low = mid
            candidate = spec.round_price(high)
            if (value := pnl(candidate)) is None:
                log.error("Broker could not verify profit-lock stop for %s", pos.symbol)
                return None
            for _ in range(4):
                if value >= target:
                    break
                candidate = spec.round_price(candidate + step)
                if (value := pnl(candidate)) is None:
                    log.error("Broker could not verify profit-lock stop for %s", pos.symbol)
                    return None
            if value < target:
                log.error("Rounded profit-lock stop does not reach floor for %s", pos.symbol)
                return None
        else:
            low, high = entry - step, entry
            for _ in range(40):
                if low <= 0:
                    log.error("Could not bracket profit-lock stop for %s", pos.symbol)
                    return None
                value = pnl(low)
                if value is None:
                    log.error("Broker could not estimate profit-lock stop for %s", pos.symbol)
                    return None
                if value >= target:
                    break
                low = entry - (entry - low) * 2.0
            else:
                log.error("Could not bracket profit-lock stop for %s", pos.symbol)
                return None
            for _ in range(48):
                mid = (low + high) / 2.0
                value = pnl(mid)
                if value is None:
                    log.error("Broker could not estimate profit-lock stop for %s", pos.symbol)
                    return None
                if value >= target:
                    low = mid
                else:
                    high = mid
            candidate = spec.round_price(low)
            if (value := pnl(candidate)) is None:
                log.error("Broker could not verify profit-lock stop for %s", pos.symbol)
                return None
            for _ in range(4):
                if value >= target:
                    break
                candidate = spec.round_price(candidate - step)
                if candidate <= 0 or (value := pnl(candidate)) is None:
                    log.error("Broker could not verify profit-lock stop for %s", pos.symbol)
                    return None
            if value < target:
                log.error("Rounded profit-lock stop does not reach floor for %s", pos.symbol)
                return None
        return candidate

    # ------------------------------------------------------------------ hooks
    def on_tick(self, tick: Tick) -> None:
        self._last_tick = tick
        for trade in list(self.book.open_positions()):
            pos = self._ensure_bracket(trade)
            if pos is None:
                continue
            floating_profit = float(pos.profit) + float(pos.swap)
            st = self.book.ensure_mstate(trade)
            if st is None:
                continue
            estimated_exit_costs: Optional[float] = 0.0
            hard_target = getattr(self, "hard_profit_target_money", 0.0)
            estimated_net_profit = floating_profit
            previous_lock_target = st.profit_lock_target_money
            was_lock_active = st.profit_lock_active
            activation_threshold = max(self.profit_trail_activation_money,
                                       self.profit_lock_floor_money)
            if (hard_target > 0 or self.profit_trail_activation_money > 0 or st.profit_lock_active
                    or getattr(self, "small_win_activation_money", 0.20) > 0):
                estimated_exit_costs = self._profit_lock_cost_estimate(pos)
            if estimated_exit_costs is not None:
                estimated_net_profit = floating_profit - estimated_exit_costs
                previous_high = st.profit_high_watermark_money
                st.profit_high_watermark_money = max(previous_high, estimated_net_profit)
                if st.profit_high_watermark_money > previous_high + 1e-9:
                    extra = dict(trade.get("extra") or {})
                    extra["profit_high_watermark_money"] = st.profit_high_watermark_money
                    self.book.update(trade, extra=extra)
                if hard_target > 0 and estimated_net_profit >= hard_target:
                    self.close(trade, 1.0, "HARD_PROFIT_TARGET")
                    continue
                activation_threshold = max(
                    self.profit_trail_activation_money,
                    self.profit_lock_floor_money + 2.0 * estimated_exit_costs,
                )
                small_activation = self._level(trade, "small_win_activation", 0.20)
                small_floor = self._level(trade, "small_win_floor", 0.05)
                ambition_activation = self._level(trade, "ambition_activation", 0.90)
                ambition_floor = self._level(trade, "ambition_floor", 0.50)
                # The ladder itself is app/alpha/exits.py::profit_lock_target, shared with the backtester and the
                # counterfactual tracker so research judges the exit that actually trades. Both rungs trail the
                # peak at profit_giveback_fraction (a $10 peak on an $18.97 risk locks $7.00, not a flat $1.90),
                # and the target is maxed against the previous one, so the floor only ever rises.
                # A strategy whose exit model says profit_lock.enabled = false (trend following) is never locked:
                # banking 70% of a 0.5R peak would end every trend long before it pays.
                if profit_lock_enabled(trade.get("exit_model")):
                    lock_active, lock_target = profit_lock_target(
                        st.profit_high_watermark_money, small_activation, small_floor, ambition_activation,
                        ambition_floor, getattr(self, "profit_giveback_fraction", 0.0),
                        previous_target=st.profit_lock_target_money, was_active=st.profit_lock_active,
                        base_floor=self.profit_lock_floor_money)
                else:
                    lock_active, lock_target = False, self.profit_lock_floor_money
                # A floor below the round-trip cost is not protection: the close books a LOSS. Seven exits on
                # 14-15 Oct came out at -0.08, -0.08, -0.12, -0.07, -0.08, -0.00 and -0.19R with the reason
                # SMALL_WIN_PROFIT_FLOOR, because the base rung locks a flat profit_lock_floor_money ($0.05)
                # that the spread alone swallows. The floor is now held at a multiple of this trade's own
                # estimated exit cost, so firing it can only ever realise a positive result.
                if estimated_exit_costs is not None and lock_active:
                    cost_floor = float(estimated_exit_costs) * float(
                        getattr(self, "profit_lock_cost_multiple", 1.5))
                    if cost_floor > lock_target:
                        lock_target = cost_floor
                if lock_active:
                    st.profit_lock_active = True
                    st.profit_lock_target_money = lock_target
                    if (not was_lock_active or previous_lock_target is None
                            or lock_target > previous_lock_target + 1e-9):
                        extra = dict(trade.get("extra") or {})
                        extra["profit_lock_active"] = True
                        extra["profit_lock_target_money"] = lock_target
                        self.book.update(trade, extra=extra)
            fav = tick.bid if st.side is Side.BUY else tick.ask
            st.observe(fav, fav)
            self._persist_extremes(trade, st)
            if (estimated_exit_costs is not None and self.profit_trail_activation_money > 0
                    and not st.profit_lock_active
                    and floating_profit >= activation_threshold):
                st.profit_lock_active = True
                extra = dict(trade.get("extra") or {})
                extra["profit_lock_active"] = True
                self.book.update(trade, extra=extra)
            if st.profit_lock_active and estimated_exit_costs is not None:
                lock_target = st.profit_lock_target_money or self.profit_lock_floor_money
                if estimated_net_profit <= lock_target + 1e-9:
                    reason = ("AMBITION_PROFIT_FLOOR"
                              if st.profit_high_watermark_money >= self._level(trade, "ambition_activation", 0.90)
                              else "SMALL_WIN_PROFIT_FLOOR")
                    self.close(trade, 1.0, reason)
                    continue
                candidate = st.profit_lock_stop
                if candidate is None or lock_target > (previous_lock_target or 0.0) + 1e-9:
                    candidate = self._profit_floor_price(pos, lock_target,
                                                        estimated_exit_costs)
                    if candidate is not None:
                        st.profit_lock_stop = candidate
                        extra = dict(trade.get("extra") or {})
                        extra["profit_lock_stop_price"] = candidate
                        self.book.update(trade, extra=extra)
                if candidate is not None:
                    spec = self.spec_fn()
                    min_distance = ((spec.stops_level + spec.freeze_level)
                                    * spec.point
                                    + getattr(self, "profit_lock_stop_buffer_points", 3.0) * spec.point)
                    clear_of_market = (candidate < tick.bid - min_distance if st.side is Side.BUY
                                       else candidate > tick.ask + min_distance)
                    if improves(st, candidate, getattr(self, "min_sl_step_r", 0.0)) and clear_of_market:
                        result = self._modify_sl_result(trade, candidate, "DYNAMIC_PROFIT_FLOOR")
                        if not result.ok and not result.already_closed and not result.ambiguous and result.retcode != RETCODE_SELF_COOLDOWN:
                            log.warning("Profit-lock stop change failed for %s (%s): %s",
                                        pos.symbol, result.retcode_text, result.comment)
                            current_pos, known = self._position(trade)
                            if not known or current_pos is None:
                                log.error("Cannot verify live profit before fallback close for %s",
                                          pos.symbol)
                                if not known:
                                    self.on_uncertain(trade)
                            else:
                                current_costs = self._profit_lock_cost_estimate(current_pos)
                                current_net_profit = (
                                    float(current_pos.profit) + float(current_pos.swap) - current_costs
                                    if current_costs is not None else None
                                )
                                if current_costs is None:
                                    log.error(
                                        "Cannot estimate live exit costs for %s; fallback close skipped "
                                        "and the existing broker stop is retained",
                                        pos.symbol,
                                    )
                                elif (current_net_profit is not None
                                      and current_net_profit >= lock_target - 1e-9):
                                    if getattr(self, "close_when_profit_lock_unprotectable", False):
                                        self.close(trade, 1.0, "DYNAMIC_PROFIT_FLOOR_MODIFY_FAILED")
                                    else:
                                        # profit is still above the floor and the existing broker stop is in place:
                                        # keep the winner and try to tighten again on a later tick
                                        log.info("Profit-lock stop change failed for %s but net P/L %.2f is still "
                                                 "above the floor %.2f - position kept, retrying later",
                                                 pos.symbol, current_net_profit, lock_target)
                                else:
                                    log.error(
                                        "Profit-lock modification failed for %s; estimated net P/L is "
                                        "below the positive floor, so the existing broker stop is retained",
                                        pos.symbol,
                                    )
            model = trade.get("exit_model") or {}
            lvl = partial_level(st, model)
            breakeven = model.get("breakeven") or {}
            activate_r = float(breakeven.get("activate_r", 0.5))
            if breakeven.get("enabled", True) and st.mfe_r >= activate_r:
                spec = self.spec_fn()
                offset = max(tick.spread, 5 * spec.point) if breakeven.get("cover_spread", True) else 0.0
                candidate = breakeven_sl(st, offset)
                min_distance = (spec.stops_level + spec.freeze_level) * spec.point
                clear_of_market = (candidate < tick.bid - min_distance if st.side is Side.BUY
                                   else candidate > tick.ask + min_distance)
                if improves(st, candidate, getattr(self, "min_sl_step_r", 0.0)) and clear_of_market:
                    self.modify_sl(trade, candidate, f"BREAKEVEN_{activate_r:g}R")
            if lvl is not None and (fav - lvl) * st.side.sign >= 0:
                frac = float((model.get("partial") or {}).get("fraction", 0.5))
                if self.close(trade, frac, "PARTIAL_PROFIT"):
                    st.partial_done = True
                    st.realized_r_partial += frac * st.r(fav)
                    st.volume = float(trade.get("open_volume") or st.volume)
                    if (model.get("partial") or {}).get("move_sl_to_be", True):
                        be = breakeven_sl(st, 5 * float(self.spec_fn().point))
                        if improves(st, be, getattr(self, "min_sl_step_r", 0.0)):
                            self.modify_sl(trade, be, "BREAKEVEN_AFTER_PARTIAL")   # st.sl set only on broker OK
                else:
                    st.partial_done = True   # volume cannot be split: run the full position

    def _persist_extremes(self, trade: dict, st) -> None:
        """Write the best and worst price this trade has seen, as soon as they move.

        They used to be saved only in on_bar_close, once every 15 minutes. A trade makes its final run and
        closes BETWEEN bar closes - that is exactly what happens when price runs into a target - so the saved
        peak was whatever it had been up to a quarter of an hour earlier. On 16 Oct a GBPJPY trade closed at
        its target for +1.51R with its peak recorded as +0.30R, which is not merely imprecise but impossible.

        The damage went further than one wrong column. The close path prefers the in-memory state but falls
        back to the stored value whenever that state is missing - after a restart, or when the position was
        briefly unreadable - so a stale number became the permanent record. Every MFE-based conclusion drawn
        from this database, including "these losses never went into profit, so there was nothing to protect",
        was computed on numbers that could only understate.

        Writes are throttled by peak_persist_step_r (a fraction of the trade's own risk), so a position
        drifting tick by tick does not generate a write per tick: at the 0.02R default a full 1R run costs
        about fifty writes, against the ~1,100 ticks it would otherwise take.
        """
        try:
            step = float(getattr(self, "peak_persist_step_r", 0.02))
            last = self._last_persisted_extremes.get(trade["id"])
            mfe, mae = st.mfe_r, st.mae_r
            if last is not None and abs(mfe - last[0]) < step and abs(mae - last[1]) < step:
                return
            self._last_persisted_extremes[trade["id"]] = (mfe, mae)
            if len(self._last_persisted_extremes) > 512:      # a long-lived process must not grow a leak here
                live = {t["id"] for t in self.book.open_positions()}
                self._last_persisted_extremes = {k: v for k, v in self._last_persisted_extremes.items()
                                                 if k in live}
            self.book.update(trade, best_price=st.best_price, worst_price=st.worst_price,
                             mfe_r=mfe, mae_r=mae)
        except Exception:      # recording how far a trade ran must never interfere with managing it
            log.debug("could not persist trade extremes for %s", trade.get("id"), exc_info=True)

    def on_bar_close(self, bar: Bar, ff: FeatureFrame, i: int, regime_trend: str, spread_price: float) -> None:
        spec = self.spec_fn()
        for trade in list(self.book.open_positions()):
            st = self.book.ensure_mstate(trade)
            if st is None or trade.get("strategy_id") == "UNKNOWN":
                continue
            if bar.time + 900 <= st.entry_time:
                continue
            st.bars_held += 1
            if st.side is Side.BUY:
                st.observe(bar.high, bar.low)
                exit_px = bar.close
            else:
                st.observe(bar.low + spread_price, bar.high + spread_price)
                exit_px = bar.close + spread_price
            self.book.update(trade, bars_held=st.bars_held, best_price=st.best_price, worst_price=st.worst_price,
                             mfe_r=st.mfe_r, mae_r=st.mae_r)
            min_stop = (spec.stops_level + spec.freeze_level) * spec.point
            dec = on_bar_close(st, trade.get("exit_model") or {}, ff, i, regime_trend, exit_px, min_stop)
            if dec.close_fraction >= 1.0:
                self.close(trade, 1.0, dec.reason)
            elif dec.new_sl is not None:
                self.modify_sl(trade, dec.new_sl, dec.reason)
