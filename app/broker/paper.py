"""Paper broker: real (or simulated) market data, simulated execution.

Used for DRY_RUN with live MT5 data, and - wrapped around SimMarket - as a
stand-in "real broker" in integration and chaos tests. It behaves like an MT5
server: positions live here, SL/TP are enforced on ticks, every fill produces
deals, and its state can be persisted so a restart of the trading core finds
the same positions (the recovery invariant).
"""
from __future__ import annotations

import itertools
import threading
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from app.broker.base import BrokerAdapter, BrokerError
from app.core.types import (AccountInfo, Bar, Deal, OrderRequest, OrderResult, PendingOrder, PendingOrderRequest,
                            Position, Side, SymbolSpec, Tick)


class PaperBroker(BrokerAdapter):
    name = "paper"
    supports_pending = True

    def __init__(self, data: BrokerAdapter, starting_balance: float = 100.0, slippage_points: float = 0.0,
                 account_mode: str = "PAPER", currency: str = "USD", leverage: int = 500,
                 state_saver: Optional[Callable[[dict], None]] = None):
        self.data = data
        self.balance = float(starting_balance)
        self.slippage_points = slippage_points
        self.account_mode = account_mode
        self.currency = currency
        self.leverage = leverage
        self._positions: Dict[int, Position] = {}
        self._pending: Dict[int, PendingOrder] = {}
        self._deals: List[Deal] = []
        self._ticket = itertools.count(1000001)
        self._lock = threading.RLock()
        self.state_saver = state_saver
        # fault injection hooks (tests / chaos)
        self.fail_after_send: bool = False        # simulate process death right after the broker filled
        self.next_retcode: Optional[int] = None   # force a retcode on next order_send
        self.partial_fill_fraction: Optional[float] = None
        self.reject_sl_on_open: bool = False      # broker accepts the deal but drops the SL
        self.ambiguous_after_fill: bool = False   # broker fills but the response is lost (timeout)

    # ------------------------------------------------------------- persistence
    def export_state(self) -> dict:
        with self._lock:
            return {"balance": self.balance, "next_ticket": next(self._ticket),
                    "positions": [p.__dict__ | {"side": str(p.side)} for p in self._positions.values()],
                    "pending": [o.__dict__ | {"side": str(o.side)} for o in self._pending.values()],
                    "deals": [d.__dict__ | {"side": str(d.side)} for d in self._deals[-500:]]}

    def import_state(self, st: dict) -> None:
        with self._lock:
            self.balance = float(st.get("balance", self.balance))
            self._ticket = itertools.count(int(st.get("next_ticket", 1000001)))
            self._positions = {}
            for p in st.get("positions", []):
                p = dict(p)
                p["side"] = Side(p["side"])
                pos = Position(**p)
                self._positions[pos.ticket] = pos
            self._pending = {}
            for o in st.get("pending", []):
                o = dict(o)
                o["side"] = Side(o["side"])
                order = PendingOrder(**o)
                self._pending[order.ticket] = order
            self._deals = []
            for d in st.get("deals", []):
                d = dict(d)
                d["side"] = Side(d["side"])
                self._deals.append(Deal(**d))

    def _save(self) -> None:
        if self.state_saver:
            try:
                self.state_saver(self.export_state())
            except Exception:  # never let persistence break simulated execution
                pass

    # ------------------------------------------------------------- delegation (data)
    def connect(self) -> None:
        self.data.connect()

    def shutdown(self) -> None:
        self.data.shutdown()

    def is_connected(self) -> bool:
        return self.data.is_connected()

    def terminal_info(self) -> dict:
        return self.data.terminal_info()

    def find_symbols(self, pattern: str) -> List[str]:
        return self.data.find_symbols(pattern)

    def select_symbol(self, symbol: str) -> bool:
        return self.data.select_symbol(symbol)

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        return self.data.symbol_spec(symbol)

    def tick(self, symbol: str) -> Optional[Tick]:
        return self.data.tick(symbol)

    def rates(self, symbol: str, timeframe: str, count: int) -> List[Bar]:
        return self.data.rates(symbol, timeframe, count)

    def rates_range(self, symbol, timeframe, start, end) -> List[Bar]:
        return self.data.rates_range(symbol, timeframe, start, end)

    def ticks_range(self, symbol, start, end):
        return self.data.ticks_range(symbol, start, end)

    def server_time(self) -> float:
        return self.data.server_time()

    def calc_margin(self, symbol, side, volume, price):
        return self.data.calc_margin(symbol, side, volume, price)

    def calc_profit(self, symbol, side, volume, price_open, price_close):
        return self.data.calc_profit(symbol, side, volume, price_open, price_close)

    # ------------------------------------------------------------- account
    def _floating(self) -> float:
        total = 0.0
        for p in self._positions.values():
            t = self.data.tick(p.symbol)
            if t is None:
                continue
            px = t.bid if p.side is Side.BUY else t.ask
            pr = self.data.calc_profit(p.symbol, p.side, p.volume, p.price_open, px)
            total += pr or 0.0
            p.price_current, p.profit = px, pr or 0.0
        return total

    def _used_margin(self) -> float:
        m = 0.0
        for p in self._positions.values():
            m += self.data.calc_margin(p.symbol, p.side, p.volume, p.price_open) or 0.0
        return m

    def account_info(self) -> AccountInfo:
        with self._lock:
            real = None
            try:
                real = self.data.account_info()
            except Exception:
                pass
            fl = self._floating()
            eq = self.balance + fl
            mg = self._used_margin()
            return AccountInfo(login=real.login if real else 0, server=(real.server if real else "paper"),
                               company=(real.company if real else "paper"), currency=self.currency,
                               leverage=real.leverage if real else self.leverage,
                               trade_mode=self.account_mode if self.account_mode != "PAPER" else "PAPER",
                               balance=self.balance, equity=eq, margin=mg, margin_free=eq - mg,
                               margin_level=(eq / mg * 100.0) if mg > 0 else 0.0, profit=fl)

    # ------------------------------------------------------------- state queries
    def positions(self, symbol: Optional[str] = None) -> Optional[List[Position]]:
        with self._lock:
            self._floating()
            return [Position(**p.__dict__) for p in self._positions.values() if symbol in (None, p.symbol)]

    def history_deals(self, start: datetime, end: datetime) -> Optional[List[Deal]]:
        s, e = start.timestamp(), end.timestamp()
        with self._lock:
            return [d for d in self._deals if s - 86400 <= d.time <= e + 86400]

    def deals_for_position(self, position_id: int) -> Optional[List[Deal]]:
        with self._lock:
            return [d for d in self._deals if d.position_id == position_id]

    # ------------------------------------------------------------- pending (resting) orders
    def pending_orders(self, symbol: Optional[str] = None) -> Optional[List[PendingOrder]]:
        with self._lock:
            return [PendingOrder(**o.__dict__) for o in self._pending.values() if symbol in (None, o.symbol)]

    def _validate_pending(self, req: PendingOrderRequest, spec: SymbolSpec, tick: Tick) -> Optional[OrderResult]:
        if req.volume < spec.volume_min - 1e-12 or req.volume > spec.volume_max + 1e-12:
            return OrderResult(False, 10014, "INVALID_VOLUME")
        steps = req.volume / spec.volume_step
        if abs(steps - round(steps)) > 1e-6:
            return OrderResult(False, 10014, "INVALID_VOLUME")
        min_d = spec.stops_level * spec.point
        buy = req.side is Side.BUY
        if buy and req.price < tick.ask + min_d:
            return OrderResult(False, 10015, "INVALID_PRICE")
        if not buy and req.price > tick.bid - min_d:
            return OrderResult(False, 10015, "INVALID_PRICE")
        if req.sl and ((buy and req.sl >= req.price - min_d) or (not buy and req.sl <= req.price + min_d)):
            return OrderResult(False, 10016, "INVALID_STOPS")
        if req.tp and ((buy and req.tp <= req.price + min_d) or (not buy and req.tp >= req.price - min_d)):
            return OrderResult(False, 10016, "INVALID_STOPS")
        margin = self.data.calc_margin(req.symbol, req.side, req.volume, req.price) or 0.0
        if margin > self.account_info().margin_free:
            return OrderResult(False, 10019, "NO_MONEY")
        return None

    def pending_check(self, req: PendingOrderRequest) -> OrderResult:
        tick = self.data.tick(req.symbol)
        if tick is None:
            return OrderResult(False, None, "NO_TICK")
        err = self._validate_pending(req, self.data.symbol_spec(req.symbol), tick)
        return err or OrderResult(True, 0, "OK")

    def place_pending(self, req: PendingOrderRequest) -> OrderResult:
        with self._lock:
            if self.next_retcode is not None:
                code, self.next_retcode = self.next_retcode, None
                from app.broker.base import classify_result
                c = classify_result(code)
                if not c["ok"]:
                    return OrderResult(False, code, c["text"], ambiguous=c["ambiguous"])
            tick = self.data.tick(req.symbol)
            if tick is None:
                return OrderResult(False, None, "NO_TICK", ambiguous=False)
            spec = self.data.symbol_spec(req.symbol)
            err = self._validate_pending(req, spec, tick)
            if err:
                return err
            order = PendingOrder(ticket=next(self._ticket), symbol=req.symbol, side=req.side, volume=req.volume,
                                 price=spec.round_price(req.price), sl=req.sl or 0.0, tp=req.tp or 0.0,
                                 magic=req.magic, comment=req.comment[:31],
                                 time_setup=int(self.data.server_time() or 0), expiration=int(req.expiration or 0))
            self._pending[order.ticket] = order
            self._save()
            return OrderResult(True, 10009, "DONE", order=order.ticket, volume=order.volume, price=order.price)

    def cancel_pending(self, ticket: int, symbol: str, magic: int) -> OrderResult:
        with self._lock:
            if self._pending.pop(int(ticket), None) is None:
                return OrderResult(False, 10035, "INVALID_ORDER", already_closed=True)
            self._save()
            return OrderResult(True, 10009, "DONE", order=int(ticket))

    def _process_pending(self, tick: Tick, symbol: str) -> None:
        """Expire and trigger resting stop orders like a trade server (fills at the market price that triggered)."""
        now = int(self.data.server_time() or 0)
        spec = self.data.symbol_spec(symbol)
        slip = self.slippage_points * spec.point
        for o in list(self._pending.values()):
            if o.symbol != symbol:
                continue
            if o.expiration and now and now >= o.expiration:
                self._pending.pop(o.ticket, None)
                continue
            if o.side is Side.BUY and tick.ask >= o.price:
                price = spec.round_price(max(o.price, tick.ask) + slip)
            elif o.side is Side.SELL and tick.bid <= o.price:
                price = spec.round_price(min(o.price, tick.bid) - slip)
            else:
                continue
            self._pending.pop(o.ticket, None)
            pos = Position(ticket=next(self._ticket), symbol=o.symbol, side=o.side, volume=o.volume,
                           price_open=price, sl=o.sl, tp=o.tp, price_current=price, profit=0.0, swap=0.0,
                           magic=o.magic, comment=o.comment, time=now)
            pos.identifier = pos.ticket
            self._positions[pos.ticket] = pos
            self._new_deal(pos, o.side, "IN", o.volume, price, 0.0, 3, o.comment, o.ticket)

    # ------------------------------------------------------------- execution
    def _validate(self, req: OrderRequest, spec: SymbolSpec, tick: Tick) -> Optional[OrderResult]:
        if req.volume < spec.volume_min - 1e-12 or req.volume > spec.volume_max + 1e-12:
            return OrderResult(False, 10014, "INVALID_VOLUME")
        steps = req.volume / spec.volume_step
        if abs(steps - round(steps)) > 1e-6:
            return OrderResult(False, 10014, "INVALID_VOLUME")
        px = tick.ask if req.side is Side.BUY else tick.bid
        min_d = spec.stops_level * spec.point
        if req.sl:
            if (req.side is Side.BUY and req.sl >= px - min_d) or (req.side is Side.SELL and req.sl <= px + min_d):
                return OrderResult(False, 10016, "INVALID_STOPS")
        if req.tp:
            if (req.side is Side.BUY and req.tp <= px + min_d) or (req.side is Side.SELL and req.tp >= px - min_d):
                return OrderResult(False, 10016, "INVALID_STOPS")
        margin = self.data.calc_margin(req.symbol, req.side, req.volume, px) or 0.0
        acct = self.account_info()
        if margin > acct.margin_free:
            return OrderResult(False, 10019, "NO_MONEY")
        return None

    def order_check(self, req: OrderRequest) -> OrderResult:
        if req.action != "DEAL":
            return OrderResult(True, 0, "OK")
        tick = self.data.tick(req.symbol)
        if tick is None:
            return OrderResult(False, None, "NO_TICK")
        spec = self.data.symbol_spec(req.symbol)
        err = self._validate(req, spec, tick)
        return err or OrderResult(True, 0, "OK")

    def _new_deal(self, pos: Position, side: Side, entry: str, volume: float, price: float, profit: float,
                  reason: int, comment: str, order: int) -> Deal:
        d = Deal(ticket=next(self._ticket), order=order, position_id=pos.ticket, symbol=pos.symbol, side=side,
                 entry=entry, volume=volume, price=price, profit=profit, commission=0.0, swap=0.0, fee=0.0,
                 magic=pos.magic, comment=comment, time=int(self.data.server_time() or 0), reason=reason)
        self._deals.append(d)
        return d

    def order_send(self, req: OrderRequest) -> OrderResult:
        if req.action == "SLTP":
            return self.modify_sltp(req.symbol, req.position, req.sl, req.tp)
        with self._lock:
            if self.next_retcode is not None:
                code, self.next_retcode = self.next_retcode, None
                from app.broker.base import classify_result
                c = classify_result(code)
                if not c["ok"]:
                    return OrderResult(False, code, c["text"], ambiguous=c["ambiguous"])
            tick = self.data.tick(req.symbol)
            if tick is None:
                return OrderResult(False, None, "NO_TICK", ambiguous=False)
            spec = self.data.symbol_spec(req.symbol)
            err = self._validate(req, spec, tick)
            if err:
                return err
            slip = self.slippage_points * spec.point
            price = (tick.ask + slip) if req.side is Side.BUY else (tick.bid - slip)
            price = spec.round_price(price)
            vol = req.volume
            code = 10009
            if self.partial_fill_fraction is not None:
                vol = spec.floor_volume(req.volume * self.partial_fill_fraction)
                vol = max(vol, spec.volume_min)
                self.partial_fill_fraction = None
                code = 10010
            order_id = next(self._ticket)
            pos = Position(ticket=next(self._ticket), symbol=req.symbol, side=req.side, volume=vol,
                           price_open=price, sl=0.0 if self.reject_sl_on_open else (req.sl or 0.0),
                           tp=req.tp or 0.0, price_current=price, profit=0.0, swap=0.0, magic=req.magic,
                           comment=req.comment[:31], time=int(self.data.server_time() or 0))
            pos.identifier = pos.ticket
            self._positions[pos.ticket] = pos
            deal = self._new_deal(pos, req.side, "IN", vol, price, 0.0, 3, req.comment[:31], order_id)
            self._save()
            if self.fail_after_send:
                self.fail_after_send = False
                raise SystemExit("CHAOS: process killed after broker accepted the order")
            if self.ambiguous_after_fill:
                self.ambiguous_after_fill = False
                return OrderResult(False, 10012, "TIMEOUT", ambiguous=True)   # filled, but we never heard back
            return OrderResult(True, code, "DONE" if code == 10009 else "DONE_PARTIAL", order=order_id,
                               deal=deal.ticket, volume=vol, price=price)

    def modify_sltp(self, symbol: str, ticket: int, sl: float, tp: float) -> OrderResult:
        with self._lock:
            p = self._positions.get(ticket)
            if p is None:
                return OrderResult(False, 10036, "POSITION_CLOSED", already_closed=True)
            tick = self.data.tick(symbol)
            spec = self.data.symbol_spec(symbol)
            if tick is not None:
                min_d = spec.stops_level * spec.point
                px = tick.bid if p.side is Side.BUY else tick.ask
                if sl and ((p.side is Side.BUY and sl >= px - min_d) or (p.side is Side.SELL and sl <= px + min_d)):
                    return OrderResult(False, 10016, "INVALID_STOPS")
            p.sl, p.tp = float(sl or 0.0), float(tp or 0.0)
            self._save()
            return OrderResult(True, 10009, "DONE")

    def _close(self, p: Position, volume: float, price: float, reason: int, comment: str) -> Deal:
        volume = min(volume, p.volume)
        profit = self.data.calc_profit(p.symbol, p.side, volume, p.price_open, price) or 0.0
        self.balance += profit
        deal = self._new_deal(p, p.side.opposite(), "OUT", volume, price, profit, reason, comment, next(self._ticket))
        p.volume = round(p.volume - volume, 8)
        if p.volume <= 1e-9:
            self._positions.pop(p.ticket, None)
        return deal

    def close_position(self, position: Position, volume: float, deviation: int, magic: int, comment: str) -> OrderResult:
        with self._lock:
            p = self._positions.get(position.ticket)
            if p is None:
                return OrderResult(False, 10036, "POSITION_CLOSED", already_closed=True)
            tick = self.data.tick(p.symbol)
            if tick is None:
                return OrderResult(False, None, "NO_TICK")
            spec = self.data.symbol_spec(p.symbol)
            slip = self.slippage_points * spec.point
            price = (tick.bid - slip) if p.side is Side.BUY else (tick.ask + slip)
            deal = self._close(p, volume, spec.round_price(price), 3, comment)
            self._save()
            return OrderResult(True, 10009, "DONE", deal=deal.ticket, volume=deal.volume, price=deal.price)

    def on_tick(self, tick: Tick, symbol: str) -> List[Deal]:
        """Enforce broker-side SL/TP like a real trade server."""
        closed = []
        with self._lock:
            if self._pending:
                self._process_pending(tick, symbol)
                self._save()
            for p in list(self._positions.values()):
                if p.symbol != symbol:
                    continue
                # triggered stops/targets execute at the market price that triggered them
                # (gaps therefore fill worse than the stop - as on a real server)
                if p.side is Side.BUY:
                    if p.sl and tick.bid <= p.sl:
                        closed.append(self._close(p, p.volume, tick.bid, 4, "[sl]"))
                    elif p.tp and tick.bid >= p.tp:
                        closed.append(self._close(p, p.volume, tick.bid, 5, "[tp]"))
                else:
                    if p.sl and tick.ask >= p.sl:
                        closed.append(self._close(p, p.volume, tick.ask, 4, "[sl]"))
                    elif p.tp and tick.ask <= p.tp:
                        closed.append(self._close(p, p.volume, tick.ask, 5, "[tp]"))
            if closed:
                self._save()
        return closed

    # test helper: a user closing a position by hand in MT5
    def manual_close(self, ticket: int) -> None:
        with self._lock:
            p = self._positions[ticket]
            t = self.data.tick(p.symbol)
            self._close(p, p.volume, t.bid if p.side is Side.BUY else t.ask, 0, "manual")
            self._save()

    def inject_position(self, pos: Position) -> None:
        with self._lock:
            self._positions[pos.ticket] = pos
