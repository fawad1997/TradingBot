"""Broker adapter interface. The trading core only talks to this interface,
so the MT5 library (or aiomql, or a simulator) never becomes business logic."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from app.core.types import (AccountInfo, Bar, Deal, OrderRequest, OrderResult, PendingOrder, PendingOrderRequest,
                            Position, Side, SymbolSpec, Tick)

# MT5 trade server return codes (subset with explicit handling)
RETCODE_TEXT = {
    10004: "REQUOTE", 10006: "REJECT", 10007: "CANCEL", 10008: "PLACED", 10009: "DONE",
    10010: "DONE_PARTIAL", 10011: "ERROR", 10012: "TIMEOUT", 10013: "INVALID", 10014: "INVALID_VOLUME",
    10015: "INVALID_PRICE", 10016: "INVALID_STOPS", 10017: "TRADE_DISABLED", 10018: "MARKET_CLOSED",
    10019: "NO_MONEY", 10020: "PRICE_CHANGED", 10021: "PRICE_OFF", 10022: "INVALID_EXPIRATION",
    10023: "ORDER_CHANGED", 10024: "TOO_MANY_REQUESTS", 10025: "NO_CHANGES", 10026: "SERVER_DISABLES_AT",
    10027: "CLIENT_DISABLES_AT", 10028: "LOCKED", 10029: "FROZEN", 10030: "INVALID_FILL",
    10031: "CONNECTION", 10032: "ONLY_REAL", 10033: "LIMIT_ORDERS", 10034: "LIMIT_VOLUME",
    10035: "INVALID_ORDER", 10036: "POSITION_CLOSED", 10038: "INVALID_CLOSE_VOLUME",
    10039: "CLOSE_ORDER_EXIST", 10040: "LIMIT_POSITIONS", 10041: "REJECT_CANCEL", 10042: "LONG_ONLY",
    10043: "SHORT_ONLY", 10044: "CLOSE_ONLY", 10045: "FIFO_CLOSE",
}
SUCCESS_CODES = {10008, 10009, 10010}
# Outcome unknown: the broker may or may not have executed -> never retry, reconcile.
AMBIGUOUS_CODES = {10011, 10012, 10031}
REQUOTE_CODES = {10004, 10020, 10021}
ALREADY_CLOSED_CODES = {10036}


def classify_result(retcode: Optional[int]) -> dict:
    if retcode is None:
        return {"ok": False, "ambiguous": True, "already_closed": False, "text": "NO_RESPONSE"}
    return {"ok": retcode in SUCCESS_CODES, "ambiguous": retcode in AMBIGUOUS_CODES,
            "already_closed": retcode in ALREADY_CLOSED_CODES,
            "text": RETCODE_TEXT.get(retcode, f"UNKNOWN_{retcode}")}


class BrokerError(Exception):
    pass


class BrokerAdapter(ABC):
    name = "abstract"

    # ---- connection
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def shutdown(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    def terminal_info(self) -> dict:
        return {}

    # ---- account / symbols
    @abstractmethod
    def account_info(self) -> AccountInfo: ...

    @abstractmethod
    def find_symbols(self, pattern: str) -> List[str]: ...

    @abstractmethod
    def select_symbol(self, symbol: str) -> bool: ...

    @abstractmethod
    def symbol_spec(self, symbol: str) -> SymbolSpec: ...

    # ---- market data
    @abstractmethod
    def tick(self, symbol: str) -> Optional[Tick]: ...

    @abstractmethod
    def rates(self, symbol: str, timeframe: str, count: int) -> List[Bar]:
        """Latest `count` bars, oldest->newest, the LAST one is the forming bar."""

    @abstractmethod
    def rates_range(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> List[Bar]: ...

    def ticks_range(self, symbol: str, start: datetime, end: datetime) -> List[Tick]:
        return []

    @abstractmethod
    def server_time(self) -> float:
        """Best estimate of broker server epoch seconds (from the latest tick)."""

    # ---- calculations
    @abstractmethod
    def calc_margin(self, symbol: str, side: Side, volume: float, price: float) -> Optional[float]: ...

    @abstractmethod
    def calc_profit(self, symbol: str, side: Side, volume: float, price_open: float, price_close: float) -> Optional[float]: ...

    # ---- trading state
    @abstractmethod
    def positions(self, symbol: Optional[str] = None) -> Optional[List[Position]]:
        """None means the query FAILED (unknown state) - distinct from an empty list."""

    @abstractmethod
    def history_deals(self, start: datetime, end: datetime) -> Optional[List[Deal]]: ...

    def deals_for_position(self, position_id: int) -> Optional[List[Deal]]:
        return None

    # ---- trading actions
    @abstractmethod
    def order_check(self, req: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def order_send(self, req: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def modify_sltp(self, symbol: str, ticket: int, sl: float, tp: float) -> OrderResult: ...

    @abstractmethod
    def close_position(self, position: Position, volume: float, deviation: int, magic: int, comment: str) -> OrderResult: ...

    def market_book(self, symbol: str):
        """Depth of market as [(price, volume, 'BID'|'ASK')] or None when the broker publishes none."""
        return None

    # ---- pending (resting) orders - optional; only the news straddle uses them
    supports_pending = False

    def pending_orders(self, symbol: Optional[str] = None) -> Optional[List[PendingOrder]]:
        """Resting orders. None means the query FAILED (unknown state) - distinct from an empty list."""
        raise BrokerError(f"{self.name} adapter does not support pending orders")

    def pending_check(self, req: PendingOrderRequest) -> OrderResult:
        raise BrokerError(f"{self.name} adapter does not support pending orders")

    def place_pending(self, req: PendingOrderRequest) -> OrderResult:
        raise BrokerError(f"{self.name} adapter does not support pending orders")

    def cancel_pending(self, ticket: int, symbol: str, magic: int) -> OrderResult:
        raise BrokerError(f"{self.name} adapter does not support pending orders")
