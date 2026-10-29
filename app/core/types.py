"""Core domain types shared by every subsystem.

Time convention
---------------
* Bar and tick times coming from MT5 are "broker epoch seconds" (broker server
  time expressed as if it were UTC). The BrokerClock converts them to real UTC.
* Everything written to the database is real UTC (timestamptz).
"""
from __future__ import annotations

import enum
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


def epoch_to_utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class StrEnum(str, enum.Enum):
    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


class Environment(StrEnum):
    DRY_RUN = "DRY_RUN"   # live MT5 data, simulated (paper) fills, no broker orders
    SHADOW = "SHADOW"     # live MT5 data, signals + counterfactuals only, no fills at all
    DEMO = "DEMO"         # real orders on a DEMO account only
    REAL = "REAL"         # retained for research/test gates; disabled for execution in this release


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class TrendRegime(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    TRANSITION = "TRANSITION"
    UNKNOWN = "UNKNOWN"


class VolRegime(StrEnum):
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    NORMAL = "NORMAL"
    VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"
    VOLATILITY_CONTRACTION = "VOLATILITY_CONTRACTION"
    UNKNOWN = "UNKNOWN"


class Session(StrEnum):
    ASIA = "ASIA"
    LONDON = "LONDON"
    LONDON_NY_OVERLAP = "LONDON_NY_OVERLAP"
    NEW_YORK = "NEW_YORK"
    ROLLOVER = "ROLLOVER"
    WEEKEND_OPEN = "WEEKEND_OPEN"
    WEEKEND = "WEEKEND"     # always-open symbols (e.g. XAUUSD247m) trading through the weekend: own spread bucket
    CLOSED = "CLOSED"


class EventState(StrEnum):
    NORMAL = "NORMAL"
    PRE_EVENT = "PRE_EVENT"
    EVENT = "EVENT"
    POST_EVENT = "POST_EVENT"
    UNKNOWN = "UNKNOWN"


class RiskState(StrEnum):
    NORMAL = "NORMAL"
    REDUCED = "REDUCED"
    RESTRICTED = "RESTRICTED"
    NO_NEW_TRADES = "NO_NEW_TRADES"
    EMERGENCY = "EMERGENCY"


RISK_STATE_ORDER = [RiskState.NORMAL, RiskState.REDUCED, RiskState.RESTRICTED,
                    RiskState.NO_NEW_TRADES, RiskState.EMERGENCY]


def worst_risk_state(*states: RiskState) -> RiskState:
    return max(states, key=RISK_STATE_ORDER.index)


class StrategyStatus(StrEnum):
    """Promotion ladder: RESEARCH_ONLY -> SHADOW -> DEMO -> MICRO_LIVE -> VALIDATED."""
    RESEARCH_ONLY = "RESEARCH_ONLY"
    SHADOW = "SHADOW"
    DEMO = "DEMO"
    MICRO_LIVE = "MICRO_LIVE"
    VALIDATED = "VALIDATED"
    QUARANTINE = "QUARANTINE"
    DISABLED = "DISABLED"


class Eligibility(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    DEGRADED = "DEGRADED"
    RESTRICTED = "RESTRICTED"
    DISABLED = "DISABLED"
    RESEARCH_ONLY = "RESEARCH_ONLY"


class StrategyHealth(StrEnum):
    HEALTHY = "HEALTHY"
    WATCH = "WATCH"
    DEGRADED = "DEGRADED"
    RESTRICTED = "RESTRICTED"
    DISABLED = "DISABLED"


class ComponentStatus(StrEnum):
    HEALTHY = "HEALTHY"
    WATCH = "WATCH"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    MARKET_CLOSED = "MARKET_CLOSED"
    UNKNOWN = "UNKNOWN"


class EvidenceLevel(StrEnum):
    INSUFFICIENT = "INSUFFICIENT"
    PRELIMINARY = "PRELIMINARY"
    PROMISING = "PROMISING"
    ROBUST = "ROBUST"
    VALIDATED = "VALIDATED"
    DEGRADED = "DEGRADED"
    INVALIDATED = "INVALIDATED"


class TradeState(StrEnum):
    SIGNAL_RECEIVED = "SIGNAL_RECEIVED"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"
    ORDER_SENT = "ORDER_SENT"
    OPEN = "OPEN"
    PROTECTION = "PROTECTION"       # stop moved to break-even or better
    PARTIAL = "PARTIAL"             # partial profit taken
    RUNNER = "RUNNER"               # remaining position under trailing management
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    BROKER_CLOSED = "BROKER_CLOSED"  # closed by broker-side SL/TP/stop-out
    USER_MODIFIED = "USER_MODIFIED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    ERROR = "ERROR"


ACTIVE_TRADE_STATES = [TradeState.APPROVED, TradeState.ORDER_SENT, TradeState.OPEN, TradeState.PROTECTION,
                       TradeState.PARTIAL, TradeState.RUNNER, TradeState.USER_MODIFIED,
                       TradeState.RECONCILIATION_REQUIRED]
OPEN_POSITION_STATES = [TradeState.OPEN, TradeState.PROTECTION, TradeState.PARTIAL, TradeState.RUNNER,
                        TradeState.USER_MODIFIED]
TERMINAL_TRADE_STATES = [TradeState.CLOSED, TradeState.REJECTED, TradeState.CANCELLED,
                         TradeState.BROKER_CLOSED, TradeState.ERROR]


class StartupState(StrEnum):
    BOOT = "BOOT"
    CONNECTING = "CONNECTING"
    MT5_CONNECTED = "MT5_CONNECTED"
    ACCOUNT_VALIDATION = "ACCOUNT_VALIDATION"
    SYMBOL_VALIDATION = "SYMBOL_VALIDATION"
    DATA_HYDRATION = "DATA_HYDRATION"
    INDICATOR_INITIALIZATION = "INDICATOR_INITIALIZATION"
    STRUCTURE_INITIALIZATION = "STRUCTURE_INITIALIZATION"
    BROKER_STATE_RECONCILIATION = "BROKER_STATE_RECONCILIATION"
    STRATEGY_VALIDATION = "STRATEGY_VALIDATION"
    RISK_VALIDATION = "RISK_VALIDATION"
    READY = "READY"
    ACTIVE = "ACTIVE"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


STARTUP_SEQUENCE = [StartupState.BOOT, StartupState.CONNECTING, StartupState.MT5_CONNECTED,
                    StartupState.ACCOUNT_VALIDATION, StartupState.SYMBOL_VALIDATION,
                    StartupState.DATA_HYDRATION, StartupState.INDICATOR_INITIALIZATION,
                    StartupState.STRUCTURE_INITIALIZATION, StartupState.BROKER_STATE_RECONCILIATION,
                    StartupState.STRATEGY_VALIDATION, StartupState.RISK_VALIDATION,
                    StartupState.READY, StartupState.ACTIVE]


class Gate(StrEnum):
    STRATEGY_STATUS = "STRATEGY_STATUS"
    REGIME = "REGIME"
    KILL_SWITCH = "KILL_SWITCH"
    HEALTH = "HEALTH"
    SESSION = "SESSION"
    EVENT = "EVENT"
    SPREAD = "SPREAD"
    COST = "COST"
    EDGE = "EDGE"
    RISK = "RISK"
    SIZE = "SIZE"
    MARGIN = "MARGIN"
    PORTFOLIO = "PORTFOLIO"
    STALE = "STALE"
    VISION = "VISION"                       # RETIRED: the Gemini vision gate was removed. Kept so historical
                                            # rejection rows written before the removal still parse.
    EXECUTION = "EXECUTION"
    ENVIRONMENT = "ENVIRONMENT"
    SYMBOL_DISABLED = "SYMBOL_DISABLED"     # per-symbol Start/Stop switch on the dashboard
    RESEARCH = "RESEARCH"                   # research lab found no edge for this strategy on this symbol
    ML_FILTER = "ML_FILTER"                 # learned trade filter scored this setup below its validated cut-off
    SELECTOR = "SELECTOR"                   # strategy selector benched this strategy here (negative after costs)


class RouteDecision(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    WAIT = "WAIT"
    CANCEL = "CANCEL"
    NO_TRADE = "NO_TRADE"


TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}


# --------------------------------------------------------------------------- market data
@dataclass
class Tick:
    time_msc: int          # broker epoch milliseconds
    bid: float
    ask: float
    last: float = 0.0
    volume: float = 0.0

    @property
    def time(self) -> float:
        return self.time_msc / 1000.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class Bar:
    time: int              # broker epoch seconds, bar OPEN time
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0
    spread: int = 0        # MT5 bar spread, in points
    real_volume: int = 0


@dataclass
class SymbolSpec:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    tick_value_loss: float = 0.0
    tick_value_profit: float = 0.0
    stops_level: int = 0           # points
    freeze_level: int = 0          # points
    filling_mode_flags: int = 0    # bitmask: 1 = FOK, 2 = IOC
    execution_mode: str = "MARKET"
    trade_mode: str = "FULL"
    swap_long: float = 0.0
    swap_short: float = 0.0
    swap_mode: int = 0
    currency_base: str = ""
    currency_profit: str = "USD"
    currency_margin: str = ""
    description: str = ""
    raw: dict = field(default_factory=dict)

    def round_price(self, price: float) -> float:
        if self.tick_size > 0:
            price = round(price / self.tick_size) * self.tick_size
        return round(price, self.digits)

    def floor_volume(self, volume: float) -> float:
        """Round DOWN to the broker volume step. Never rounds up (no hidden risk increase)."""
        if volume <= 0 or self.volume_step <= 0:
            return 0.0
        steps = math.floor(volume / self.volume_step + 1e-9)
        v = steps * self.volume_step
        decimals = max(0, -int(math.floor(math.log10(self.volume_step)))) if self.volume_step < 1 else 0
        return round(min(v, self.volume_max), decimals + 2)

    @property
    def min_stop_distance(self) -> float:
        return self.stops_level * self.point

    def loss_per_lot(self, stop_distance: float) -> float:
        """Account-currency loss for 1.0 lot moving `stop_distance` against us (tick math)."""
        tv = self.tick_value_loss or self.tick_value
        if self.tick_size <= 0:
            return 0.0
        return stop_distance / self.tick_size * tv


@dataclass
class AccountInfo:
    login: int
    server: str
    company: str
    currency: str
    leverage: int
    trade_mode: str            # DEMO / CONTEST / REAL
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float
    profit: float
    trade_allowed: bool = True
    trade_expert: bool = True
    margin_so_call: float = 0.0
    margin_so_so: float = 0.0
    hedging: bool = True
    raw: dict = field(default_factory=dict)


@dataclass
class Position:
    ticket: int
    symbol: str
    side: Side
    volume: float
    price_open: float
    sl: float
    tp: float
    price_current: float
    profit: float
    swap: float
    magic: int
    comment: str
    time: int
    identifier: int = 0


@dataclass
class Deal:
    ticket: int
    order: int
    position_id: int
    symbol: str
    side: Side
    entry: str                 # IN / OUT / INOUT / OUT_BY
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    fee: float
    magic: int
    comment: str
    time: int
    reason: int = 0            # MT5 DEAL_REASON_*: 3 expert, 4 SL, 5 TP, 6 stop-out


DEAL_REASON_NAMES = {0: "CLIENT", 1: "MOBILE", 2: "WEB", 3: "EXPERT", 4: "SL", 5: "TP", 6: "STOP_OUT",
                     7: "ROLLOVER", 8: "VMARGIN", 9: "SPLIT"}


@dataclass
class OrderRequest:
    action: str                # DEAL / SLTP / CLOSE
    symbol: str
    side: Optional[Side] = None
    volume: float = 0.0
    price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    deviation: int = 20
    magic: int = 0
    comment: str = ""
    position: int = 0
    filling: str = ""

    def to_dict(self) -> dict:
        return {"action": self.action, "symbol": self.symbol, "side": str(self.side) if self.side else None,
                "volume": self.volume, "price": self.price, "sl": self.sl, "tp": self.tp,
                "deviation": self.deviation, "magic": self.magic, "comment": self.comment,
                "position": self.position, "filling": self.filling}


@dataclass
class PendingOrderRequest:
    """A BUY_STOP / SELL_STOP resting order (used by the news straddle)."""
    symbol: str
    side: Side
    volume: float
    price: float               # trigger price
    sl: float = 0.0
    tp: float = 0.0
    magic: int = 0
    comment: str = ""
    expiration: int = 0        # broker epoch seconds; 0 = good till cancelled
    deviation: int = 20

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "side": str(self.side), "volume": self.volume, "price": self.price,
                "sl": self.sl, "tp": self.tp, "magic": self.magic, "comment": self.comment,
                "expiration": self.expiration, "deviation": self.deviation}


@dataclass
class PendingOrder:
    """A resting order as reported by the broker."""
    ticket: int
    symbol: str
    side: Side
    volume: float
    price: float
    sl: float
    tp: float
    magic: int
    comment: str
    time_setup: int = 0
    expiration: int = 0


@dataclass
class OrderResult:
    ok: bool
    retcode: Optional[int]
    retcode_text: str = ""
    comment: str = ""
    order: int = 0
    deal: int = 0
    volume: float = 0.0
    price: float = 0.0
    ambiguous: bool = False      # we do NOT know whether the broker acted -> reconcile
    already_closed: bool = False
    latency_ms: float = 0.0
    raw: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- strategy output
@dataclass
class TradePlan:
    direction: Side
    entry: float               # reference entry (ask for BUY / bid for SELL at signal time)
    sl: float
    tp: Optional[float]
    exit_model: dict
    setup_key: str             # identity of the market condition (prevents regenerating a stale signal)
    quality: float = 0.0
    ttl_seconds: int = 900
    features: dict = field(default_factory=dict)

    @property
    def stop_distance(self) -> float:
        return abs(self.entry - self.sl)

    def r_of(self, price: float) -> float:
        d = self.stop_distance
        return 0.0 if d <= 0 else (price - self.entry) * self.direction.sign / d


@dataclass
class Signal:
    id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    created_at: datetime        # UTC
    created_broker_time: float  # broker epoch seconds
    valid_until_broker: float
    snapshot_id: str
    plan: TradePlan
    regime: str
    session: str
    event_state: str
    edge_r: float = 0.0
    cost_r: float = 0.0
    confidence: float = 0.0
    shadow: bool = False
    meta: dict = field(default_factory=dict)


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default
