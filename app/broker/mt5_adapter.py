"""Native MetaTrader 5 adapter (Windows only - requires the MetaTrader5 package
and a running, logged-in MT5 terminal with Algo Trading enabled).

The MetaTrader5 library is NOT thread safe: only the trading-core thread may
call this adapter.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import List, Optional

from app.broker.base import BrokerAdapter, BrokerError, classify_result
from app.core.types import (AccountInfo, Bar, Deal, OrderRequest, OrderResult, PendingOrder, PendingOrderRequest,
                            Position, Side, SymbolSpec, Tick)

log = logging.getLogger(__name__)

try:  # pragma: no cover - only importable on Windows
    import MetaTrader5 as mt5  # type: ignore
except Exception:  # pragma: no cover
    mt5 = None

TF = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15", "M30": "TIMEFRAME_M30",
      "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4", "D1": "TIMEFRAME_D1"}
EXEMODE = {0: "REQUEST", 1: "INSTANT", 2: "MARKET", 3: "EXCHANGE"}
TRADEMODE = {0: "DISABLED", 1: "LONGONLY", 2: "SHORTONLY", 3: "CLOSEONLY", 4: "FULL"}
ACC_MODE = {0: "DEMO", 1: "CONTEST", 2: "REAL"}
DEAL_ENTRY = {0: "IN", 1: "OUT", 2: "INOUT", 3: "OUT_BY"}


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class MT5Adapter(BrokerAdapter):  # pragma: no cover - exercised on the user's Windows PC
    name = "mt5"
    supports_pending = True

    def __init__(self, login: Optional[int], password: Optional[str], server: Optional[str],
                 terminal_path: Optional[str] = None, timeout_ms: int = 60000):
        if mt5 is None:
            raise BrokerError("MetaTrader5 package not available. Install it on Windows: pip install MetaTrader5")
        self.login, self.password, self.server = login, password, server
        self.terminal_path = terminal_path
        self.timeout_ms = timeout_ms
        self._connected = False
        self._last_server_time = 0.0
        self._last_server_time_wall = 0.0

    # ------------------------------------------------------------- connection
    def connect(self) -> None:
        kwargs = {"timeout": self.timeout_ms}
        if self.terminal_path:
            kwargs["path"] = self.terminal_path
        if self.login:
            kwargs.update(login=int(self.login), password=self.password or "", server=self.server or "")
        if not mt5.initialize(**kwargs):
            code, msg = mt5.last_error()
            raise BrokerError(f"MT5 initialize failed: {code} {msg}")
        if self.login:
            ai = mt5.account_info()
            if ai is None or int(ai.login) != int(self.login):
                if not mt5.login(int(self.login), password=self.password or "", server=self.server or "",
                                 timeout=self.timeout_ms):
                    code, msg = mt5.last_error()
                    raise BrokerError(f"MT5 login failed: {code} {msg}")
        self._connected = True
        log.info("MT5 connected: %s", mt5.terminal_info())

    def shutdown(self) -> None:
        try:
            mt5.shutdown()
        finally:
            self._connected = False

    def is_connected(self) -> bool:
        ti = mt5.terminal_info()
        ok = bool(ti and ti.connected)
        self._connected = ok
        return ok

    def terminal_info(self) -> dict:
        ti = mt5.terminal_info()
        if ti is None:
            return {}
        d = ti._asdict()
        return {"connected": d.get("connected"), "trade_allowed": d.get("trade_allowed"),
                "tradeapi_disabled": d.get("tradeapi_disabled"), "ping_ms": (d.get("ping_last") or 0) / 1000.0,
                "build": d.get("build"), "company": d.get("company"), "path": d.get("path")}

    # ------------------------------------------------------------- account / symbols
    def account_info(self) -> AccountInfo:
        a = mt5.account_info()
        if a is None:
            raise BrokerError(f"account_info failed: {mt5.last_error()}")
        d = a._asdict()
        return AccountInfo(login=d["login"], server=d["server"], company=d["company"], currency=d["currency"],
                           leverage=d["leverage"], trade_mode=ACC_MODE.get(d["trade_mode"], str(d["trade_mode"])),
                           balance=d["balance"], equity=d["equity"], margin=d["margin"],
                           margin_free=d["margin_free"], margin_level=d["margin_level"] or 0.0,
                           profit=d["profit"], trade_allowed=bool(d["trade_allowed"]),
                           trade_expert=bool(d["trade_expert"]), margin_so_call=d["margin_so_call"],
                           margin_so_so=d["margin_so_so"], hedging=d.get("margin_mode") == 2, raw=d)

    def find_symbols(self, pattern: str) -> List[str]:
        syms = mt5.symbols_get(group=pattern)
        return [s.name for s in syms] if syms else []

    def select_symbol(self, symbol: str) -> bool:
        return bool(mt5.symbol_select(symbol, True))

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        s = mt5.symbol_info(symbol)
        if s is None:
            raise BrokerError(f"symbol_info({symbol}) failed: {mt5.last_error()}")
        d = s._asdict()
        return SymbolSpec(name=d["name"], digits=d["digits"], point=d["point"], tick_size=d["trade_tick_size"],
                          tick_value=d["trade_tick_value"], tick_value_loss=d.get("trade_tick_value_loss", 0.0),
                          tick_value_profit=d.get("trade_tick_value_profit", 0.0),
                          contract_size=d["trade_contract_size"], volume_min=d["volume_min"],
                          volume_max=d["volume_max"], volume_step=d["volume_step"],
                          stops_level=d["trade_stops_level"], freeze_level=d["trade_freeze_level"],
                          filling_mode_flags=d["filling_mode"], execution_mode=EXEMODE.get(d["trade_exemode"], "?"),
                          trade_mode=TRADEMODE.get(d["trade_mode"], "?"), swap_long=d["swap_long"],
                          swap_short=d["swap_short"], swap_mode=d["swap_mode"],
                          currency_base=d["currency_base"], currency_profit=d["currency_profit"],
                          currency_margin=d["currency_margin"], description=d.get("description", ""),
                          raw={k: (v if isinstance(v, (int, float, str, bool)) else str(v)) for k, v in d.items()})

    # ------------------------------------------------------------- market data
    def tick(self, symbol: str) -> Optional[Tick]:
        t = mt5.symbol_info_tick(symbol)
        if t is None:
            return None
        if t.time > self._last_server_time:
            self._last_server_time = float(t.time)
            self._last_server_time_wall = time.time()
        return Tick(time_msc=int(t.time_msc), bid=float(t.bid), ask=float(t.ask), last=float(t.last),
                    volume=float(t.volume))

    @staticmethod
    def _bars(arr) -> List[Bar]:
        if arr is None:
            return []
        return [Bar(int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                    int(r["tick_volume"]), int(r["spread"]), int(r["real_volume"])) for r in arr]

    def rates(self, symbol: str, timeframe: str, count: int) -> List[Bar]:
        arr = mt5.copy_rates_from_pos(symbol, getattr(mt5, TF[timeframe]), 0, int(count))
        if arr is None:
            log.warning("copy_rates_from_pos %s %s failed: %s", symbol, timeframe, mt5.last_error())
        return self._bars(arr)

    def rates_range(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> List[Bar]:
        arr = mt5.copy_rates_range(symbol, getattr(mt5, TF[timeframe]), _utc(start), _utc(end))
        return self._bars(arr)

    def ticks_range(self, symbol: str, start: datetime, end: datetime) -> List[Tick]:
        arr = mt5.copy_ticks_range(symbol, _utc(start), _utc(end), mt5.COPY_TICKS_INFO)
        if arr is None:
            return []
        return [Tick(int(r["time_msc"]), float(r["bid"]), float(r["ask"])) for r in arr]

    def ticks_array(self, symbol: str, start: datetime, end: datetime):
        """Raw tick arrays (time_msc, bid, ask) for research - no per-tick Python objects, so millions are cheap."""
        arr = mt5.copy_ticks_range(symbol, _utc(start), _utc(end), mt5.COPY_TICKS_INFO)
        if arr is None or len(arr) == 0:
            return None
        return arr["time_msc"], arr["bid"], arr["ask"]

    def server_time(self) -> float:
        if self._last_server_time == 0:
            return 0.0
        # extrapolate between ticks using the wall clock delta (never decides bar closes alone)
        return self._last_server_time + max(0.0, time.time() - self._last_server_time_wall)

    # ------------------------------------------------------------- calculations
    def calc_margin(self, symbol: str, side: Side, volume: float, price: float) -> Optional[float]:
        t = mt5.ORDER_TYPE_BUY if side is Side.BUY else mt5.ORDER_TYPE_SELL
        return mt5.order_calc_margin(t, symbol, float(volume), float(price))

    def calc_profit(self, symbol: str, side: Side, volume: float, price_open: float, price_close: float) -> Optional[float]:
        t = mt5.ORDER_TYPE_BUY if side is Side.BUY else mt5.ORDER_TYPE_SELL
        return mt5.order_calc_profit(t, symbol, float(volume), float(price_open), float(price_close))

    # ------------------------------------------------------------- state
    @staticmethod
    def _pos(p) -> Position:
        return Position(ticket=int(p.ticket), symbol=p.symbol, side=Side.BUY if p.type == 0 else Side.SELL,
                        volume=float(p.volume), price_open=float(p.price_open), sl=float(p.sl), tp=float(p.tp),
                        price_current=float(p.price_current), profit=float(p.profit), swap=float(p.swap),
                        magic=int(p.magic), comment=p.comment or "", time=int(p.time), identifier=int(p.identifier))

    def positions(self, symbol: Optional[str] = None) -> Optional[List[Position]]:
        res = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if res is None:
            code, _ = mt5.last_error()
            if code == 1:      # RES_S_OK with no positions on some builds
                return []
            log.warning("positions_get failed: %s", mt5.last_error())
            return None
        return [self._pos(p) for p in res]

    @staticmethod
    def _deal(d) -> Deal:
        return Deal(ticket=int(d.ticket), order=int(d.order), position_id=int(d.position_id), symbol=d.symbol,
                    side=Side.BUY if d.type == 0 else Side.SELL, entry=DEAL_ENTRY.get(d.entry, str(d.entry)),
                    volume=float(d.volume), price=float(d.price), profit=float(d.profit),
                    commission=float(d.commission), swap=float(d.swap), fee=float(getattr(d, "fee", 0.0)),
                    magic=int(d.magic), comment=d.comment or "", time=int(d.time), reason=int(d.reason))

    def history_deals(self, start: datetime, end: datetime) -> Optional[List[Deal]]:
        res = mt5.history_deals_get(_utc(start), _utc(end))
        if res is None:
            code, _ = mt5.last_error()
            return [] if code == 1 else None
        return [self._deal(d) for d in res if d.type in (0, 1)]

    def deals_for_position(self, position_id: int) -> Optional[List[Deal]]:
        res = mt5.history_deals_get(position=int(position_id))
        if res is None:
            code, _ = mt5.last_error()
            return [] if code == 1 else None
        return [self._deal(d) for d in res if d.type in (0, 1)]

    # ------------------------------------------------------------- trading
    def _filling(self, symbol: str, preferred: str = "") -> int:
        if preferred == "FOK":
            return mt5.ORDER_FILLING_FOK
        if preferred == "IOC":
            return mt5.ORDER_FILLING_IOC
        info = mt5.symbol_info(symbol)
        flags = info.filling_mode if info else 0
        if flags & 1:
            return mt5.ORDER_FILLING_FOK
        if flags & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def _request(self, req: OrderRequest) -> dict:
        if req.action in ("DEAL", "CLOSE"):
            r = {"action": mt5.TRADE_ACTION_DEAL, "symbol": req.symbol, "volume": float(req.volume),
                 "type": mt5.ORDER_TYPE_BUY if req.side is Side.BUY else mt5.ORDER_TYPE_SELL,
                 "price": float(req.price), "deviation": int(req.deviation), "magic": int(req.magic),
                 "comment": req.comment[:31], "type_time": mt5.ORDER_TIME_GTC,
                 "type_filling": self._filling(req.symbol, req.filling)}
            if req.action == "DEAL":
                if req.sl:
                    r["sl"] = float(req.sl)
                if req.tp:
                    r["tp"] = float(req.tp)
            if req.position:
                r["position"] = int(req.position)
            return r
        if req.action == "SLTP":
            return {"action": mt5.TRADE_ACTION_SLTP, "symbol": req.symbol, "position": int(req.position),
                    "sl": float(req.sl or 0.0), "tp": float(req.tp or 0.0), "magic": int(req.magic)}
        raise BrokerError(f"Unsupported action {req.action}")

    def _result(self, res, t0: float) -> OrderResult:
        lat = (time.perf_counter() - t0) * 1000.0
        if res is None:
            code, msg = mt5.last_error()
            return OrderResult(ok=False, retcode=None, retcode_text="NO_RESPONSE", comment=f"{code} {msg}",
                               ambiguous=True, latency_ms=lat)
        c = classify_result(int(res.retcode))
        d = res._asdict()
        d.pop("request", None)
        return OrderResult(ok=c["ok"], retcode=int(res.retcode), retcode_text=c["text"], comment=res.comment or "",
                           order=int(res.order), deal=int(res.deal), volume=float(res.volume),
                           price=float(res.price), ambiguous=c["ambiguous"], already_closed=c["already_closed"],
                           latency_ms=lat, raw={k: v for k, v in d.items() if isinstance(v, (int, float, str))})

    def order_check(self, req: OrderRequest) -> OrderResult:
        t0 = time.perf_counter()
        res = mt5.order_check(self._request(req))
        lat = (time.perf_counter() - t0) * 1000.0
        if res is None:
            return OrderResult(ok=False, retcode=None, retcode_text="NO_RESPONSE", comment=str(mt5.last_error()),
                               latency_ms=lat)
        ok = int(res.retcode) == 0
        return OrderResult(ok=ok, retcode=int(res.retcode), retcode_text="OK" if ok else
                           classify_result(int(res.retcode))["text"], comment=res.comment or "", latency_ms=lat,
                           raw={"margin": res.margin, "margin_free": res.margin_free, "margin_level": res.margin_level})

    def order_send(self, req: OrderRequest) -> OrderResult:
        t0 = time.perf_counter()
        res = mt5.order_send(self._request(req))
        return self._result(res, t0)

    def modify_sltp(self, symbol: str, ticket: int, sl: float, tp: float) -> OrderResult:
        t0 = time.perf_counter()
        res = mt5.order_send(self._request(OrderRequest("SLTP", symbol, position=ticket, sl=sl, tp=tp)))
        r = self._result(res, t0)
        if not r.ok and r.retcode == 10013:
            # INVALID on a ticket that no longer exists -> treat as already closed, reconcile
            if not mt5.positions_get(ticket=int(ticket)):
                r.already_closed = True
        return r

    def close_position(self, position: Position, volume: float, deviation: int, magic: int, comment: str) -> OrderResult:
        t = mt5.symbol_info_tick(position.symbol)
        if t is None:
            return OrderResult(ok=False, retcode=None, retcode_text="NO_TICK", ambiguous=False)
        side = position.side.opposite()
        price = t.bid if position.side is Side.BUY else t.ask
        req = OrderRequest("CLOSE", position.symbol, side=side, volume=volume, price=price, deviation=deviation,
                           magic=magic, comment=comment, position=position.ticket)
        t0 = time.perf_counter()
        res = mt5.order_send(self._request(req))
        r = self._result(res, t0)
        if not r.ok and not r.ambiguous and not mt5.positions_get(ticket=int(position.ticket)):
            r.already_closed = True
        return r

    # ------------------------------------------------------------- pending (resting) orders
    BUY_ORDER_TYPES = {2, 4, 6}      # BUY_LIMIT, BUY_STOP, BUY_STOP_LIMIT
    SELL_ORDER_TYPES = {3, 5, 7}     # SELL_LIMIT, SELL_STOP, SELL_STOP_LIMIT

    @classmethod
    def _pending(cls, o) -> PendingOrder:
        return PendingOrder(ticket=int(o.ticket), symbol=o.symbol,
                            side=Side.BUY if int(o.type) in cls.BUY_ORDER_TYPES else Side.SELL,
                            volume=float(o.volume_current), price=float(o.price_open), sl=float(o.sl), tp=float(o.tp),
                            magic=int(o.magic), comment=o.comment or "", time_setup=int(o.time_setup),
                            expiration=int(getattr(o, "time_expiration", 0) or 0))

    def pending_orders(self, symbol: Optional[str] = None) -> Optional[List[PendingOrder]]:
        res = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        if res is None:
            code, _ = mt5.last_error()
            if code == 1:
                return []
            log.warning("orders_get failed: %s", mt5.last_error())
            return None
        return [self._pending(o) for o in res]

    def _pending_request(self, req: PendingOrderRequest, filling: int, gtc: bool) -> dict:
        r = {"action": mt5.TRADE_ACTION_PENDING, "symbol": req.symbol, "volume": float(req.volume),
             "type": mt5.ORDER_TYPE_BUY_STOP if req.side is Side.BUY else mt5.ORDER_TYPE_SELL_STOP,
             "price": float(req.price), "deviation": int(req.deviation), "magic": int(req.magic),
             "comment": req.comment[:31], "type_filling": filling}
        if req.sl:
            r["sl"] = float(req.sl)
        if req.tp:
            r["tp"] = float(req.tp)
        if req.expiration and not gtc:
            r["type_time"] = mt5.ORDER_TIME_SPECIFIED
            r["expiration"] = int(req.expiration)      # broker-epoch seconds (the same clock as symbol_info.time)
        else:
            r["type_time"] = mt5.ORDER_TIME_GTC
        return r

    def _pending_fillings(self) -> list:
        # Stop orders are normally accepted with RETURN; brokers differ, so INVALID_FILL falls through the list.
        return [mt5.ORDER_FILLING_RETURN, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK]

    def pending_check(self, req: PendingOrderRequest) -> OrderResult:
        t0 = time.perf_counter()
        last = None
        for gtc in ((False, True) if req.expiration else (True,)):
            for filling in self._pending_fillings():
                res = mt5.order_check(self._pending_request(req, filling, gtc))
                lat = (time.perf_counter() - t0) * 1000.0
                if res is None:
                    last = OrderResult(ok=False, retcode=None, retcode_text="NO_RESPONSE",
                                       comment=str(mt5.last_error()), latency_ms=lat)
                    continue
                code = int(res.retcode)
                if code == 0:
                    return OrderResult(ok=True, retcode=0, retcode_text="OK", comment=res.comment or "",
                                       latency_ms=lat, raw={"margin": res.margin, "margin_free": res.margin_free,
                                                            "gtc": gtc, "filling": int(filling)})
                last = OrderResult(ok=False, retcode=code, retcode_text=classify_result(code)["text"],
                                   comment=res.comment or "", latency_ms=lat)
                if code not in (10030, 10022):      # only INVALID_FILL / INVALID_EXPIRATION are worth another try
                    return last
                if code == 10022:
                    break
        return last or OrderResult(ok=False, retcode=None, retcode_text="NO_RESPONSE")

    def place_pending(self, req: PendingOrderRequest) -> OrderResult:
        chk = self.pending_check(req)
        if not chk.ok:
            return chk
        t0 = time.perf_counter()
        raw = chk.raw or {}
        res = mt5.order_send(self._pending_request(req, raw.get("filling", mt5.ORDER_FILLING_RETURN),
                                                   bool(raw.get("gtc", not req.expiration))))
        return self._result(res, t0)

    def cancel_pending(self, ticket: int, symbol: str, magic: int) -> OrderResult:
        t0 = time.perf_counter()
        res = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": int(ticket)})
        r = self._result(res, t0)
        if not r.ok and not r.ambiguous and not mt5.orders_get(ticket=int(ticket)):
            r.already_closed = True        # the order is gone (triggered, expired or removed by hand)
        return r

    # ------------------------------------------------------------- depth of market (most CFD servers have none)
    _book_subscribed: set = set()

    def market_book(self, symbol: str):
        try:
            if symbol not in self._book_subscribed:
                if not mt5.market_book_add(symbol):
                    return None
                self._book_subscribed.add(symbol)
            items = mt5.market_book_get(symbol)
            if not items:
                return None
            out = []
            for it in items:                       # BOOK_TYPE_SELL=1, BUY=2, SELL_MARKET=3, BUY_MARKET=4
                t = int(it.type)
                out.append((float(it.price), float(it.volume), "ASK" if t in (1, 3) else "BID"))
            return out
        except Exception:
            return None
