"""QUANTARA TRADING CORE.

Startup state machine (section 39) -> main loop (section 37):
  ticks -> validation -> spread stats -> fast health -> paper fills / partials
  bar closes (temporally confirmed) -> regime -> strategies -> gate pipeline
  -> execution -> reconciliation -> exits -> counterfactuals -> telemetry

Gate pipeline for every signal (first failing gate wins, every rejection is
recorded and followed as a counterfactual):
  STRATEGY_STATUS/REGIME -> MACRO/VOLUME -> KILL_SWITCH -> RISK STATE -> HEALTH -> SESSION ->
  EVENT -> PORTFOLIO -> SPREAD (WAIT or reject) -> COST/EDGE -> SIZE/MARGIN ->
  ENVIRONMENT (SHADOW) -> EXECUTION
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np

from app.microstructure.flow import FlowAnalyzer
from app.microstructure.book import BookTracker
from app.adaptive.optimizer import AdaptiveState, compute as adaptive_compute
from app.alpha.counterfactuals import CounterfactualTracker
from app.alpha.exits import with_execution_exits
from app.broker.base import BrokerAdapter, BrokerError
from app.broker.paper import PaperBroker
from app.config import ConfigError, ConfigManager, Secrets
from app.core.bridge import Bridge
from app.core.clock import BrokerClock
from app.core.symbol_control import SymbolTradingControl, parse_enabled
from app.core.types import (ACTIVE_TRADE_STATES, Environment, EventState, Gate, RiskState, Session, Side, Signal,
                            StartupState, StrategyHealth, StrategyStatus, Tick, TIMEFRAME_SECONDS, TradeState,
                            epoch_to_utc, new_id, utcnow)
from app.edge.cost import CostModel
from app.edge.expectancy import estimate_from_stats
from app.edge.gate import edge_gate
from app.execution.book import TradeBook, as_epoch
from app.execution.dispatcher import ExecutionDispatcher
from app.execution.engine import ExecutionEngine, OrderRouter
from app.execution.reconciliation import Reconciler
from app.execution.straddle import StraddleManager
from app.execution.trade_manager import TradeManager
from app.features.frame import FeatureFrame
from app.governance.health import BotStatusMonitor, FastHealth, StrategyHealthMonitor, strategy_health_key
from app.market.bars import BarStore, required_bars
from app.market.events import EconomicEvent, EventCalendar, symbol_currencies
from app.market.sessions import SessionClassifier
from app.market.ticks import SpreadCollector, TickValidator
from app.notifications.base import Notifier
from app.regime.detector import RegimeDetector, RegimeState
from app.regime.router import route
from app.risk.drawdown import DrawdownTracker, RiskSnapshot
from app.risk.correlation import rolling_h1_correlation
from app.risk.kill_switch import KillSwitch
from app.risk.sizing import SizingInputs, SizingResult, size_position
from app.storage.repo import Repository
from app.storage.writer import FallbackJournal, Ledger
from app.strategies.base import StrategyView
from app.strategies.registry import build as build_strategy
from app.market.symbols import contains as symbol_in, lookup as symbol_lookup, resolve as resolve_symbol_name

log = logging.getLogger("core")

# Selector input rows per (repo, strategy, version), shared by every core: the cores run round-robin on one thread,
# so without this each of 13 symbols would re-read the same cross-symbol history and stall the loop at once.
_SELECTOR_ROWS: Dict[tuple, tuple] = {}
SELECTOR_ROWS_TTL_SEC = 300.0


def _side_sign(direction) -> int:
    """+1 for a long, -1 for a short, from a Side or the string stored on a trade row.

    Anything unrecognised returns +1: the portfolio model treats same-direction legs as reinforcing,
    so defaulting to long is the reading that adds risk rather than quietly discounting it."""
    if isinstance(direction, Side):
        return direction.sign
    return -1 if str(direction).upper().endswith("SELL") else 1


class StartupError(Exception):
    pass


class _SyncTelemetry:
    """Telemetry sink that writes immediately (tests / simulation without a writer thread)."""

    def __init__(self, repo: Repository):
        self.repo = repo

    def enqueue(self, table: str, row: dict) -> None:
        try:
            self.repo.insert(table, row)
        except Exception as e:  # telemetry must never break trading
            log.debug("telemetry insert failed: %s", e)

    def enqueue_many(self, table: str, rows: List[dict]) -> None:
        for r in rows:
            self.enqueue(table, r)


class TradingCore:
    STORE_TFS = ["H4", "H1", "M15", "M5", "M1"]
    _broker_offset_warned = False          # one warning per process, not one per symbol core

    def __init__(self, cfgm: ConfigManager, data_broker: BrokerAdapter, repo: Repository, secrets: Secrets,
                 bridge: Optional[Bridge] = None, notifier: Optional[Notifier] = None, telemetry=None,
                 exec_broker: Optional[BrokerAdapter] = None, journal_path: Optional[Path] = None,
                 wall: Callable[[], float] = time.time, loop_interval: float = 0.25,
                 symbol_override: Optional[str] = None, shutdown_broker: bool = True,
                 symbol_control: Optional[SymbolTradingControl] = None):
        self.cfgm = cfgm
        self.cfg = cfgm.app
        self.env = Environment(self.cfg.environment)
        self.data = data_broker
        self.repo = repo
        self.secrets = secrets
        self.bridge = bridge or Bridge()
        self.notifier = notifier or Notifier()
        self.telemetry = telemetry or _SyncTelemetry(repo)
        self.wall = wall
        self.loop_interval = loop_interval
        self.symbol_override = symbol_override
        self.shutdown_broker = shutdown_broker
        # per-symbol Start/Stop switch; shared by every core when a MultiAssetRunner owns the cores
        self.symbol_control = symbol_control or SymbolTradingControl(repo)
        if exec_broker is not None:
            self.exec_broker = exec_broker
        elif self.env is Environment.DRY_RUN:
            cost = self.cfg.cost
            self.exec_broker = PaperBroker(data_broker, self.cfg.paper.starting_balance, self.cfg.paper.slippage_points,
                                           state_saver=lambda st: self.repo.set_flag("paper_state", st, "paper"),
                                           commission_per_lot=lambda sym: symbol_lookup(
                                               cost.symbol_commission_per_lot_round_trip, sym,
                                               cost.commission_per_lot_round_trip))
        else:
            self.exec_broker = data_broker
        self.paper = self.exec_broker if isinstance(self.exec_broker, PaperBroker) else None

        self.state = StartupState.BOOT
        self.clock = BrokerClock(self.cfg.broker.broker_utc_offset_hours, wall=wall)
        self.sessions = SessionClassifier(self.cfg.sessions)
        self.calendar = EventCalendar(self.cfg.events)
        self.health = FastHealth(self.cfg.health, wall=wall)
        self.status_monitor = BotStatusMonitor(wall=wall)
        self.shealth = StrategyHealthMonitor()
        self.kill = KillSwitch(lambda: self.repo.get_flag("kill_switch"),
                               lambda st: self.repo.set_flag("kill_switch", st, st.get("by", "system")))
        self.ledger = Ledger(repo, FallbackJournal(journal_path or Path("logs/critical_journal.jsonl")),
                             self._on_db_failure)
        self.book = TradeBook(self.ledger, emit=self.bridge.emit)
        # per-trade OPEN / CLOSE notifications, when the operator has switched them on
        from app.notifications.trade_alerts import attach as attach_trade_alerts
        self.trade_alerts = attach_trade_alerts(self.book, self.notifier, self.cfg.alerts)
        self.regime = RegimeDetector()
        self.regime_state = RegimeState()
        self.dd = DrawdownTracker(self.cfg.risk)
        self.risk: Optional[RiskSnapshot] = None
        self.cost = CostModel(self.cfg.cost)
        self.router = OrderRouter(self.cfg)
        self.validator = TickValidator(self.cfg.health.max_tick_jump_atr)
        self.strategies = {}
        self.symbol: str = ""
        self.spec = None
        self.stores: Dict[str, BarStore] = {}
        self.ff: Dict[str, FeatureFrame] = {}
        self.spread: Optional[SpreadCollector] = None
        self.exec: Optional[ExecutionEngine] = None
        self.execution_dispatcher: Optional[ExecutionDispatcher] = None
        self.recon: Optional[Reconciler] = None
        self.manager: Optional[TradeManager] = None
        self.cf: Optional[CounterfactualTracker] = None
        self.straddle: Optional[StraddleManager] = None
        self.adaptive = AdaptiveState("")
        self.flow: Optional[FlowAnalyzer] = None
        self.lab_hint: dict = {}
        self.lab_hint_created_at = None
        # Per-strategy research for THIS symbol: {strategy_id: entry}. Populated from the lab's
        # "strategies" block when research_all_strategies is on. micro_scalper keeps using lab_hint so its
        # behaviour is unchanged; the rest are recorded and only block if added to gate_strategies.
        self.strategy_lab_hints: dict = {}
        self.strategy_lab_created_at = None
        # {strategy_id: Allocation} for THIS symbol from the strategy selector; restored on startup so a
        # benched strategy stays benched across restarts.
        self.allocations: dict = {}
        self._ml = None
        self.book_tracker = BookTracker()
        self._book_next = 0.0
        self.pending: List[dict] = []          # WAIT-routed signals
        self.last_tick: Optional[Tick] = None
        self._prev_tick_msc: Optional[int] = None
        self._seen_first_tick = False
        self.account = None
        self._account_refresh_ok = False
        self._last_loss_streak = 0
        self._loss_cooldown_active = False
        self.positions_cache: list = []
        self.diagnostics: Dict[str, List[str]] = {}
        # (bar_time, reason) of the last NO_SETUP row written per strategy, so the same reason on the same bar is
        # recorded once instead of once per runner loop. See _evaluate_strategies.
        self._no_setup_seen: Dict[str, tuple] = {}
        self.last_signal: Optional[dict] = None
        self._deferred_m15_since: Optional[float] = None
        self._timers: Dict[str, float] = {}
        self._uncertain = False
        self._consecutive_errors = 0
        self._last_risk_state: Optional[str] = None
        self._strategy_stats: Dict[str, dict] = {}
        self._today_realized = 0.0
        self._last_summary_day: Optional[str] = None
        self._global_positions_known = False
        self.stop_event = threading.Event()
        self._last_disconnect_alert = 0.0

    # ================================================================== helpers
    def _emit(self, event: str, payload: dict) -> None:
        self.bridge.emit(event, payload)

    def _alert(self, level: str, text: str) -> None:
        self.notifier.alert(level, text)
        self._emit("health_event", {"level": level, "message": text})

    def _health_row(self, component: str, status: str, message: str, details: Optional[dict] = None) -> None:
        self.telemetry.enqueue("system_health", {"created_at": utcnow(), "component": component, "status": status,
                                                 "startup_state": str(self.state), "message": message,
                                                 "details": details or {}})

    def _set_state(self, st: StartupState, message: str = "") -> None:
        self.state = st
        log.info("STARTUP -> %s %s", st, message)
        self._health_row("CORE", "STARTUP", f"{st} {message}".strip())
        self._emit("health_event", {"startup_state": str(st), "message": message})
        self.bridge.update_state(startup_state=str(st))

    def _on_db_failure(self, msg: str) -> None:
        self.health.db_ok = False
        self.kill.engage_volatile("DATABASE_WRITE_FAILED")
        self._alert("CRITICAL", "Could not save to the database, so new trades are stopped - the bot will not trade what it cannot record. Technical detail: " + str(msg)[:200])

    def _every(self, name: str, seconds: float) -> bool:
        now = self.wall()
        if now - self._timers.get(name, 0.0) >= seconds:
            self._timers[name] = now
            return True
        return False

    def server_now(self) -> float:
        st = self.data.server_time()
        if st:
            return st
        return self.last_tick.time if self.last_tick else 0.0

    def now_utc(self) -> datetime:
        sn = self.server_now()
        return self.clock.to_utc(sn) if sn else utcnow()

    # ================================================================== startup
    def start(self) -> None:
        try:
            self._startup()
        except Exception as e:
            self.state = StartupState.FAILED
            self._health_row("CORE", "FAILED", str(e))
            self._alert("CRITICAL", f"Startup failed in {self.state}: {e}")
            self.bridge.update_state(startup_state="FAILED", startup_error=str(e))
            raise

    def _connect(self) -> None:
        attempts = self.cfg.broker.reconnect_max_attempts
        for n in range(1, attempts + 1):
            try:
                self.data.connect()
                self.health.broker_connected = True
                return
            except Exception as e:
                log.warning("connect attempt %d/%d failed: %s", n, attempts, e)
                if n == attempts:
                    raise StartupError(f"cannot connect to broker: {e}")
                time.sleep(min(30, 2 ** n) if self.wall is time.time else 0)

    def _startup(self) -> None:
        if self.env is Environment.REAL:
            raise StartupError("REAL execution is disabled in this release; use DEMO or DRY_RUN")
        self._set_state(StartupState.CONNECTING)
        self._connect()
        self._set_state(StartupState.MT5_CONNECTED, str(self.data.terminal_info()))
        ti = self.data.terminal_info()
        if self.env in (Environment.DEMO, Environment.REAL) and ti and ti.get("trade_allowed") is False:
            raise StartupError("Algo Trading is disabled in the MT5 terminal (press the 'Algo Trading' button)")

        # ---------------- ACCOUNT
        self._set_state(StartupState.ACCOUNT_VALIDATION)
        acct = self.data.account_info()
        if self.env is Environment.DEMO and acct.trade_mode != "DEMO":
            raise StartupError(f"environment DEMO but the MT5 account is {acct.trade_mode}. Refusing to start.")
        if self.env is Environment.REAL:
            if acct.trade_mode != "REAL":
                raise StartupError(f"environment REAL but the account is {acct.trade_mode}")
            if self.secrets.confirm_real != self.secrets.REAL_CONFIRMATION_PHRASE:
                raise StartupError("REAL trading requires QUANTARA_CONFIRM_REAL=I_ACCEPT_REAL_MONEY_RISK in .env")
            bad = [s.id for s in self.cfgm.strategies.strategies if s.enabled and
                   s.status in ("MICRO_LIVE", "VALIDATED") and not s.stats]
            if bad:
                raise StartupError(f"strategies {bad} are promoted without an evidence package")
        self.ledger.upsert("accounts", {"login": acct.login, "server": acct.server, "company": acct.company,
                                        "currency": acct.currency, "leverage": acct.leverage,
                                        "trade_mode": acct.trade_mode, "hedging": acct.hedging,
                                        "environment": str(self.env), "last_seen": utcnow(), "raw": acct.raw},
                           ["login", "server"], ["company", "currency", "leverage", "trade_mode", "hedging",
                                                 "environment", "last_seen", "raw"])
        if self.paper is not None:
            saved = self.repo.get_flag("paper_state")
            if saved:
                self.paper.import_state(saved)
                log.info("paper broker state restored (%d positions)", len(saved.get("positions", [])))

        # ---------------- SYMBOL
        self._set_state(StartupState.SYMBOL_VALIDATION)
        chosen = None
        candidates = [self.symbol_override] if self.symbol_override else self.cfg.broker.symbol_candidates
        for cand in candidates:
            try:
                # the same instrument has another name on another account type (XAUUSDm / XAUUSD)
                cand = resolve_symbol_name(self.data, cand) or cand
                if self.data.select_symbol(cand):
                    spec = self.data.symbol_spec(cand)
                    if self.env in (Environment.DEMO, Environment.REAL) and spec.trade_mode != "FULL":
                        log.warning("%s trade mode %s - skipping", cand, spec.trade_mode)
                        continue
                    chosen = spec
                    break
            except Exception as e:
                log.info("symbol %s not usable: %s", cand, e)
        if chosen is None:
            found = self.data.find_symbols("*XAU*")
            raise StartupError(f"none of {candidates} available. Broker has: {found[:20]}")
        self.spec = chosen
        self.symbol = chosen.name
        self.calendar.scope_to(symbol_currencies(chosen))     # this symbol's releases only (EURUSD: EUR + USD)
        self.sessions.always_open = symbol_in(self.cfg.broker.always_open_symbols, self.symbol)
        if self.sessions.always_open:
            log.info("%s is configured as an always-open (24/7) symbol - weekend closure not applied", self.symbol)
        self.symbol_control.load()
        self.adaptive = AdaptiveState(self.symbol)
        mcfg = self.cfg.microstructure
        self.flow = FlowAnalyzer(self.spec.point, mcfg.bucket_ticks, mcfg.vpin_buckets, mcfg.window_ticks, mcfg.burst_z,
                                 mcfg.bin_points) if mcfg.enabled else None
        try:
            self.adaptive.restore(self.repo.get_flag(f"adaptive:{self.symbol}"))
        except Exception as e:
            log.info("adaptive state not restored: %s", e)
        self._restore_selector()
        self.ledger.insert("symbols", {"captured_at": utcnow(), "account_login": acct.login, "symbol": chosen.name,
                                       "digits": chosen.digits, "point": chosen.point, "tick_size": chosen.tick_size,
                                       "tick_value": chosen.tick_value, "contract_size": chosen.contract_size,
                                       "volume_min": chosen.volume_min, "volume_step": chosen.volume_step,
                                       "volume_max": chosen.volume_max, "stops_level": chosen.stops_level,
                                       "freeze_level": chosen.freeze_level, "filling_modes": chosen.filling_mode_flags,
                                       "execution_mode": chosen.execution_mode, "trade_mode": chosen.trade_mode,
                                       "swap_long": chosen.swap_long, "swap_short": chosen.swap_short,
                                       "currency_profit": chosen.currency_profit, "spec": chosen.raw or chosen.__dict__})
        self.spread = SpreadCollector(point=chosen.point)
        try:
            hist = self.repo.select("ticks", {"symbol": self.symbol}, order_by="id", desc=True, limit=20000)
            self.spread.seed([(r.get("session") or "ASIA", float(r["spread_points"])) for r in hist
                              if r.get("spread_points") is not None])
        except Exception as e:
            log.info("no spread history to seed: %s", e)

        # ---------------- strategies (needed for lookbacks)
        self.strategies = {c.id: build_strategy(c) for c in self.cfgm.strategies.strategies}

        # ---------------- DATA HYDRATION
        self._set_state(StartupState.DATA_HYDRATION)
        lookbacks: Dict[str, List[int]] = {tf: [] for tf in self.STORE_TFS}
        required: Dict[str, List[int]] = {tf: [] for tf in self.STORE_TFS}   # what must exist to START
        for s in self.strategies.values():
            for tf, n in s.lookbacks().items():
                lookbacks.setdefault(tf, []).append(n)
            for tf, n in s.min_history().items():
                required.setdefault(tf, []).append(n)
        lookbacks["H1"].append(self.regime.lookback())
        h = self.cfg.hydration
        tick = self.data.tick(self.symbol)
        server_now = tick.time if tick else self.data.server_time()
        for tf in self.STORE_TFS:
            need = required_bars(lookbacks.get(tf) or [50], h.warmup_bars, h.safety_margin, h.min_bars,
                                 h.ema_warmup_multiple)
            store = BarStore(self.symbol, tf, maxlen=max(need + 200, 1000))
            series = self.data.rates(self.symbol, tf, need + 1)
            got = store.hydrate(series, server_now)
            self.stores[tf] = store
            # H4: fetch for the longest lookback, but refuse to start only below what a strategy cannot do without
            # (trend_daily_v1 asks for ~3,900 H4 bars and needs none - it reports INSUFFICIENT_HISTORY instead).
            min_required = max(required.get("H4") or [1]) if tf == "H4" else need * 0.9
            if tf in ("H4", "H1", "M15") and got < min_required:
                raise StartupError(f"{tf}: only {got} closed bars available, {min_required:g} required. "
                                   f"Open the {self.symbol} {tf} chart in MT5 to download history.")
            self._health_row("DATA", "OK", f"hydrated {tf}: {got}/{need}")

        # ---------------- INDICATORS / STRUCTURE
        self._set_state(StartupState.INDICATOR_INITIALIZATION)
        self._rebuild("H1")
        self._rebuild("H4")
        self._rebuild("M15")
        k = self.ff["H1"].last
        if not np.isfinite(self.ff["H1"].ema(200)[k]) or not np.isfinite(self.ff["M15"].atr(14)[self.ff["M15"].last]):
            raise StartupError("indicators not warm after hydration")
        self._set_state(StartupState.STRUCTURE_INITIALIZATION)
        for j in range(max(0, k - 10), k + 1):
            self.regime_state = self.regime.update(self.ff["H1"], j)
        self.bridge.update_state(regime=self.regime_state.to_dict())

        # ---------------- events calendar
        try:
            rows = self.repo.select("economic_events")
            self.calendar.set_events([EconomicEvent(r["event_time"] if isinstance(r["event_time"], datetime)
                                                    else datetime.fromisoformat(str(r["event_time"])),
                                                    r["currency"], r["impact"], r["title"]) for r in rows])
        except Exception as e:
            log.warning("economic calendar not loaded: %s", e)

        # ---------------- RECONCILIATION
        self._set_state(StartupState.BROKER_STATE_RECONCILIATION)
        self.cf = CounterfactualTracker(self.ledger, chosen.point, self.cfg.cost.default_slippage_points,
                                        self.cfg.counterfactual.max_holding_bars,
                                        self.cfg.counterfactual.enabled and self.cfg.research.counterfactual_enabled)
        self.recon = Reconciler(self.exec_broker, self.book, self.symbol, self.cfg.broker.magic_number,
                                str(self.env), self._alert, self.health, wall=self.wall)
        if self.env is not Environment.SHADOW:
            self.exec = ExecutionEngine(self.exec_broker, self.book, self.cfg, self.env, self.cfg.broker.magic_number,
                                        self.symbol, lambda: self.exec_broker.symbol_spec(self.symbol),
                                        self.health.on_latency,
                                        self.health.on_exec_failure, self.cost.slippage.add, self._mark_uncertain)
            self.exec.reserve_risk = self._reserve_portfolio_risk
            self.execution_dispatcher = ExecutionDispatcher({("MT5", "*"): self.exec})
            self.manager = TradeManager(self.exec_broker, self.book, self.recon, self.symbol,
                                        self.cfg.broker.magic_number, self.cfg.execution.deviation_points,
                                        lambda: self.exec_broker.symbol_spec(self.symbol), self._mark_uncertain,
                                        quick_profit_close_money=self.cfg.execution.quick_profit_close_money,
                                        profit_lock_floor_money=self.cfg.execution.profit_lock_floor_money,
                                        commission_per_lot_round_trip=self.commission_rt(),
                                        slippage_points=self.cfg.cost.default_slippage_points,
                                        profit_lock_stop_buffer_points=self.cfg.execution.profit_lock_stop_buffer_points,
                                        hard_profit_target_money=self.cfg.execution.hard_profit_target_money,
                                        profit_giveback_fraction=self.cfg.execution.profit_giveback_fraction,
                                        small_win_activation_money=self.cfg.execution.small_win_activation_money,
                                        small_win_floor_money=self.cfg.execution.small_win_floor_money,
                                        ambition_activation_money=self.cfg.execution.ambition_activation_money,
                                        ambition_floor_money=self.cfg.execution.ambition_floor_money,
                                        protection_safety_buffer_points=self.cfg.execution.protection_safety_buffer_points,
                                        sl_modification_cooldown_sec=self.cfg.execution.sl_modification_cooldown_sec,
                                        close_when_profit_lock_unprotectable=self.cfg.execution.close_when_profit_lock_unprotectable,
                                        clock=self.wall)
            self.manager.floor_r = self._floor_r()
            self.manager.min_sl_step_r = float(self.cfg.execution.min_sl_step_r)
            self.manager.profit_lock_cost_multiple = float(self.cfg.execution.profit_lock_cost_multiple)
            self.manager.peak_persist_step_r = float(self.cfg.execution.peak_persist_step_r)
        active = self.repo.select("trades", {"state": [str(s) for s in ACTIVE_TRADE_STATES]})
        self.book.load_active([a for a in active if a.get("symbol") in (None, self.symbol)])
        summary = self.recon.run(startup=True)
        log.info("startup reconciliation: %s", summary)
        self._health_row("RECONCILIATION", "OK" if summary.get("status") == "OK" else "UNCERTAIN", str(summary))
        if summary.get("status") != "OK":
            raise StartupError("broker positions could not be read - refusing to trade blind")
        for t in self.book.open_positions():
            self.book.ensure_mstate(t)
        self._restore_counterfactuals()
        if self.exec is not None:
            self.straddle = self._build_straddle()
            self.straddle.recover()
        # observed slippage history
        try:
            for r in self.repo.select("execution_events", {"event_type": "FILL"}, order_by="id", desc=True, limit=300):
                if r.get("slippage_points") is not None:
                    self.cost.slippage.add(float(r["slippage_points"]))
        except Exception:
            pass

        # ---------------- STRATEGY VALIDATION
        self._set_state(StartupState.STRATEGY_VALIDATION)
        since = utcnow() - timedelta(days=3)
        for c in self.cfgm.strategies.strategies:
            s = self.strategies[c.id]
            self.ledger.upsert("strategy_versions", {"strategy_id": c.id, "version": c.version,
                                                     "params_hash": s.params_hash, "params": s.params,
                                                     "exit_model": s.exit_model, "status": c.status,
                                                     "evidence_level": (c.stats or {}).get("evidence_level",
                                                                                           "INSUFFICIENT"),
                                                     "stats": c.stats or {}, "updated_at": utcnow()},
                               ["strategy_id", "version", "params_hash"], ["status", "evidence_level", "stats",
                                                                           "updated_at"])
            for name, p in c.params.items():
                self.ledger.upsert("parameters", {"strategy_id": c.id, "name": name, "value": p.value,
                                                  "minimum": p.minimum, "maximum": p.maximum, "step": p.step,
                                                  "economic_hypothesis": p.hypothesis, "updated_at": utcnow()},
                                   ["strategy_id", "name"])
            try:
                for r in self.repo.select("signals", {"strategy_id": c.id}, order_by="created_at", desc=True,
                                          limit=200):
                    if r.get("setup_key") and as_epoch(r.get("created_at")) >= since.timestamp():
                        s.remember_setup(r["setup_key"], int(self.server_now()))
            except Exception as e:
                log.info("setup history unavailable: %s", e)
        self._refresh_strategy_stats()

        # ---------------- RISK VALIDATION
        self._set_state(StartupState.RISK_VALIDATION)
        self.kill.restore()
        saved = self.repo.get_flag("risk_tracker")
        if saved:
            self.dd.restore(saved)
        self._refresh_account()
        self._recover_risk_reservations()
        self._update_risk(force_persist=True)
        if self.risk and self.risk.state is RiskState.EMERGENCY:
            self.kill.engage("EMERGENCY risk state at startup", "risk")
        self._set_state(StartupState.READY)
        self._set_state(StartupState.ACTIVE, f"{self.env} {self.symbol}")
        self._alert("INFO", f"Trading core ACTIVE - {self.env} on {self.symbol} "
                            f"(kill switch {'ENGAGED' if self.kill.blocking else 'off'})")
        self._publish()

    def _restore_counterfactuals(self) -> None:
        if self.cf is None:
            return
        try:
            rows = self.repo.select("counterfactuals", {"status": "OPEN", "symbol": self.symbol})
        except Exception:
            return
        if not rows:
            return

        def bar_time_of(r):
            sec = self.clock.from_utc(r["created_at"] if isinstance(r["created_at"], datetime)
                                      else datetime.fromisoformat(str(r["created_at"])))
            return int(sec // 900 * 900) - 900
        self.cf.restore(rows, bar_time_of)
        ff = self.ff["M15"]
        bars = self.stores["M15"].to_list()
        earliest = min(c.signal_bar_time for c in self.cf.open.values()) if self.cf.open else None
        if earliest is None:
            return
        for idx, b in enumerate(bars):
            if b.time > earliest:
                self.cf.on_bar(b, ff, idx, str(self.regime_state.trend), utcnow())
        log.info("counterfactuals restored; %d still open", len(self.cf.open))

    # ================================================================== features
    def _rebuild(self, tf: str) -> FeatureFrame:
        ff = FeatureFrame.from_bars(self.stores[tf].to_list(), tf)
        self.ff[tf] = ff
        return ff

    # ================================================================== main loop
    def run(self) -> None:
        log.info("main loop started")
        while not self.stop_event.is_set():
            t0 = time.perf_counter()
            try:
                self.step()
                self._consecutive_errors = 0
            except Exception as e:
                self._consecutive_errors += 1
                log.exception("loop error: %s", e)
                if self._consecutive_errors == 10:
                    self.kill.engage_volatile("REPEATED_LOOP_ERRORS")
                    self._alert("CRITICAL", "Something keeps going wrong inside the bot, so new trades are stopped until it is looked at. Technical detail: " + str(e)[:200])
            dt = time.perf_counter() - t0
            self.stop_event.wait(max(0.0, self.loop_interval - dt))
        self.shutdown()

    def step(self) -> None:
        self._process_commands()
        if self._every("conn", 5.0):
            ok = self.data.is_connected()
            if not ok and self.health.broker_connected:
                self._alert("CRITICAL", "Lost the connection to MetaTrader. No new trades until it is back. Positions already open still have their stop and target sitting at the broker.")
            self.health.broker_connected = ok
            if not ok:
                self._try_reconnect()
                self._publish()
                return
        tick = self.data.tick(self.symbol)
        if tick is not None:
            self._on_tick(tick)
        if self._every("bars", 1.0):
            self._poll_bars()
        if self._every("positions", self.cfg.execution.position_sync_sec):
            self._refresh_account()
        if self.cfg.adaptive.enabled and self.state is StartupState.ACTIVE and \
                self._every("adaptive", self.cfg.adaptive.interval_minutes * 60.0):
            self._run_adaptive()
        if self.flow is not None and self.cfg.microstructure.depth_enabled:
            self._poll_book()
        if self.straddle is not None and self._every("straddle", 1.0):
            self.straddle.step()
        if self.recon is not None and (self._uncertain or self._every("recon", self.cfg.execution.reconcile_interval_sec)):
            self._uncertain = False
            self.recon.run()
        if self._every("risk", 5.0):
            self._update_risk()
        if self._every("telemetry", 2.0):
            self._flush_spreads()
        if self._every("minute", 60.0):
            self._minute_tasks()
        if self._every("publish", 1.0):
            self._publish()

    def _mark_uncertain(self, trade: dict) -> None:
        self._uncertain = True

    def _try_reconnect(self) -> None:
        if not self._every("reconnect", 15.0):
            return
        try:
            self.data.connect()
            self.health.broker_connected = True
            self._alert("INFO", "Reconnected to MetaTrader. Checking that its record of your positions matches mine.")
            if self.recon:
                self.recon.run()
        except Exception as e:
            log.warning("reconnect failed: %s", e)

    # ================================================================== ticks
    def _on_tick(self, tick: Tick) -> None:
        atr = None
        m15 = self.ff.get("M15")
        if m15 is not None and len(m15):
            a = m15.atr(14)[m15.last]
            atr = float(a) if np.isfinite(a) else None
        fresh = tick.time_msc != self._prev_tick_msc
        if not fresh:
            return
        chk = self.validator.check(tick, atr)
        self.health.on_tick(tick.time_msc, chk.ok)
        if not chk.ok:
            if chk.reason in ("PRICE_JUMP", "CROSSED_QUOTE", "NON_POSITIVE_PRICE"):
                self.health.on_data_issue()
                log.warning("tick rejected: %s %s", chk.reason, tick)
            return
        if self._seen_first_tick:
            self.clock.observe(tick.time, fresh=True)
            if self.clock.offset_seconds and not TradingCore._broker_offset_warned:
                TradingCore._broker_offset_warned = True
                hours = self.clock.offset_seconds / 3600
                log.warning("broker server clock is UTC%+.2g h. Rules that read the hour or the day straight from "
                            "bar times (session_breakout_v1, the daily bars of trend_daily_v1 / trend_ensemble_v1 / "
                            "turn_of_month_v1, intraday momentum) assume bars stamped in UTC, as Exness stamps them; "
                            "on this server they act %g h early.", hours, hours)
        self._seen_first_tick = True
        self._prev_tick_msc = tick.time_msc
        self.last_tick = tick
        utc = self.clock.to_utc(tick.time)
        session = self.sessions.classify(utc)
        self.health.market_closed = session is Session.CLOSED
        self.health.weekend_open = session is Session.WEEKEND
        if self.flow is not None:
            try:      # research-only analytics must never be able to break the trading loop
                self.flow.on_tick(tick.bid, tick.ask, utc)
            except Exception:
                log.exception("order-flow analytics failed; disabling for %s", self.symbol)
                self.flow = None
        sp = self.spread.observe(tick, str(session), utc.isoformat(),
                                 {"symbol": self.symbol, "volatility_state": str(self.regime_state.vol),
                                  "event_state": str(self.calendar.state(utc))})
        self.health.on_spread(sp, self.spread.median(str(session)))
        if self.paper is not None:
            closed = self.paper.on_tick(tick, self.symbol)
            if closed:
                self._uncertain = True      # reconcile now to book the broker-side exits
        if self.manager is not None:
            self.manager.on_tick(tick)
        if self.pending:
            self._process_pending(tick)

    def _flush_spreads(self) -> None:
        if self.spread is None:
            return
        rows = self.spread.drain()
        for r in rows:
            r["time"] = datetime.fromisoformat(r["time"])
        if rows:
            self.telemetry.enqueue_many("ticks", rows)

    # ================================================================== bars
    def _poll_bars(self) -> None:
        server_now = self.server_now()
        if not server_now:
            return
        new_by_tf = {}
        for tf in self.STORE_TFS:
            try:
                new = self.stores[tf].poll(lambda n, tf=tf: self.data.rates(self.symbol, tf, n), server_now)
            except Exception as e:
                log.warning("bar poll %s failed: %s", tf, e)
                new = []
            if new:
                new_by_tf[tf] = new
                if tf in ("H4", "H1", "M15"):
                    for b in new:
                        self.ledger.upsert("bars", {"symbol": self.symbol, "timeframe": tf,
                                                    "time": self.clock.to_utc(b.time), "open": b.open, "high": b.high,
                                                    "low": b.low, "close": b.close, "tick_volume": b.tick_volume,
                                                    "spread_points": b.spread, "real_volume": b.real_volume,
                                                    "source": self.data.name.upper()},
                                           ["symbol", "timeframe", "time"], [])
        issues = sum(1 for tf in self.stores for i in self.stores[tf].issues if i.kind in ("CORRUPT", "OUT_OF_ORDER"))
        if issues > getattr(self, "_issue_count", 0):
            self.health.on_data_issue()
        self._issue_count = issues
        if "H1" in new_by_tf:
            ff = self._rebuild("H1")
            for _ in new_by_tf["H1"]:
                pass
            self.regime_state = self.regime.update(ff, ff.last, spread_shock=self.health.spread_shock)
            self._emit("strategy_state", {"regime": self.regime_state.to_dict()})
        if "H4" in new_by_tf:
            self._rebuild("H4")
        if "M15" in new_by_tf or self._deferred_m15_since is not None:
            self._on_m15_close(new_by_tf.get("M15", []))
        if "M1" in new_by_tf:
            self._rebuild("M1")
            self._evaluate_strategies()

    def _on_m15_close(self, new_bars: list) -> None:
        m15 = self.stores["M15"]
        h1 = self.stores["H1"]
        last_m15 = m15.bars[-1]
        close_time = last_m15.time + 900
        expected_h1_open = (close_time // 3600) * 3600 - 3600
        # an H1 bar is only expected if the market actually traded in that hour (weekend gaps)
        hour_traded = any(expected_h1_open <= b.time < expected_h1_open + 3600 for b in list(m15.bars)[-8:])
        if hour_traded and h1.last_time is not None and h1.last_time < expected_h1_open:
            # the H1 bar that just closed is not confirmed yet - wait (temporal integrity)
            if self._deferred_m15_since is None:
                self._deferred_m15_since = self.wall()
                self._pending_m15_bars = list(new_bars)
                return
            if self.wall() - self._deferred_m15_since < 90:
                self._pending_m15_bars.extend(new_bars)
                return
            log.warning("H1 bar %s not available after 90s - evaluating with previous H1", expected_h1_open)
        bars = (getattr(self, "_pending_m15_bars", []) or []) + [b for b in new_bars
                                                                 if b not in getattr(self, "_pending_m15_bars", [])]
        self._deferred_m15_since = None
        self._pending_m15_bars = []
        ff = self._rebuild("M15")
        idx_of = {b.time: i for i, b in enumerate(m15.bars)}
        now = utcnow()
        trend = str(self.regime_state.trend)
        for b in bars:
            i = idx_of.get(b.time)
            if self.cf is not None and i is not None:
                for res in self.cf.on_bar(b, ff, i, trend, now):
                    self._emit("rejection_event", {"counterfactual_resolved": res["id"], "gate": res["gate"],
                                                   "hypothetical_r": res["hypothetical_r"]})
        if self.manager is not None and bars:
            spread_price = (self.last_tick.spread if self.last_tick else bars[-1].spread * self.spec.point)
            self.manager.on_bar_close(bars[-1], ff, ff.last, trend, spread_price)
        self._evaluate_strategies()

    # ================================================================== strategies
    def _symbol_enabled(self) -> bool:
        return self.symbol_control.is_enabled(self.symbol)

    def _evaluate_strategies(self) -> None:
        if not self._symbol_enabled():
            # operator pressed STOP for this symbol: no strategy evaluation, no entries
            for c in self.cfgm.strategies.strategies:
                self.diagnostics[c.id] = ["SYMBOL_TRADING_STOPPED"]
            if self._every("symbol_stopped_eval", 900.0):
                for c in self.cfgm.strategies.strategies:
                    self._record_candidate_evaluation(c.id, "SKIPPED", "SYMBOL_CONTROL", "SYMBOL_TRADING_STOPPED")
            return
        tick = self.last_tick
        if tick is None or self.health.market_closed:
            reason = "NO_VALID_TICK" if tick is None else "MARKET_CLOSED"
            for c in self.cfgm.strategies.strategies:
                self._record_candidate_evaluation(c.id, "SKIPPED", "PRECONDITION", reason)
            return
        ff_m, ff_h = self.ff["M15"], self.ff["H1"]
        if len(ff_m) < 50 or len(ff_h) < 50:
            for c in self.cfgm.strategies.strategies:
                self._record_candidate_evaluation(c.id, "SKIPPED", "PRECONDITION", "INSUFFICIENT_HISTORY")
            return
        utc = self.clock.to_utc(tick.time)
        session = self.sessions.classify(utc)
        regime = self.regime.with_fast_stress(self.health.spread_shock)
        routes = route(self.cfgm.strategies.strategies, regime, self.env, self.shealth.state,
                       self.cfg.research.shadow_strategies_enabled,
                       self.allocations if self.cfg.selector.mode == "auto" else None)
        ff_m1 = self.ff.get("M1")
        ff_h4 = self.ff.get("H4")
        m15_close = int(ff_m.t[ff_m.last]) + 900
        h4_index = (int(np.searchsorted(ff_h4.t + 14400, m15_close, side="right") - 1)
                    if ff_h4 is not None else None)
        spread_ok, spread_why = self._spread_ok(tick, str(session))
        view = StrategyView(m15=ff_m, i=ff_m.last, h1=ff_h, k=ff_h.last,
                            h4=ff_h4, q=h4_index,
                            symbol=self.symbol,
                            m1=ff_m1, j=ff_m1.last if ff_m1 else None,
                            point=self.spec.point,
                            spread_price=tick.spread, bid=tick.bid, ask=tick.ask,
                            regime_trend=str(regime.trend), session=str(session),
                            spread_safe=spread_ok, spread_why="" if spread_ok else spread_why,
                            min_stop_distance=self.spec.min_stop_distance)
        snapshot_id = new_id()
        atr = float(ff_m.atr(14)[ff_m.last])
        snap = {"symbol": self.symbol, "bid": tick.bid, "ask": tick.ask, "spread_points": tick.spread / self.spec.point,
                "m15_bar": int(ff_m.t[ff_m.last]), "h1_bar": int(ff_h.t[ff_h.last]), "atr_m15": atr,
                "regime": regime.to_dict(), "session": str(session), "event_state": str(self.calendar.state(utc)),
                "risk_state": str(self.risk.state) if self.risk else None}
        self.telemetry.enqueue("market_snapshots", {"id": snapshot_id, "created_at": utcnow(), "symbol": self.symbol,
                                                    "data": snap})
        bar_time = int(ff_m1.t[ff_m1.last]) if ff_m1 else int(ff_m.t[ff_m.last])
        for c in self.cfgm.strategies.strategies:
            # A strategy's `symbols` list was never enforced: every strategy ran on every symbol. trend_daily_v1
            # leaves out natural gas (its decade replay lost -0.40R a trade, swap alone 0.23R), so it is now.
            if c.symbols and "*" not in c.symbols and not symbol_in(c.symbols, self.symbol):
                self.diagnostics[c.id] = ["NOT_CONFIGURED_FOR_THIS_SYMBOL"]
                continue
            strat = self.strategies.get(c.id)
            rr = routes.get(c.id)
            if strat is None or rr is None:
                self._record_candidate_evaluation(c.id, "NOT_ROUTED", "ROUTING", "STRATEGY_OR_ROUTE_UNAVAILABLE",
                                                  bar_time)
                continue
            if not rr.executable and not rr.shadow:
                self.diagnostics[c.id] = [f"NOT_EVALUATED: {rr.reason}"]
                self._record_candidate_evaluation(c.id, "SKIPPED", "ROUTING", rr.reason, bar_time,
                                                  details={"eligibility": str(rr.eligibility)})
                continue
            try:
                res = strat.evaluate(view)
            except Exception as e:
                log.exception("strategy %s failed: %s", c.id, e)
                self.diagnostics[c.id] = [f"ERROR: {e}"]
                self._record_candidate_evaluation(c.id, "EVALUATION_ERROR", "STRATEGY", str(e), bar_time)
                continue
            self.diagnostics[c.id] = res.reasons or ["SIGNAL"]
            if res.plan is None:
                reason = "|".join(res.reasons) or "NO_SETUP_REASON_UNAVAILABLE"
                # One row per bar, not one per loop. The runner cycles every 0.25s, so a symbol is re-evaluated
                # roughly every 3s and a strategy that is simply waiting would write ~26k identical rows a day -
                # millions a day across 8 strategies and 13 symbols. The reason is a property of the BAR, so a
                # row per (strategy, bar, reason) loses nothing a query would ever ask for. Any CHANGE of reason
                # within a bar is still recorded, so a rule that flips state mid-bar is not hidden.
                seen = self._no_setup_seen.get(c.id)
                if seen != (bar_time, reason):
                    self._no_setup_seen[c.id] = (bar_time, reason)
                    self._record_candidate_evaluation(c.id, "NO_SETUP", "STRATEGY", reason, bar_time,
                                                      details={"strategy_reasons": res.reasons})
                continue
            cooldown = int(c.params.get("setup_cooldown_bars").value * 900) if "setup_cooldown_bars" in c.params else 0
            if strat.is_duplicate_setup(res.plan.setup_key, bar_time, cooldown):
                self.diagnostics[c.id] = ["DUPLICATE_SETUP (already acted on)"]
                self._record_candidate_evaluation(c.id, "DEDUPLICATED", "DEDUPLICATION",
                                                  "DUPLICATE_SETUP", bar_time,
                                                  details={"setup_key": res.plan.setup_key})
                continue
            strat.remember_setup(res.plan.setup_key, bar_time)
            now_b = self.server_now()
            evaluation_id = self._record_candidate_evaluation(
                c.id, "CANDIDATE", "STRATEGY", "", bar_time,
                details={"strategy_reasons": res.reasons})
            # The exit as it will actually be TRADED (breakeven + profit lock from `execution`), so the trade, its
            # counterfactual and its stored exit_plan all carry the same rules the trade manager applies.
            res.plan.exit_model = with_execution_exits(res.plan.exit_model, self.cfg.execution)
            sig = Signal(id=new_id(), strategy_id=c.id, strategy_version=c.version, symbol=self.symbol,
                         created_at=utcnow(), created_broker_time=now_b,
                         valid_until_broker=now_b + res.plan.ttl_seconds, snapshot_id=snapshot_id, plan=res.plan,
                         regime=regime.label, session=str(session), event_state=snap["event_state"],
                         confidence=res.plan.quality,
                         meta={"bar_time": bar_time, "vol": str(regime.vol),
                               "candidate_evaluation_id": evaluation_id})
            self._process_signal(sig, rr, c, tick)

    def _floor_r(self) -> dict:
        e = self.cfg.execution
        return {"small_win_activation": e.small_win_activation_r, "small_win_floor": e.small_win_floor_r,
                "ambition_activation": e.ambition_activation_r, "ambition_floor": e.ambition_floor_r}

    def _ml_filter_check(self, sig, strategy_id: str):
        """Score the signal with the learned filter. Returns (reason, details) to block, or None.
        The score is always recorded on the signal, whatever the mode."""
        cfg = self.cfg.ml_filter
        if not cfg.enabled or cfg.mode == "off" or strategy_id not in cfg.strategies:
            return None
        try:
            if self._ml is None:
                from app.ml.filter import LiveFilter
                self._ml = LiveFilter()
            got = self._ml.score(strategy_id, sig.meta.get("stored_features") or sig.plan.features,
                                 str(sig.plan.direction), sig.regime, sig.session,
                                 sig.meta.get("created_at_utc") or sig.created_at, sig.plan.quality)
        except Exception:
            log.exception("learned filter scoring failed for %s; not blocking", sig.id)
            return None
        if got is None:
            return None
        score, meta = got
        threshold = float(meta.get("threshold") or 0.0)
        age_days = None
        try:
            trained = datetime.fromisoformat(str(meta.get("trained_at")))
            age_days = (datetime.now(timezone.utc) - trained).total_seconds() / 86400.0
        except (TypeError, ValueError):
            pass
        fresh = age_days is not None and age_days <= cfg.max_model_age_days
        enforce = cfg.mode == "enforce" or (cfg.mode == "auto" and bool(meta.get("validated")) and fresh)
        update = {"ml_score": round(score, 4), "ml_threshold": round(threshold, 4),
                  "ml_mode": ("ENFORCED" if enforce else "SHADOW"), "ml_validated": bool(meta.get("validated"))}
        sig.plan.features.update(update)
        try:
            self.ledger.update("signals", "id", sig.id, {"features": {**(sig.meta.get("stored_features") or {}),
                                                                     **update}})
        except Exception:
            log.debug("could not store learned-filter score", exc_info=True)
        if enforce and score < threshold:
            test = meta.get("test") or {}
            return (f"ML_FILTER (score {score:.2f} < {threshold:.2f}; on unseen data the kept trades averaged "
                    f"{test.get('accepted_r')}R vs {test.get('baseline_r')}R for all)",
                    {"score": score, "threshold": threshold, "test": test})
        return None

    def _context_features(self) -> dict:
        """Dollar index / S&P 500 / VIX / yields / oil moves (shared, refreshed once a minute). Never raises."""
        if not self.cfg.context.enabled:
            return {}
        try:
            from app.market.context import shared_feed
            return shared_feed(self.data, self.cfg).features()
        except Exception:
            log.debug("context features unavailable", exc_info=True)
            return {}

    def commission_rt(self) -> float:
        """Round-trip commission per lot for this instrument (Raw / Zero accounts), in account currency."""
        cost = self.cfg.cost
        return float(symbol_lookup(cost.symbol_commission_per_lot_round_trip, self.symbol or "",
                                   cost.commission_per_lot_round_trip))

    def research_gate(self, strategy_id: str = "micro_scalper") -> dict:
        """Live verdict of the research lab for this symbol and strategy (see app/research/lab/gate.py).

        micro_scalper is judged on the lab's own fast-path result (`lab_hint`), which sweeps stop/reward
        combinations on the trigger timeframe. Every other strategy is judged on the generic per-strategy
        research (`strategy_lab_hints`), which replays it at its live parameters with no sweep. Both shapes
        carry the train/test numbers the gate reads, so one gate serves both."""
        from app.research.lab.gate import evaluate
        try:
            if strategy_id != "micro_scalper":
                entry = (self.strategy_lab_hints or {}).get(strategy_id)
                state = evaluate(entry, self.strategy_lab_created_at, self.cfg.research_lab, self.now_utc(),
                                 strategy_id=strategy_id)
                state["timeframe"] = "M15"
                state["strategy_id"] = strategy_id
                return state
            # judge the timeframe micro_scalper is actually trading on (M1 by default)
            tf = "M1"
            try:
                tf = str(self.cfgm.strategy("micro_scalper").param_values().get("trigger_timeframe", "M1")).upper()
            except Exception:
                pass
            entry = self.lab_hint if tf == "M1" else ((self.lab_hint or {}).get("timeframes") or {}).get(tf)
            state = evaluate(entry, self.lab_hint_created_at, self.cfg.research_lab, self.now_utc(),
                             strategy_id=strategy_id)
            state["timeframe"] = tf
            state["strategy_id"] = strategy_id
            return state
        except Exception:
            # An exception here is the ninth way this gate can fail open, and the one the reviewer called out
            # as "database errors". It follows the same policy as a missing result: permitted while the lab is
            # being populated, refused once the operator has said research evidence is required.
            require = bool(getattr(self.cfg.research_lab, "gate_require_evidence", False))
            log.exception("research gate evaluation failed for %s; %s",
                          self.symbol, "BLOCKING (gate_require_evidence)" if require else "not blocking")
            return {"blocked": require, "evidence": "ERROR",
                    "reason": ("RESEARCH_LAB_NO_EVIDENCE (research gate raised; gate_require_evidence is on)"
                               if require else "research gate error - permitted, no evidence either way")}

    # ================================================================== gate pipeline
    def _spread_ok(self, tick: Tick, session: str, stop_distance: Optional[float] = None) -> tuple:
        # When the stop distance is known the spread is also measured AGAINST THE STOP: expectancy without an edge
        # is about -(spread / stop) R per trade, so a tight stop on a wide spread loses money by construction.
        limit_frac = self.cfg.cost.max_spread_fraction_of_stop
        if stop_distance and stop_distance > 0 and limit_frac > 0:
            frac = tick.spread / stop_distance
            if frac > limit_frac:
                return False, f"SPREAD_TOO_LARGE_VS_STOP ({frac:.0%} of the stop, limit {limit_frac:.0%})"
        sp = tick.spread / self.spec.point
        limit, basis = self.spread_limit_points(session)
        if sp > limit:
            return False, f"SPREAD_ABOVE_MAX ({sp:.0f} > {limit:.0f} pts, {basis})"
        pct = self.spread.percentile(sp, session, self.cfg.spread_gate.min_samples_for_percentile)
        if pct is not None and pct > self.cfg.spread_gate.max_spread_percentile:
            return False, f"SPREAD_PERCENTILE ({pct:.2f})"
        if self.health.spread_shock:
            return False, "SPREAD_SHOCK"
        return True, "ok"

    def spread_limit_points(self, session: Optional[str] = None) -> tuple:
        """The spread ceiling for this symbol, in points, and how it was arrived at.

        Preference order: what the symbol's own live quotes say (rolling median x multiple), bounded by the
        configured number x dynamic_max_expansion; otherwise the configured number. The adaptive module and the
        operator's per-symbol value can only tighten it further."""
        cfg = self.cfg.spread_gate
        configured = symbol_lookup(cfg.symbol_max_spread_points, self.symbol, cfg.max_spread_points)
        limit, basis = configured, "configured"
        if cfg.dynamic_ceiling and self.spread is not None:
            samples = len(self.spread.overall)
            median = self.spread.median(session) if session else self.spread.median()
            if samples >= cfg.dynamic_min_samples and median and median == median and median > 0:
                learned = median * cfg.dynamic_multiple
                limit = min(learned, configured * cfg.dynamic_max_expansion)
                basis = f"live median {median:.0f} x{cfg.dynamic_multiple:g}, {samples} samples"
        if self.cfg.adaptive.enabled:
            # Only while fresh. A refresh that could not run leaves the old cap in place and a restart reloads
            # it, so an expired cap would otherwise tighten this gate indefinitely on conditions long gone.
            adaptive_cap = self.adaptive.effective_max_spread_points(
                self.cfg.adaptive.max_spread_cap_age_minutes)
            if adaptive_cap and adaptive_cap < limit:
                age = self.adaptive.cap_age_minutes()
                limit = adaptive_cap
                basis = f"hourly adaptive cap, {age:.0f}m old" if age is not None else "hourly adaptive cap"
        return limit, basis

    def _insert_signal(self, sig: Signal, status: str, shadow: bool) -> None:
        p = sig.plan
        stored = None
        self.ledger.insert("signals", stored := {
            "id": sig.id, "created_at": sig.created_at, "strategy_id": sig.strategy_id,
            "strategy_version": sig.strategy_version, "symbol": sig.symbol, "environment": str(self.env),
            "direction": str(p.direction), "entry": p.entry, "sl": p.sl, "tp": p.tp, "exit_plan": p.exit_model,
            "quality": p.quality, "confidence": sig.confidence, "edge_r": None, "cost_r": None, "regime": sig.regime,
            "session": sig.session, "event_state": sig.event_state,
            "valid_until": self.clock.to_utc(sig.valid_until_broker), "market_snapshot_id": sig.snapshot_id,
            "setup_key": p.setup_key, "status": status, "shadow": shadow,
            "features": {**p.features,
                         **({"microstructure": self.flow.compact()}
                            if self.flow is not None and self.cfg.microstructure.attach_to_signals else {}),
                         **({"research_lab": {"verdict": self.lab_hint.get("verdict"), "best": self.lab_hint.get("best"),
                                              "test": self.lab_hint.get("test")}} if self.lab_hint else {}),
                         **({"selector": {"state": a.state, "multiplier": a.multiplier,
                                          "posterior_r": round(a.posterior_r, 4), "lower_r": round(a.lower_r, 4),
                                          "n_eff": round(a.n_eff, 1), "applied": self.cfg.selector.mode == "auto"}}
                            if (a := self.allocations.get(sig.strategy_id)) is not None else {}),
                         **({"context": ctx} if (ctx := self._context_features()) else {})}})
        sig.meta["stored_features"] = stored["features"]
        sig.meta["created_at_utc"] = sig.created_at
        self.last_signal = {"id": sig.id, "strategy": sig.strategy_id, "direction": str(p.direction),
                            "created_broker_time": sig.created_broker_time, "status": status}
        evaluation_id = sig.meta.get("candidate_evaluation_id")
        if evaluation_id:
            self.ledger.update("candidate_evaluations", "id", evaluation_id, {
                "signal_id": sig.id, "updated_at": utcnow(),
            })
        self._emit("signal_event", {"signal_id": sig.id, "strategy": sig.strategy_id, "direction": str(p.direction),
                                    "entry": p.entry, "sl": p.sl, "tp": p.tp, "quality": p.quality, "status": status})

    def _record_candidate_evaluation(self, strategy_id: str, outcome: str, stage: str, reason: str,
                                     bar_time: Optional[int] = None, gate: Optional[str] = None,
                                     details: Optional[dict] = None) -> str:
        evaluation_id = new_id()
        self.ledger.insert("candidate_evaluations", {
            "id": evaluation_id, "created_at": utcnow(), "updated_at": utcnow(),
            "strategy_id": strategy_id, "symbol": self.symbol, "environment": str(self.env),
            "bar_time": bar_time, "stage": stage, "outcome": outcome, "gate": gate,
            "reason": reason or None, "signal_id": None, "details": details or {},
        })
        return evaluation_id

    def _update_candidate_evaluation(self, sig: Signal, outcome: str, stage: str,
                                    gate: Optional[str], reason: str,
                                    details: Optional[dict] = None) -> None:
        evaluation_id = sig.meta.get("candidate_evaluation_id")
        if evaluation_id:
            self.ledger.update("candidate_evaluations", "id", evaluation_id, {
                "updated_at": utcnow(), "outcome": outcome, "stage": stage, "gate": gate,
                "reason": reason or None, "details": details or {},
            })

    def _reject(self, sig: Signal, gate: Gate, reason: str, details: Optional[dict] = None,
                size: Optional[float] = None, risk_money: Optional[float] = None, status: str = "REJECTED") -> None:
        p = sig.plan
        rid = new_id()
        sp_pts = (self.last_tick.spread / self.spec.point) if self.last_tick else None
        self.ledger.insert("rejections", {
            "id": rid, "created_at": utcnow(), "signal_id": sig.id, "strategy_id": sig.strategy_id,
            "symbol": sig.symbol, "direction": str(p.direction), "gate": str(gate), "reason": reason,
            "proposed_entry": p.entry, "proposed_sl": p.sl, "proposed_tp": p.tp, "proposed_size": size,
            "spread_points": sp_pts, "volatility": sig.meta.get("vol"), "regime": sig.regime, "session": sig.session,
            "news_state": sig.event_state, "details": details or {}})
        self.ledger.update("signals", "id", sig.id, {"status": status})
        sig.meta["rejected_gate"] = str(gate)
        self._update_candidate_evaluation(sig, "SHADOW" if status == "SHADOW" else "REJECTED",
                                          str(gate), str(gate), reason, details)
        if self.cf is not None:
            self.cf.add(sig, str(gate), reason, rid, int(sig.meta.get("bar_time", 0)), sp_pts or 0.0,
                        sig.meta.get("vol", ""), size, risk_money)
        self._emit("rejection_event", {"signal_id": sig.id, "strategy": sig.strategy_id, "gate": str(gate),
                                       "reason": reason, "direction": str(p.direction), "status": status})

    def _process_signal(self, sig: Signal, rr, scfg, tick: Tick) -> None:
        shadow = not rr.executable
        self._insert_signal(sig, "CANDIDATE", shadow)
        if self.env is Environment.REAL:
            self._reject(sig, Gate.EXECUTION, "REAL_EXECUTION_DISABLED")
            return
        if not rr.executable:
            gate = Gate.STRATEGY_STATUS if str(rr.eligibility) == "RESEARCH_ONLY" else (
                Gate.SELECTOR if rr.reason.startswith("selector") else
                Gate.HEALTH if "health" in rr.reason or "stress" in rr.reason else Gate.REGIME)
            self._reject(sig, gate, rr.reason, {"eligibility": str(rr.eligibility)}, status="SHADOW")
            return
        if not self._symbol_enabled():
            self._reject(sig, Gate.SYMBOL_DISABLED, "SYMBOL_TRADING_STOPPED", {"symbol": self.symbol})
            return
        if scfg.id in self.cfg.research_lab.gate_strategies:
            gate_state = self.research_gate(scfg.id)
            if gate_state["blocked"]:
                self._reject(sig, Gate.RESEARCH, gate_state["reason"],
                             {k: gate_state[k] for k in ("pooled_r", "pooled_trades", "test_r", "age_hours")})
                return
        blocked = self._ml_filter_check(sig, scfg.id)
        if blocked:
            self._reject(sig, Gate.ML_FILTER, blocked[0], blocked[1])
            return
        blocker = self._entry_blocker(scfg, sig.session, sig.event_state)
        if blocker is not None:
            self._reject(sig, *blocker)
            return
        if not self._global_positions_known:
            self._reject(sig, Gate.PORTFOLIO, "BROKER_POSITIONS_UNKNOWN")
            return
        active_states = ["APPROVED", "ORDER_SENT", "OPEN", "PROTECTION", "PARTIAL", "RUNNER",
                         "USER_MODIFIED", "RECONCILIATION_REQUIRED"]
        active_trades = self.repo.select("trades", {"state": active_states})
        exposure = self._symbol_exposure(active_trades)
        if exposure:
            add_mult = self._scale_in_multiplier(sig, active_trades, exposure)
            if add_mult is None:
                self._reject(sig, Gate.PORTFOLIO, "SYMBOL_ALREADY_EXPOSED",
                             {"symbol": self.symbol, **exposure})
                return
            rr.risk_multiplier = rr.risk_multiplier * add_mult
            sig.meta["scale_in"] = {"multiplier": add_mult, "existing": exposure.get("active_trades")}
        broker_tickets = {int(pos.ticket) for pos in self.positions_cache}
        tracked_tickets = {int(t["position_id"]) for t in active_trades if t.get("position_id")}
        unfilled_active = sum(1 for t in active_trades if not t.get("position_id"))
        n_open = len(broker_tickets | tracked_tickets) + unfilled_active + self._portfolio_pending_count()
        if n_open >= self.cfg.risk.max_concurrent_positions:
            self._reject(sig, Gate.PORTFOLIO, "MAX_CONCURRENT_POSITIONS",
                         {"open_and_reserved": n_open, "limit": self.cfg.risk.max_concurrent_positions})
            return
        # cost / edge
        est = estimate_from_stats(scfg.stats, self.cfg.risk.kelly_shrinkage_trades)
        cost = self.cost.estimate(sig.plan, self.spec, tick.spread)
        g = edge_gate(est, cost, self.cfg.cost, self.env)
        sig.edge_r, sig.cost_r = g.net_edge_r, cost.cost_r
        self.ledger.update("signals", "id", sig.id, {"edge_r": g.net_edge_r if math.isfinite(g.net_edge_r) else None,
                                                     "cost_r": cost.cost_r if math.isfinite(cost.cost_r) else None})
        if not g.passed:
            self._reject(sig, g.gate, g.reason, g.detail)
            return
        # sizing (risk can veto alpha, broker constraints can veto risk)
        sizing = self._size(sig, sig.plan.entry, sig.plan.sl, est, rr.risk_multiplier)
        if not sizing.feasible:
            gate = Gate.MARGIN if sizing.reason == "MARGIN_INFEASIBLE" else Gate.SIZE
            self._reject(sig, gate, sizing.reason, sizing.to_dict(), size=None,
                         risk_money=sizing.min_volume_risk or None)
            return
        if self.env is Environment.SHADOW:
            self._reject(sig, Gate.ENVIRONMENT, "SHADOW_MODE", sizing.to_dict(), size=sizing.volume,
                         risk_money=sizing.risk_money, status="SHADOW")
            return
        ok, why = self._spread_ok(tick, sig.session, sig.plan.stop_distance)
        if not ok:
            if self.cfg.spread_gate.wait_for_spread and self.server_now() < sig.valid_until_broker:
                self.pending.append({"signal": sig, "rr": rr, "scfg": scfg, "est": est, "reason": why})
                self.ledger.update("signals", "id", sig.id, {"status": "WAITING"})
                self._update_candidate_evaluation(sig, "WAITING", "SPREAD", str(Gate.SPREAD), why)
                self._emit("signal_event", {"signal_id": sig.id, "status": "WAITING", "reason": why})
                return
            self._reject(sig, Gate.SPREAD, why, size=sizing.volume, risk_money=sizing.risk_money)
            return
        self._execute(sig, rr, est, tick)

    def _entry_blocker(self, scfg, session: str, event_state: str) -> Optional[tuple]:
        """(gate, reason, details) for the first entry check that fails NOW, or None.

        Shared by a new signal and by one waiting for its spread to narrow. That wait can last the signal's whole
        TTL (up to 15 minutes), and re-checking only the kill switch let a waiting signal trade after the daily
        loss limit, a loss cooldown, a stale feed or a news window had stopped new entries (audit, 23 Oct 2026)."""
        if self.kill.blocking:
            return Gate.KILL_SWITCH, self.kill.reason or ",".join(self.kill.volatile_reasons), None
        if self.risk is None or self.risk.state in (RiskState.NO_NEW_TRADES, RiskState.EMERGENCY):
            return Gate.RISK, f"RISK_STATE_{self.risk.state if self.risk else 'UNKNOWN'}", None
        loss_streak, loss_blocked = self._loss_guard()
        if loss_blocked:
            return Gate.RISK, "SYMBOL_LOSS_COOLDOWN", {
                "symbol": self.symbol, "consecutive_losses": loss_streak,
                "cooldown_minutes": self.cfg.risk.loss_circuit_breaker_cooldown_minutes}
        rep = self.health.report(self.now_utc())
        blockers = [b for b in rep.blockers if b != "SPREAD_SHOCK"]
        if blockers:
            return Gate.HEALTH, ",".join(blockers), rep.details
        if scfg.allowed_sessions and session not in scfg.allowed_sessions:
            return Gate.SESSION, f"SESSION_{session}_NOT_ALLOWED", None
        ev = EventState(event_state)
        if self.cfg.events.mode == "block":
            if ev in (EventState.PRE_EVENT, EventState.EVENT):
                return Gate.EVENT, f"EVENT_STATE_{ev}", None
            if ev is EventState.UNKNOWN and self.env is Environment.REAL:
                return Gate.EVENT, "EVENT_CALENDAR_MISSING_OR_STALE", None
        return None

    def _process_pending(self, tick: Tick) -> None:
        now_b = self.server_now()
        utc = self.clock.to_utc(tick.time)
        keep = []
        for item in self.pending:
            sig = item["signal"]
            if now_b > sig.valid_until_broker:
                self._reject(sig, Gate.SPREAD, f"EXPIRED_WAITING_FOR_SPREAD ({item['reason']})", status="EXPIRED")
                continue
            if self.kill.blocking:
                self._reject(sig, Gate.KILL_SWITCH, "KILL_SWITCH_WHILE_WAITING")
                continue
            if not self._symbol_enabled():
                self._reject(sig, Gate.SYMBOL_DISABLED, "SYMBOL_TRADING_STOPPED_WHILE_WAITING")
                continue
            # the session and the news window as they are now, not as they were when the signal was made
            blocker = self._entry_blocker(item["scfg"], str(self.sessions.classify(utc)), str(self.calendar.state(utc)))
            if blocker is not None:
                gate, reason, details = blocker
                self._reject(sig, gate, reason, {**(details or {}), "while_waiting_for_spread": True})
                continue
            ok, _ = self._spread_ok(tick, sig.session, sig.plan.stop_distance)
            if ok:
                self._execute(sig, item["rr"], item["est"], tick)
            else:
                keep.append(item)
        self.pending = keep

    def _execute(self, sig: Signal, rr, est, tick: Tick) -> None:
        if self.execution_dispatcher is None:
            return
        if self.env is Environment.REAL:
            self._reject(sig, Gate.EXECUTION, "REAL_EXECUTION_DISABLED")
            return
        if not self._symbol_enabled():
            self._reject(sig, Gate.SYMBOL_DISABLED, "SYMBOL_TRADING_STOPPED", {"symbol": self.symbol})
            return
        out = self.execution_dispatcher.dispatch(
            "MT5", sig, tick, self.server_now(),
            resize=lambda price, sl: self._size(sig, price, sl, est, rr.risk_multiplier),
            spread_ok=lambda t: self._spread_ok(t, sig.session, sig.plan.stop_distance)[0],
            session=sig.session, regime=sig.regime)
        status = {"FILLED": "EXECUTED", "RECONCILIATION_REQUIRED": "EXECUTED"}.get(out.status, "REJECTED")
        self.ledger.update("signals", "id", sig.id, {"status": status})
        self._update_candidate_evaluation(sig, out.status, "EXECUTION", "EXECUTION",
                                          out.reason or out.status,
                                          {"execution_status": out.status})
        if out.status in ("CANCELLED", "REJECTED"):
            self._release_portfolio_risk(sig.id)
            gate = Gate.STALE if out.reason in ("STALE_SIGNAL", "ENTRY_DRIFT") else Gate.EXECUTION
            self._reject(sig, gate, out.reason, {"trade_id": out.trade["id"] if out.trade else None})
        if out.trade is not None and out.status == "FILLED":
            self.book.ensure_mstate(out.trade)
        self._refresh_account()

    # ================================================================== risk / account
    def _open_risk_money(self) -> float:
        total = 0.0
        open_states = ["OPEN", "PROTECTION", "PARTIAL", "RUNNER", "USER_MODIFIED",
                       "RECONCILIATION_REQUIRED", "APPROVED", "ORDER_SENT"]
        trades = self.repo.select("trades", {"state": open_states})
        tracked_positions = {int(t["position_id"]) for t in trades if t.get("position_id")}
        for t in trades:
            try:
                entry_value = t.get("entry_price")
                if entry_value is None or float(entry_value) <= 0:
                    entry_value = t.get("entry_requested")
                sl_value = t.get("sl")
                if sl_value is None or float(sl_value) <= 0:
                    sl_value = t.get("initial_sl")
                volume_value = t.get("open_volume")
                if volume_value is None or float(volume_value) <= 0:
                    volume_value = t.get("filled_volume")
                if volume_value is None or float(volume_value) <= 0:
                    volume_value = t.get("requested_volume")
                if volume_value is None or float(volume_value) <= 0:
                    if t.get("state") == "APPROVED":
                        continue
                    log.error("open risk unavailable for trade %s; blocking new entries", t.get("id"))
                    return float("inf")
                if entry_value is None or sl_value is None:
                    log.error("open risk unavailable for trade %s; blocking new entries", t.get("id"))
                    return float("inf")
                entry, sl, vol = float(entry_value), float(sl_value), float(volume_value)
                side = 1 if t.get("direction") == "BUY" else -1
                if vol <= 0:
                    log.error("open risk invalid for trade %s; blocking new entries", t.get("id"))
                    return float("inf")
                dist = (entry - sl) * side
                if dist <= 0:
                    # stop at or beyond entry (breakeven / locked profit): no capital is at risk. This used to be
                    # treated as "invalid" and blocked EVERY new entry on every symbol until the winner closed.
                    continue
                spec = self.spec if t.get("symbol") == self.symbol else self.data.symbol_spec(t["symbol"])
                total += spec.loss_per_lot(dist) * vol
            except (TypeError, ValueError, BrokerError):
                log.exception("cannot calculate open risk for trade %s; blocking new entries", t.get("id"))
                return float("inf")
        for pos in self.positions_cache:
            if pos.ticket in tracked_positions:
                continue
            if pos.sl <= 0:
                return float("inf")
            spec = self.spec if pos.symbol == self.symbol else self.data.symbol_spec(pos.symbol)
            total += spec.loss_per_lot(abs(pos.price_open - pos.sl)) * pos.volume
        return total

    def _risk_account_key(self) -> str:
        account = self.account or self.exec_broker.account_info()
        if account is None or account.login is None or not account.server:
            raise RuntimeError("verified account identity unavailable for portfolio risk reservation")
        return f"{account.login}:{account.server}:{self.env.value}"

    def _external_risk_by_symbol(self) -> Dict[str, float]:
        if not self._global_positions_known:
            raise RuntimeError("broker positions unavailable for portfolio risk reservation")
        open_states = ["OPEN", "PROTECTION", "PARTIAL", "RUNNER", "USER_MODIFIED",
                       "RECONCILIATION_REQUIRED", "APPROVED", "ORDER_SENT"]
        trades = self.repo.select("trades", {"state": open_states})
        tracked_positions = {int(t["position_id"]) for t in trades if t.get("position_id")}
        external: Dict[str, float] = {}
        for position in self.positions_cache:
            if position.ticket in tracked_positions:
                continue
            if position.sl <= 0:
                raise RuntimeError(f"untracked position {position.ticket} has no protective stop")
            spec = self.spec if position.symbol == self.symbol else self.data.symbol_spec(position.symbol)
            if spec is None:
                raise RuntimeError(f"symbol specification unavailable for untracked position {position.ticket}")
            risk = spec.loss_per_lot(abs(position.price_open - position.sl)) * position.volume
            if not math.isfinite(risk) or risk <= 0:
                raise RuntimeError(f"risk unavailable for untracked position {position.ticket}")
            external[position.symbol] = external.get(position.symbol, 0.0) + risk
        return external

    def _open_directions_by_symbol(self) -> Dict[str, int]:
        """Net direction per symbol across every open position: +1 long, -1 short.

        Needed because correlation alone cannot say whether two positions double up or partly offset.
        Where a symbol holds both directions the larger side wins, and a dead heat is treated as long
        (+1), which is the side that reinforces rather than offsets - the cautious reading."""
        net: Dict[str, float] = {}
        for position in (self.positions_cache or []):
            try:
                # Position carries `side`; it has no MT5-style `type`, which read as 0 (long) for every position
                sign = float(position.side.sign)
                net[position.symbol] = net.get(position.symbol, 0.0) + sign * float(position.volume)
            except Exception:                                        # noqa: BLE001
                continue
        return {sym: (1 if v >= 0 else -1) for sym, v in net.items()}

    def _correlations_for_portfolio(self, symbol: str) -> Dict[str, Optional[float]]:
        cores = getattr(self, "portfolio_cores", None) or [self]
        correlations: Dict[str, Optional[float]] = {}
        for core in cores:
            other_symbol = core.symbol
            if not other_symbol or other_symbol == symbol:
                continue
            correlations[other_symbol] = rolling_h1_correlation(
                self.ff.get("H1"), core.ff.get("H1"),
                self.cfg.risk.correlation_lookback_days, self.cfg.risk.correlation_min_samples)
        return correlations

    def _reserve_portfolio_risk(self, trade: dict, risk_money: float) -> tuple[bool, str]:
        self._refresh_account()
        if not self._account_refresh_ok or self.account is None:
            return False, "ACCOUNT_OR_POSITIONS_UNAVAILABLE"
        try:
            equity = float(self.account.equity)
            if not math.isfinite(equity) or equity <= 0:
                return False, "ACCOUNT_EQUITY_UNAVAILABLE"
            r = self.cfg.risk
            total_cap = equity * r.max_total_open_risk_fraction
            group_cap = total_cap * r.correlation_group_fraction
            reason = self.repo.reserve_portfolio_risk(
                account_key=self._risk_account_key(),
                signal_id=str(trade["signal_id"]),
                trade_id=str(trade["id"]),
                symbol=str(trade["symbol"]),
                risk_money=float(risk_money),
                max_total_risk=total_cap,
                max_correlated_risk=group_cap,
                correlation_threshold=self.cfg.risk.correlation_threshold,
                correlations=self._correlations_for_portfolio(str(trade["symbol"])),
                external_risk_by_symbol=self._external_risk_by_symbol(),
                expires_at=utcnow() + timedelta(seconds=60),
                # In "gross" mode these are ignored and the arithmetic sum is used, exactly as before.
                # In "diversified" mode the total is computed as portfolio variance, so two genuinely
                # independent strategies no longer each consume the whole budget.
                portfolio_cfg={
                    "mode": r.portfolio_risk_mode,
                    "gross_budget": equity * r.max_gross_open_risk_fraction,
                    "stressed_risk_tolerance": r.stressed_risk_tolerance,
                    "stress_correlation": r.stress_correlation,
                    "diversification_floor": r.diversification_floor,
                    "missing_correlation_value": r.missing_correlation_value,
                },
                direction=_side_sign(trade.get("direction")),
                directions_by_symbol=self._open_directions_by_symbol(),
            )
        except Exception as exc:
            log.exception("portfolio risk reservation failed for trade %s", trade.get("id"))
            return False, f"PORTFOLIO_RISK_RESERVATION_ERROR:{exc}"
        return (False, reason) if reason else (True, "")

    def _release_portfolio_risk(self, signal_id: str) -> None:
        try:
            self.repo.release_portfolio_risk(self._risk_account_key(), signal_id)
        except Exception:
            log.exception("failed to release portfolio risk reservation for signal %s", signal_id)

    def _recover_risk_reservations(self) -> None:
        try:
            changed = self.repo.recover_portfolio_risk_reservations(
                self._risk_account_key(), utcnow(), timedelta(seconds=60))
            if changed:
                log.info("recovered %d portfolio risk reservations", changed)
        except Exception:
            log.exception("portfolio risk reservation recovery failed")

    def _loss_guard(self) -> tuple[int, bool]:
        cfg = self.cfg.risk
        terminal = ["CLOSED", "BROKER_CLOSED"]
        rows = self.repo.select("trades", {"symbol": self.symbol, "state": terminal},
                                order_by="closed_at", desc=True,
                                limit=max(cfg.loss_reduction_after, cfg.loss_circuit_breaker_losses))
        streak = 0
        losses = []
        for row in rows:
            pnl = row.get("net_pnl")
            if pnl is None or float(pnl) >= 0:
                break
            streak += 1
            losses.append(as_epoch(row.get("closed_at")))
        blocked = False
        if streak >= cfg.loss_circuit_breaker_losses and cfg.loss_circuit_breaker_cooldown_minutes > 0:
            breaker_losses = losses[:cfg.loss_circuit_breaker_losses]
            now = self.now_utc().timestamp()
            window = cfg.loss_circuit_breaker_window_minutes * 60
            cooldown = cfg.loss_circuit_breaker_cooldown_minutes * 60
            in_window = breaker_losses[-1] > 0 and now - breaker_losses[-1] <= window
            clustered = in_window and max(breaker_losses) - min(breaker_losses) <= window
            blocked = clustered and now - max(breaker_losses) < cooldown
        self._last_loss_streak = streak
        self._loss_cooldown_active = blocked
        return streak, blocked

    def _portfolio_pending_count(self) -> int:
        cores = getattr(self, "portfolio_cores", None)
        return sum(len(core.pending) for core in cores) if cores else len(self.pending)

    def _symbol_pending_count(self) -> int:
        cores = getattr(self, "portfolio_cores", None)
        if cores is None:
            cores = [self]
        return sum(1 for core in cores for item in core.pending
                   if item["signal"].symbol == self.symbol)

    def _scale_in_multiplier(self, sig, active_trades: list, exposure: dict) -> Optional[float]:
        """Risk multiplier for adding to a winner on this symbol, or None when adding is not allowed."""
        sc = self.cfg.scaling
        if not sc.enabled or exposure.get("pending_signals"):
            return None
        # A strategy can refuse to add to an open position (trend_daily_v1: its research holds one position per
        # symbol, so adds would trade something the lab never tested).
        try:
            spec = self.cfgm.strategy(sig.strategy_id).params.get("allow_scale_in")
        except Exception:
            spec = None
        if spec is not None and not bool(spec.value):
            return None
        mine = [t for t in active_trades if t.get("symbol") == self.symbol]
        if not mine or len(mine) >= sc.max_positions_per_symbol or exposure.get("broker_positions", 0) > len(mine):
            return None
        tick = self.last_tick
        if tick is None:
            return None
        ok_states = {"OPEN", "PROTECTION", "PARTIAL", "RUNNER"}
        for t in mine:
            if (t.get("state") not in ok_states or not t.get("position_id") or t.get("direction") != str(sig.plan.direction)
                    or t.get("entry_price") is None or t.get("sl") is None):
                return None
            buy = t["direction"] == "BUY"
            if (t["sl"] < t["entry_price"]) if buy else (t["sl"] > t["entry_price"]):
                return None                      # not yet protected at breakeven: the add would be unpaid risk
            st = self.book.ensure_mstate(t)
            if st is None or st.r(tick.bid if buy else tick.ask) < sc.min_open_r:
                return None
        return sc.risk_multiplier

    def _symbol_exposure(self, active_trades: list) -> dict:
        trade_count = sum(t.get("symbol") == self.symbol for t in active_trades)
        position_count = sum(pos.symbol == self.symbol for pos in self.positions_cache)
        pending_count = self._symbol_pending_count()
        if trade_count or position_count or pending_count:
            return {"active_trades": trade_count, "broker_positions": position_count,
                    "pending_signals": pending_count}
        return {}

    def _size(self, sig: Signal, price: float, sl: float, est, route_mult: float) -> SizingResult:
        acct = self.account or self.exec_broker.account_info()
        if est.validated:
            k = est.kelly_fraction() * self.cfg.risk.kelly_fraction
            edge_frac = min(k, self.cfg.risk.max_risk_fraction_per_trade)
        else:
            edge_frac = self.cfg.risk.research_risk_fraction
        vol_mult = 1.0
        if self.regime_state.vol in ("HIGH_VOLATILITY", "VOLATILITY_EXPANSION"):
            vol_mult = self.cfg.risk.high_vol_multiplier
        if self.regime_state.stress:
            vol_mult = 0.0
        mult = (self.risk.multiplier if self.risk else 0.0) * vol_mult * route_mult
        streak, _ = self._loss_guard()
        if streak >= self.cfg.risk.loss_reduction_after:
            mult *= self.cfg.risk.loss_reduction_multiplier
        inp = SizingInputs(equity=acct.equity, free_margin=acct.margin_free, used_margin=acct.margin,
                           side=sig.plan.direction, entry=price, sl=sl, edge_risk_fraction=edge_frac,
                           risk_multiplier=mult, open_risk_money=self._open_risk_money(),
                           daily_loss_remaining=self.risk.daily_loss_remaining if self.risk else 0.0,
                           commission_per_lot=self.commission_rt())
        broker = self.exec_broker
        return size_position(self.spec, inp, self.cfg.risk,
                             calc_profit=lambda side, v, o, c: broker.calc_profit(self.symbol, side, v, o, c),
                             calc_margin=lambda side, v, p: broker.calc_margin(self.symbol, side, v, p))

    def _refresh_account(self) -> None:
        try:
            self.account = self.exec_broker.account_info()
            pos = self.exec_broker.positions()
            if pos is not None:
                self.positions_cache = pos
                self._global_positions_known = True
            else:
                self._global_positions_known = False
            self._account_refresh_ok = self.account is not None and pos is not None
        except Exception as e:
            self._global_positions_known = False
            self._account_refresh_ok = False
            log.warning("account refresh failed: %s", e)

    def _update_risk(self, force_persist: bool = False) -> None:
        if self.account is None:
            return
        rep = self.health.report(self.now_utc())
        prev = self.risk.state if self.risk else None
        self.risk = self.dd.update(self.account.equity, self.account.balance, self.now_utc(), rep.forced_risk_state,
                                   ",".join(rep.blockers))
        if self.risk.state is RiskState.EMERGENCY and not self.kill.engaged:
            self.kill.engage(f"EMERGENCY: {self.risk.reason}", "risk")
            self._alert("CRITICAL", "The account has fallen far enough to trigger the emergency stop. No new trades will open. Reason: " + str(self.risk.reason))
        changed = prev != self.risk.state
        if changed or force_persist or self._every("risk_persist", 900):
            self.ledger.insert("risk_states", {"created_at": utcnow(), "state": str(self.risk.state),
                                               "risk_multiplier": self.risk.multiplier, "equity": self.risk.equity,
                                               "balance": self.risk.balance, "peak_equity": self.risk.peak_equity,
                                               "drawdown": self.risk.drawdown, "day": self.risk.day,
                                               "day_start_equity": self.risk.day_start_equity,
                                               "daily_pnl": self.risk.daily_pnl,
                                               "daily_loss_remaining": self.risk.daily_loss_remaining,
                                               "reason": self.risk.reason, "details": {"health": rep.components}})
            try:
                self.repo.set_flag("risk_tracker", self.dd.state_for_persistence(), "risk")
            except Exception as e:
                self._on_db_failure(f"risk_tracker: {e}")
            if changed and prev is not None:
                self._alert("WARNING" if self.risk.state in (RiskState.REDUCED, RiskState.RESTRICTED) else "CRITICAL",
                            f"Risk state {prev} -> {self.risk.state}: {self.risk.reason}")
            if changed:
                self._emit("risk_state", self.risk.to_dict())
        # storage recovery check (does NOT auto-release the kill switch; a human resumes)
        if not self.health.db_ok and self._every("db_check", 30):
            if self.repo.healthy():
                self.health.db_ok = True
                self._alert("WARNING", "The database is working again. Trading stays stopped until you press “Resume trading” on the dashboard.")
        if self.account is not None and self._every("risk_reservation_recovery", 30):
            self._recover_risk_reservations()

    def _refresh_strategy_stats(self) -> None:
        for c in self.cfgm.strategies.strategies:
            health_key = strategy_health_key(c.id, c.version)
            try:
                # the NEWEST 1,000, oldest first: health reads the end of the list as "recent", so the ascending
                # select this replaced judged a strategy past 1,000 trades on its first 1,000 for good
                rows = self.repo.select("trades", {"strategy_id": c.id, "strategy_version": c.version,
                                                    "state": ["CLOSED", "BROKER_CLOSED"]},
                                        order_by="closed_at", desc=True, limit=1000)[::-1]
            except Exception as e:
                log.exception("strategy health history query failed for %s", health_key)
                h = StrategyHealth.RESTRICTED
                self.shealth.state[health_key] = h
                self.shealth.details[health_key] = {
                    "note": "closed-trade health history unavailable",
                    "error": type(e).__name__,
                }
                self._strategy_stats[c.id] = {
                    "version": c.version, "trades": 0, "expectancy_r": None,
                    "health": str(h), "health_note": "closed-trade health history unavailable",
                    "status": c.status, "enabled": c.enabled,
                    "evidence": (c.stats or {}).get("evidence_level", "INSUFFICIENT"),
                }
                if self._every(f"restricted_{health_key}", 86400):
                    self._alert("CRITICAL", f"Strategy {health_key} RESTRICTED: health history unavailable")
                continue
            rs = [float(r["r_multiple"]) for r in rows if r.get("r_multiple") is not None]
            h = self.shealth.evaluate(health_key, rs, oos_expectancy_r=(c.stats or {}).get("oos_expectancy_r"))
            self._strategy_stats[c.id] = {"version": c.version, "trades": len(rs),
                                          "expectancy_r": (sum(rs) / len(rs)) if rs else None,
                                          "health": str(h), "status": c.status, "enabled": c.enabled,
                                          "evidence": (c.stats or {}).get("evidence_level", "INSUFFICIENT")}
            if h.value == "RESTRICTED" and self._every(f"restricted_{health_key}", 86400):
                self._alert("CRITICAL", f"Strategy {health_key} RESTRICTED: live expectancy statistically negative")

    def _minute_tasks(self) -> None:
        if self._every("lab_hint", 600.0):
            try:      # advisory: latest research-lab verdict for this symbol, stored with signals for later analysis
                latest = self.repo.get_flag("research_lab_latest") or {}
                self.lab_hint = symbol_lookup(latest.get("symbols") or {}, self.symbol, None) or {}
                self.lab_hint_created_at = latest.get("created_at")
                # {strategy_id: {symbol: entry}} -> {strategy_id: entry} for this symbol
                self.strategy_lab_hints = {
                    sid: (symbol_lookup(per_sym or {}, self.symbol, None) or {})
                    for sid, per_sym in ((latest.get("strategies") or {}).items())}
                # per-strategy research is carried forward between cycles, so it has its own age
                self.strategy_lab_created_at = latest.get("strategies_created_at") or latest.get("created_at")
            except Exception:
                self.lab_hint, self.lab_hint_created_at = {}, None
                self.strategy_lab_hints, self.strategy_lab_created_at = {}, None
            # Alert per gated strategy on its own verdict, not one verdict relabelled for each of them.
            for sid in self.cfg.research_lab.gate_strategies:
                state = self.research_gate(sid)
                prev = getattr(self, "_research_blocked_last", {})
                if not isinstance(prev, dict):          # pre-upgrade state was a bare bool
                    prev = {}
                if state["blocked"] != prev.get(sid, False):
                    prev[sid] = state["blocked"]
                    self._research_blocked_last = prev
                    self._alert("WARNING" if state["blocked"] else "INFO",
                                f"{sid} on {self.symbol} {'BLOCKED' if state['blocked'] else 'unblocked'} by research "
                                f"lab: {state['reason']}")
        if self.cfg.selector.mode != "off" and self._every("selector", self.cfg.selector.interval_minutes * 60.0):
            self._run_selector()
        if self.cfg.calendar_sync.enabled and self._every("calendar_reload", 300.0):
            self._reload_calendar()
        # Every symbol core sees the same account, so only the first one records it; one row a minute per core put
        # 13 identical rows a minute in account_snapshots (23 Oct 2026).
        cores = getattr(self, "portfolio_cores", None)
        if self.account is not None and (not cores or cores[0] is self):
            a = self.account
            self.telemetry.enqueue("account_snapshots", {"created_at": utcnow(), "environment": str(self.env),
                                                         "balance": a.balance, "equity": a.equity, "margin": a.margin,
                                                         "free_margin": a.margin_free,
                                                         "margin_level": a.margin_level, "profit": a.profit})
        rep = self.health.report(self.now_utc())
        for comp, st in rep.components.items():
            self.telemetry.enqueue("system_health", {"created_at": utcnow(), "component": comp, "status": st,
                                                     "startup_state": str(self.state), "message": "",
                                                     "details": rep.details if comp == "DATA" else {}})
        if self._every("strategy_stats", 600):
            self._refresh_strategy_stats()
        try:
            day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            rows = self.repo.select("trades", {"state": ["CLOSED", "BROKER_CLOSED"]}, order_by="closed_at",
                                    desc=True, limit=200)
            self._today_realized = sum(float(r.get("net_pnl") or 0) for r in rows
                                       if as_epoch(r.get("closed_at")) >= day_start.timestamp())
        except Exception:
            pass
        self._maybe_daily_summary()

    def _maybe_daily_summary(self) -> None:
        now = datetime.now(timezone.utc)
        day = now.date().isoformat()
        if now.hour < self.cfg.alerts.daily_summary_hour_utc or self._last_summary_day == day:
            return
        self._last_summary_day = day
        try:
            from app.reports import daily_report
            rep = daily_report(self.repo, now)
        except Exception as e:
            log.warning("daily report failed: %s", e)
            return
        rep["system_uptime_sec"] = self.bridge.get_state().get("uptime_sec")
        rep["strategy_health"] = {k: v.get("health") for k, v in self._strategy_stats.items()}
        self.ledger.upsert("daily_reports", {"day": now.date(), "created_at": utcnow(), "report": rep, "sent": True},
                           ["day"])
        lines = [f"Daily Trading Report {day}"] + [f"{k}: {v}" for k, v in rep.items() if not isinstance(v, dict)]
        self.notifier.send("\n".join(lines))

    # ================================================================== news straddle
    def _build_straddle(self) -> StraddleManager:
        return StraddleManager(
            cfg_fn=lambda: self.cfg, env=self.env, broker=self.exec_broker, book=self.book, repo=self.repo,
            magic=self.cfg.broker.magic_number, symbol=self.symbol, spec_fn=lambda: self.exec_broker.symbol_spec(self.symbol),
            calendar=self.calendar, now_utc=self.now_utc, server_now=self.server_now,
            tick_fn=lambda: self.last_tick, atr_fn=self._m15_atr, block_fn=self._straddle_block_reason,
            spread_ok_fn=lambda t: self._spread_ok(t, str(self.sessions.classify(self.clock.to_utc(t.time)))),
            spread_shock_fn=lambda: self.health.spread_shock, size_fn=self._straddle_size,
            cost_fn=self._straddle_cost, reserve_risk=self._reserve_portfolio_risk,
            release_risk=self._release_portfolio_risk,
            insert_signal=lambda sig, st: self._insert_signal(sig, st, False),
            update_signal=lambda sid, st: self.ledger.update("signals", "id", sid, {"status": st}),
            context_fn=lambda: {"session": str(self.sessions.classify(self.now_utc())),
                                "regime": self.regime_state.label, "event_state": str(self.calendar.state(self.now_utc()))},
            alert=self._alert, audit=lambda status, msg, det: self._health_row("NEWS_STRADDLE", status, msg, det),
            emit=self._emit, on_uncertain=self._mark_uncertain, on_slippage=self.cost.slippage.add,
            protection_deviation=lambda: self.cfg.execution.deviation_points,
            close_unprotected=lambda: self.cfg.execution.close_unprotected_position, wall=self.wall,
            vol_scale_fn=lambda: self.adaptive.vol_scale if self.cfg.adaptive.enabled else 1.0)

    def _poll_book(self) -> None:
        now = self.wall()
        if now < self._book_next:
            return
        try:
            self.book_tracker.update(self.exec_broker.market_book(self.symbol) if self.exec_broker else None)
        except Exception:
            self.book_tracker.update(None)
        # no depth on this symbol: look again rarely instead of every second
        self._book_next = now + (600.0 if self.book_tracker.unavailable_polls >= 3 else 1.0)

    def micro_snapshot(self) -> dict:
        snap = self.flow.snapshot() if self.flow is not None else {"status": "DISABLED"}
        snap["book"] = self.book_tracker.last
        return snap

    def market_state(self) -> dict:
        """Is THIS symbol's market open, judged by the broker's own quotes rather than a calendar guess.

        SessionClassifier models ONE schedule: the FX/metals weekend (Fri 21:00 -> Sun 22:00 UTC). It knows
        nothing about equity-CFD sessions, daily breaks or holidays, so on a weekday morning it called every
        symbol OPEN - including US stock CFDs hours before their session starts.

        The broker's last quote time is the ground truth available for free: a market that is shut stops
        quoting. So a fresh quote means OPEN whatever the calendar says, a long silence means CLOSED, and the
        weekend estimate is used only when there is no quote or no broker clock to measure against.
        """
        always_open = bool(getattr(self.sessions, "always_open", False))
        estimate = SessionClassifier.market_status(datetime.now(timezone.utc), always_open=always_open)
        fallback = {**estimate, "basis": "weekend estimate (no live quote)", "quote_age_sec": None,
                    "schedule_type": "always_open_24x7" if always_open else "estimated_weekend_hours"}
        tick = self.last_tick
        if tick is None:
            return fallback
        server = self.server_now()
        if not server:
            return fallback
        age = max(0.0, float(server) - float(tick.time))
        limit = float(getattr(self.cfg.health, "market_closed_quote_age_sec", 180.0))
        if age > limit:
            return {"status": "MARKET_CLOSED", "opens_at_utc": estimate.get("opens_at_utc"),
                    "seconds_to_open": estimate.get("seconds_to_open"),
                    "basis": f"no quote for {age:.0f}s", "quote_age_sec": round(age, 1),
                    "schedule_type": "broker_quotes"}
        return {"status": "OPEN", "opens_at_utc": None, "seconds_to_open": None,
                "basis": f"quoting, last {age:.0f}s ago", "quote_age_sec": round(age, 1),
                "schedule_type": "broker_quotes"}

    def _run_adaptive(self) -> None:
        """Hourly per-symbol parameter refresh from already-hydrated bars. Runs on the trading thread (the MT5
        library is not thread safe), best effort: a failure keeps the previous parameters."""
        try:
            cfg = self.cfg.adaptive
            ff = self.ff.get("M15")
            bars = self.stores["M15"].to_list()[-cfg.lookback_bars:]
            res = adaptive_compute(self.adaptive.vol_scale, list(ff.atr(14)) if ff is not None else [],
                                   [b.spread for b in bars], self.cfg.spread_gate.max_spread_points, cfg)
            before = self.adaptive.to_dict()
            self.adaptive.apply(res)
            self.repo.set_flag(f"adaptive:{self.symbol}", self.adaptive.to_dict(), "adaptive")
            self.telemetry.enqueue("adaptive_param_history", {
                "created_at": utcnow(), "symbol": self.symbol, "updated": res.updated, "reason": res.reason,
                "vol_scale": self.adaptive.vol_scale, "max_spread_points": self.adaptive.max_spread_points,
                "previous_vol_scale": before["vol_scale"], "previous_max_spread_points": before["max_spread_points"],
                "metrics": res.metrics})
            if res.updated:
                self._emit("adaptive_update", {"symbol": self.symbol, "vol_scale": self.adaptive.vol_scale,
                                               "max_spread_points": self.adaptive.max_spread_points})
        except Exception:
            log.exception("adaptive parameter refresh failed for %s; keeping previous values", self.symbol)

    def _restore_selector(self) -> None:
        from app.selection.selector import Allocation
        try:
            saved = self.repo.get_flag(f"selector:{self.symbol}") or {}
            self.allocations = {sid: Allocation.from_dict(d) for sid, d in (saved.get("allocations") or {}).items()}
        except Exception as e:
            log.info("selector state not restored: %s", e)
            self.allocations = {}

    def _run_selector(self) -> None:
        """Re-score every strategy on this symbol from its account trades, shadow outcomes and research. Best
        effort: a failure keeps the previous allocations."""
        from app.selection.selector import allocate_strategy
        cfg = self.cfg.selector
        try:
            now = self.wall()
            new = {}
            for c in self.cfgm.strategies.strategies:
                key = (id(self.repo), c.id, c.version)
                hit = _SELECTOR_ROWS.get(key)
                if hit is not None and hit[3] is self.repo and 0.0 <= now - hit[0] < SELECTOR_ROWS_TTL_SEC:
                    trades, cfs = hit[1], hit[2]
                else:
                    trades = self.repo.select("trades", {"strategy_id": c.id, "strategy_version": c.version,
                                                         "state": ["CLOSED", "BROKER_CLOSED"]},
                                              order_by="closed_at", desc=True, limit=cfg.max_rows) or []
                    cfs = self.repo.select("counterfactuals", {"strategy_id": c.id, "status": "RESOLVED"},
                                           order_by="resolved_at", desc=True, limit=cfg.max_rows) or []
                    _SELECTOR_ROWS[key] = (now, trades, cfs, self.repo)
                new[c.id] = allocate_strategy(c.id, c.version, self.symbol, trades, cfs,
                                              self.strategy_lab_hints.get(c.id), self.strategy_lab_created_at,
                                              self.allocations.get(c.id), cfg, now)
            applied = cfg.mode == "auto"
            changes = [(sid, self.allocations.get(sid), a) for sid, a in new.items()
                       if self.allocations.get(sid) is None or self.allocations[sid].state != a.state
                       or set(self.allocations[sid].vetoed_regimes) != set(a.vetoed_regimes)]
            self.allocations = new
            self.repo.set_flag(f"selector:{self.symbol}",
                               {"computed_at": utcnow().isoformat(), "mode": cfg.mode,
                                "allocations": {sid: a.to_dict() for sid, a in new.items()}}, "selector")
            for sid, before, a in changes:
                self.telemetry.enqueue("selector_history", {
                    "created_at": utcnow(), "symbol": self.symbol, "strategy_id": sid,
                    "previous_state": before.state if before else None, "state": a.state,
                    "multiplier": a.multiplier, "reason": a.reason, "applied": applied, "metrics": a.to_dict()})
                if before is not None:
                    veto = f"; benched in {', '.join(a.vetoed_regimes)}" if a.vetoed_regimes else ""
                    self._alert("INFO", f"Strategy selector: {sid} on {self.symbol} {before.state} -> {a.state} "
                                        f"({a.reason}{veto})" + ("" if applied else " - shadow mode, not applied"))
        except Exception:
            log.exception("strategy selector failed for %s; keeping previous allocations", self.symbol)

    def _m15_atr(self) -> Optional[float]:
        m15 = self.ff.get("M15")
        if m15 is None or not len(m15):
            return None
        v = m15.atr(14)[m15.last]
        return float(v) if np.isfinite(v) else None

    def _straddle_block_reason(self, arming: bool) -> Optional[str]:
        """Same safety posture as the signal pipeline. SPREAD_SHOCK is handled by the manager (before release only)."""
        if not self._symbol_enabled():
            return "SYMBOL_TRADING_STOPPED"
        if self.kill.blocking:
            return "KILL_SWITCH"
        if self.state is not StartupState.ACTIVE:
            return f"STARTUP_{self.state}"
        if self.risk is None or self.risk.state in (RiskState.NO_NEW_TRADES, RiskState.EMERGENCY):
            return f"RISK_STATE_{self.risk.state if self.risk else 'UNKNOWN'}"
        blockers = [b for b in self.health.report(self.now_utc()).blockers if b != "SPREAD_SHOCK"]
        if blockers:
            return ",".join(blockers)
        if self.account is None or not self._account_refresh_ok or not self._global_positions_known:
            return "ACCOUNT_STATE_UNAVAILABLE"
        if not self.account.trade_allowed or not self.account.trade_expert:
            return "BROKER_TRADING_DISABLED"
        if self.account.margin > 0 and self.account.margin_level < 120.0:
            return "MARGIN_LEVEL_LOW"
        if arming:
            if self._loss_guard()[1]:
                return "SYMBOL_LOSS_COOLDOWN"
            if any(t.get("symbol") == self.symbol for t in self.book.active.values()) or \
                    any(p.symbol == self.symbol for p in self.positions_cache):
                return "SYMBOL_ALREADY_EXPOSED"
            # Resting straddle legs get their OWN ceiling and are kept out of the ordinary position count.
            # Two legs on one symbol are not two positions - OCO cancels the loser the moment the winner
            # fills - so charging both against max_concurrent_positions meant the first five symbols used
            # the whole allowance and every later symbol was blocked, arbitrarily, at every single event.
            # Money at risk is still bounded by the portfolio reservation in _reserve_portfolio_risk.
            if self._armed_straddle_symbols() >= self.cfg.news_straddle.max_armed_symbols:
                return "MAX_ARMED_STRADDLES"
            if self._non_straddle_open_count() + 1 > self.cfg.risk.max_concurrent_positions:
                return "MAX_CONCURRENT_POSITIONS"
        return None

    def _armed_straddle_symbols(self) -> int:
        """Symbols currently holding resting straddle legs, across every core sharing this account."""
        cores = getattr(self, "portfolio_cores", None) or [self]
        n = 0
        for core in cores:
            straddle = getattr(core, "straddle", None)
            try:
                if straddle is not None and straddle.is_armed():
                    n += 1
            except Exception:
                log.debug("could not read straddle state for %s", getattr(core, "symbol", "?"), exc_info=True)
        return n

    def _non_straddle_open_count(self) -> int:
        """Open positions and in-flight trades that are NOT resting straddle legs."""
        from app.execution.straddle import STRATEGY_ID as STRADDLE_ID
        cores = getattr(self, "portfolio_cores", None) or [self]
        straddle_tickets = set()
        n = 0
        for core in cores:
            for t in getattr(core, "book", None).active.values() if getattr(core, "book", None) else []:
                if str(t.get("strategy_id")) == STRADDLE_ID:
                    ticket = t.get("broker_ticket")
                    if ticket is not None:
                        straddle_tickets.add(int(ticket))
                else:
                    n += 1
        n += sum(1 for p in self.positions_cache if int(getattr(p, "ticket", -1)) not in straddle_tickets)
        return n + self._portfolio_pending_count()

    def _straddle_size(self, side, price: float, sl: float, share: float) -> SizingResult:
        from types import SimpleNamespace
        est = estimate_from_stats({}, self.cfg.risk.kelly_shrinkage_trades)
        return self._size(SimpleNamespace(plan=SimpleNamespace(direction=side)), price, sl, est, share)

    def _straddle_cost(self, plan, tick: Tick) -> Optional[str]:
        est = estimate_from_stats({}, self.cfg.risk.kelly_shrinkage_trades)
        g = edge_gate(est, self.cost.estimate(plan, self.spec, tick.spread), self.cfg.cost, self.env)
        return None if g.passed else g.reason

    def _reload_calendar(self) -> None:
        try:
            rows = self.repo.select("economic_events")
            self.calendar.set_events([EconomicEvent(r["event_time"] if isinstance(r["event_time"], datetime)
                                                    else datetime.fromisoformat(str(r["event_time"])),
                                                    r["currency"], r["impact"], r["title"]) for r in rows])
        except Exception as e:
            log.warning("calendar reload failed: %s", e)

    # ================================================================== commands
    def _process_commands(self) -> None:
        for name, payload, fut in self.bridge.drain_commands():
            try:
                fut.set_result(self._command(name, payload or {}))
            except Exception as e:
                log.warning("command %s failed: %s", name, e)
                fut.set_result({"ok": False, "error": str(e)})

    def _audit(self, rec: dict) -> None:
        self.ledger.insert("config_versions", {"created_at": utcnow(), "config_version": rec.get("config_version"),
                                               "actor": rec.get("actor"), "action": rec.get("action"),
                                               "path": rec.get("path"), "old_value": rec.get("old_value"),
                                               "new_value": rec.get("new_value"), "reason": rec.get("reason"),
                                               "full_config": rec.get("full_config")})

    def _status_reasons(self, report, *, include_kill_switch: bool = True) -> List[dict]:
        reasons = []
        if include_kill_switch and self.kill.blocking:
            detail = self.kill.reason or ", ".join(sorted(self.kill.volatile_reasons))
            reasons.append({"code": "KILL_SWITCH_ENGAGED", "severity": "CRITICAL",
                            "message": detail or "Manual/system kill switch is engaged"})
        for blocker in report.blockers:
            severity = "WARNING" if blocker in ("SPREAD_SHOCK", "MARKET_CLOSED") else "CRITICAL"
            reasons.append({"code": blocker, "severity": severity, "message": blocker.replace("_", " ").title()})
        if self.risk is None:
            reasons.append({"code": "RISK_STATE_UNKNOWN", "severity": "CRITICAL",
                            "message": "Risk state has not been calculated"})
        elif self.risk.state in (RiskState.NO_NEW_TRADES, RiskState.EMERGENCY):
            reasons.append({"code": f"RISK_STATE_{self.risk.state}", "severity": "CRITICAL",
                            "message": self.risk.reason})
        elif self.risk.state is not RiskState.NORMAL:
            reasons.append({"code": f"RISK_STATE_{self.risk.state}", "severity": "WARNING",
                            "message": self.risk.reason})
        if self.account is None or not self._account_refresh_ok or not self._global_positions_known:
            reasons.append({"code": "ACCOUNT_STATE_UNAVAILABLE", "severity": "CRITICAL",
                            "message": "Account and broker positions must be refreshed"})
        elif not self.account.trade_allowed or not self.account.trade_expert:
            reasons.append({"code": "BROKER_TRADING_DISABLED", "severity": "CRITICAL",
                            "message": "Broker account or expert trading permission is disabled"})
        elif self.account.margin > 0 and self.account.margin_level < 120.0:
            reasons.append({"code": "MARGIN_LEVEL_LOW", "severity": "CRITICAL",
                            "message": f"Margin level {self.account.margin_level:.1f}% is below 120%"})
        if getattr(self, "_loss_cooldown_active", False):
            reasons.append({"code": "SYMBOL_LOSS_COOLDOWN", "severity": "CRITICAL",
                            "message": f"Loss cooldown active after {self._last_loss_streak} consecutive losses"})
        if self.state is not StartupState.ACTIVE:
            reasons.append({"code": f"STARTUP_{self.state}", "severity": "CRITICAL",
                            "message": f"Trading core state is {self.state}"})
        return reasons

    def _force_reset_resume(self, actor: str, reason: str) -> dict:
        if len(reason) < 3:
            return {"ok": False, "error": "a reason is required to reset and resume"}
        if not self.health.db_ok:
            return {"ok": False, "error": "database still unavailable"}
        if self.state is not StartupState.ACTIVE:
            return {"ok": False, "error": f"core is not ACTIVE ({self.state})"}

        self._refresh_account()
        self._update_risk(force_persist=True)
        report = self.health.report(self.now_utc())
        streak, loss_blocked = self._loss_guard()
        reasons = self._status_reasons(report, include_kill_switch=False)
        if loss_blocked:
            reasons.append({"code": "SYMBOL_LOSS_COOLDOWN", "severity": "CRITICAL",
                            "message": f"Loss cooldown active after {streak} consecutive losses"})
        if reasons:
            return {"ok": False, "error": "safety checks failed; trading remains halted",
                    "blockers": reasons}

        previous_error_count = self._consecutive_errors
        previous_kill_state = self.kill.as_dict()
        self._consecutive_errors = 0
        rec = self.kill.release(actor, reason)
        stale_count = len(self.pending)
        for item in self.pending:
            self._reject(item["signal"], Gate.STALE, "FORCE_RESET_INVALIDATED_PENDING_SIGNAL")
        self.pending = []
        self._audit({"actor": actor, "action": "FORCE_RESET_RESUME", "path": "kill_switch",
                     "old_value": {"kill_switch": previous_kill_state,
                                   "consecutive_errors": previous_error_count},
                     "new_value": {"kill_switch": rec, "invalidated_pending_signals": stale_count},
                     "reason": reason})
        self._alert("WARNING", f"Trading reset and resumed by {actor}: {reason}; "
                               f"{stale_count} pending signals invalidated")
        return {"ok": True, "kill_switch": rec, "invalidated_pending_signals": stale_count,
                "risk": self.risk.to_dict() if self.risk else None}

    def _set_symbol_trading(self, p: dict, actor: str, reason: str) -> dict:
        """Dashboard Start/Stop button for one symbol. STOP = no new entries; open positions stay managed."""
        symbol = str(p.get("symbol") or "").strip()
        cores = getattr(self, "portfolio_cores", None) or [self]
        target = next((c for c in cores if c.symbol == symbol), None)
        if target is None:
            return {"ok": False, "error": f"unknown symbol '{symbol}' (active: {', '.join(c.symbol for c in cores)})"}
        try:
            enabled = parse_enabled(p.get("enabled"))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        reason = reason or ("manual start from dashboard" if enabled else "manual stop from dashboard")
        result = self.symbol_control.set(symbol, enabled, actor, reason)
        self._audit({"actor": actor, "action": "SYMBOL_TRADING_START" if enabled else "SYMBOL_TRADING_STOP",
                     "path": f"symbols.{symbol}.trading", "old_value": result["previous"], "new_value": enabled,
                     "reason": reason})
        cancelled = 0
        if not enabled:
            cancelled = target._on_symbol_stopped(reason)
        self._alert("WARNING", f"{symbol} trading {'STARTED' if enabled else 'STOPPED'} by {actor}: {reason}"
                               + (f" ({cancelled} waiting/resting orders cancelled)" if cancelled else ""))
        return {"ok": True, "symbol": symbol, "enabled": enabled, "changed": result["changed"],
                "persisted": result["persisted"], "cancelled_waiting_orders": cancelled}

    def _on_symbol_stopped(self, reason: str) -> int:
        """Called on the core that owns the symbol right after STOP: drop everything not yet filled."""
        n = 0
        for item in list(self.pending):
            self._reject(item["signal"], Gate.SYMBOL_DISABLED, "SYMBOL_TRADING_STOPPED")
            n += 1
        self.pending = []
        straddle = getattr(self, "straddle", None)
        if straddle is not None:
            n += straddle.cancel_resting("SYMBOL_TRADING_STOPPED")
        return n

    def _command(self, name: str, p: dict) -> dict:
        actor = p.get("actor", "dashboard")
        reason = (p.get("reason") or "").strip()
        if name == "halt":
            rec = self.kill.engage(reason or "manual halt", actor)
            self.status_monitor.trigger_halt(reason or "manual halt")
            self._audit({"actor": actor, "action": "KILL_SWITCH_ENGAGE", "path": "kill_switch",
                         "new_value": rec, "reason": reason or "manual halt"})
            self._alert("CRITICAL", f"Kill switch triggered by {actor}: {reason or 'manual halt'}")
            for item in self.pending:
                self._reject(item["signal"], Gate.KILL_SWITCH, "KILL_SWITCH_ENGAGED")
            self.pending = []
            return {"ok": True, "kill_switch": rec}
        if name == "force_reset_resume":
            return self._force_reset_resume(actor, reason)
        if name == "set_symbol_trading":
            return self._set_symbol_trading(p, actor, reason)
        if name == "resume":
            if len(reason) < 3:
                return {"ok": False, "error": "a reason is required to resume"}
            if not self.health.db_ok:
                return {"ok": False, "error": "database still unavailable"}
            if self.risk and self.risk.state is RiskState.EMERGENCY:
                return {"ok": False, "error": "risk state is EMERGENCY - review drawdown before resuming"}
            rec = self.kill.release(actor, reason)
            self._audit({"actor": actor, "action": "KILL_SWITCH_RELEASE", "path": "kill_switch",
                         "new_value": rec, "reason": reason})
            self._alert("WARNING", f"Trading resumed by {actor}: {reason}")
            return {"ok": True, "kill_switch": rec}
        if name in ("enable_strategy", "disable_strategy"):
            rec = self.cfgm.set_strategy_enabled(p["strategy_id"], name == "enable_strategy", actor, reason)
            self._audit(rec)
            if name == "disable_strategy":
                self._alert("WARNING", f"Strategy {p['strategy_id']} disabled by {actor}: {reason}")
            self._refresh_strategy_stats()
            return {"ok": True, "change": {k: v for k, v in rec.items() if k != "full_config"}}
        if name == "set_config":
            rec = self.cfgm.apply_change(p["path"], p["value"], actor, reason)
            self._apply_config()
            self._audit(rec)
            return {"ok": True, "change": {k: v for k, v in rec.items() if k != "full_config"}}
        if name == "close_all":
            if p.get("confirm") != "CLOSE_ALL":
                return {"ok": False, "error": "type CLOSE_ALL to confirm"}
            cores = getattr(self, "portfolio_cores", None) or [self]
            if not any(core.manager is not None for core in cores):
                return {"ok": False, "error": "no execution in this environment"}
            n = 0
            for core in cores:
                if core.manager is None:
                    continue
                for t in list(core.book.open_positions()):
                    if core.manager.close(t, 1.0, "MANUAL_CLOSE_ALL"):
                        n += 1
            self._audit({"actor": actor, "action": "CLOSE_ALL", "path": "positions", "new_value": {"closed": n},
                         "reason": reason or "manual"})
            self._alert("CRITICAL", f"CLOSE ALL executed by {actor}: {n} positions")
            return {"ok": True, "closed": n}
        if name == "reconcile_now":
            return {"ok": True, "summary": self.recon.run() if self.recon else {}}
        if name == "reload_events":
            rows = self.repo.select("economic_events")
            self.calendar.set_events([EconomicEvent(r["event_time"] if isinstance(r["event_time"], datetime)
                                                    else datetime.fromisoformat(str(r["event_time"])),
                                                    r["currency"], r["impact"], r["title"]) for r in rows])
            return {"ok": True, "events": len(self.calendar.events)}
        return {"ok": False, "error": f"unknown command {name}"}

    def _apply_config(self) -> None:
        self.cfg = self.cfgm.app
        self.cost.cfg = self.cfg.cost
        self.cost.slippage.default = self.cfg.cost.default_slippage_points
        self.dd.cfg = self.cfg.risk
        self.health.cfg = self.cfg.health
        self.calendar.cfg = self.cfg.events
        self.router.cfg = self.cfg
        if self.exec is not None:
            self.exec.cfg = self.cfg
        if self.cf is not None:
            self.cf.enabled = self.cfg.counterfactual.enabled and self.cfg.research.counterfactual_enabled
        if self.manager is not None:
            self.manager.profit_trail_activation_money = self.cfg.execution.quick_profit_close_money
            self.manager.profit_lock_floor_money = self.cfg.execution.profit_lock_floor_money
            self.manager.profit_lock_stop_buffer_points = self.cfg.execution.profit_lock_stop_buffer_points
            self.manager.commission_per_lot_round_trip = self.commission_rt()
            self.manager.slippage_points = self.cfg.cost.default_slippage_points
            self.manager.hard_profit_target_money = self.cfg.execution.hard_profit_target_money
            self.manager.small_win_activation_money = self.cfg.execution.small_win_activation_money
            self.manager.small_win_floor_money = self.cfg.execution.small_win_floor_money
            self.manager.ambition_activation_money = self.cfg.execution.ambition_activation_money
            self.manager.ambition_floor_money = self.cfg.execution.ambition_floor_money
            self.manager.floor_r = self._floor_r()
            self.manager.min_sl_step_r = float(self.cfg.execution.min_sl_step_r)
            self.manager.profit_lock_cost_multiple = float(self.cfg.execution.profit_lock_cost_multiple)
            self.manager.peak_persist_step_r = float(self.cfg.execution.peak_persist_step_r)

    # ================================================================== publish
    def _publish(self) -> None:
        a = self.account
        tick = self.last_tick
        rep = self.health.report(self.now_utc())
        utc = self.now_utc()
        session = str(self.sessions.classify(utc))
        m15 = self.ff.get("M15")
        atr = None
        if m15 is not None and len(m15):
            v = m15.atr(14)[m15.last]
            atr = float(v) if np.isfinite(v) else None
        sp_pts = tick.spread / self.spec.point if (tick and self.spec) else None
        # positions_cache is ACCOUNT-WIDE (exec_broker.positions() takes no symbol), but each core owns one
        # symbol's book, and every core writes this list to the same bridge key where the last writer wins. Using
        # only self.book.active therefore annotated whichever symbol happened to publish last and blanked the
        # rest, so a managed position appeared to lose its strategy and state between two refreshes. Annotate
        # from every core's book instead, so the row is the same whoever published it.
        cores = getattr(self, "portfolio_cores", None) or [self]
        trades_by_pos: Dict[int, dict] = {}
        digits_by_symbol: Dict[str, int] = {}
        for c in cores:
            book = getattr(c, "book", None)
            if book is not None:
                for t in book.active.values():
                    if t.get("position_id"):
                        trades_by_pos[int(t["position_id"])] = t
            spec = getattr(c, "spec", None)
            if spec is not None and getattr(c, "symbol", None):
                digits_by_symbol[c.symbol] = int(getattr(spec, "digits", 2) or 2)
        positions = []
        for p in self.positions_cache:
            t = trades_by_pos.get(p.ticket)
            positions.append({"ticket": p.ticket, "symbol": p.symbol, "side": str(p.side), "volume": p.volume,
                              "entry": p.price_open, "current": p.price_current, "floating_pnl": round(p.profit, 2),
                              "sl": p.sl, "tp": p.tp, "managed": p.magic == self.cfg.broker.magic_number,
                              # the page rounded every price to 2 dp, which on a 5-digit FX symbol collapses
                              # entry, current and target into the same number and hides hundreds of pips
                              "digits": digits_by_symbol.get(p.symbol),
                              "strategy": t.get("strategy_id") if t else None, "state": t.get("state") if t else None})
        nxt = self.calendar.next_event(utc)
        start_eq = self.risk.day_start_equity if self.risk else None
        status_reasons = self._status_reasons(rep)
        can_trade = (
            self.state is StartupState.ACTIVE
            and rep.trading_allowed
            and not self.kill.blocking
            and self.risk is not None
            and self.risk.state not in (RiskState.NO_NEW_TRADES, RiskState.EMERGENCY)
            and self.account is not None
            and self._account_refresh_ok
            and self._global_positions_known
            and self.account.trade_allowed
            and self.account.trade_expert
            and (self.account.margin <= 0 or self.account.margin_level >= 120.0)
            and not getattr(self, "_loss_cooldown_active", False)
        )
        halt_reason = (status_reasons[0]["code"] if status_reasons else
                       (f"STARTUP_{self.state}" if self.state is not StartupState.ACTIVE else "INITIALIZING"))
        pause_transition = self.status_monitor.update(can_trade, halt_reason)
        if pause_transition is not None:
            details = {**pause_transition, "symbol": self.symbol}
            self.telemetry.enqueue("system_health", {
                "created_at": utcnow(), "component": "TRADING_PAUSE", "status": "RESUMED",
                "startup_state": str(self.state), "message": "Trading resumed after safety checks",
                "details": details,
            })
            self._emit("trading_resumed", details)
        if can_trade and not status_reasons:
            strategy_diagnostics = [diagnostic for values in self.diagnostics.values() for diagnostic in values]
            waiting = next((d for d in strategy_diagnostics
                            if d in ("NO_TRIGGER", "NO_M1_DATA", "NO_TRIGGER_TIMEFRAME_DATA", "NO_M1_HISTORY")), None)
            if not status_reasons:
                status_reasons.append({
                    "code": "SYSTEM_READY", "severity": "INFO",
                    "message": (f"System ready; awaiting candle confirmation ({waiting})"
                               if waiting else "System ready; awaiting a strategy signal"),
                })
        state = {
            "environment": str(self.env), "symbol": self.symbol, "startup_state": str(self.state),
            "system_status": "TRADING" if can_trade else (
                "HALTED" if self.state is StartupState.ACTIVE else str(self.state)),
            "components": rep.components, "blockers": rep.blockers, "health_details": rep.details,
            "status_reasons": status_reasons,
            "trading_status": self.status_monitor.status(),
            "kill_switch": self.kill.as_dict(),
            "risk": self.risk.to_dict() if self.risk else None,
            "account": None if a is None else {
                "balance": a.balance, "equity": a.equity, "margin": a.margin, "free_margin": a.margin_free,
                "margin_level": a.margin_level, "floating_pnl": a.profit, "currency": a.currency,
                "trade_mode": a.trade_mode, "login": a.login, "server": a.server,
                "today_pnl": (a.equity - start_eq) if start_eq else None,
                "today_return": ((a.equity - start_eq) / start_eq) if start_eq else None,
                "today_realized": self._today_realized},
            "positions": positions,
            "market": None if tick is None else {
                "bid": tick.bid, "ask": tick.ask, "spread_points": sp_pts,
                "spread_pctile": self.spread.percentile(sp_pts, session, 50) if (self.spread and sp_pts) else None,
                "spread_limit_points": self.spread_limit_points(session)[0] if self.spec else None,
                # Report the strategies ACTUALLY blocked right now with each one's own reason, rather than
                # labelling the whole gate list with micro_scalper's verdict.
                "research_block": (lambda bs: {"strategies": sorted(bs), "reason": "; ".join(
                    f"{sid}: {r}" for sid, r in sorted(bs.items()))} if bs else None)(
                    {sid: g["reason"] for sid in self.cfg.research_lab.gate_strategies
                     if (g := self.research_gate(sid)).get("blocked")}),
                "spread_limit_basis": self.spread_limit_points(session)[1] if self.spec else None,
                "atr_m15": atr, "volatility_state": str(self.regime_state.vol), "session": session,
                "regime": self.regime_state.label, "regime_detail": self.regime_state.to_dict(),
                "event_state": str(self.calendar.state(utc)),
                "next_event": None if nxt is None else {"time": nxt.time.isoformat(), "title": nxt.title},
                "tick_age_sec": rep.details.get("tick_age_sec"),
                "broker_latency_ms": (self.data.terminal_info() or {}).get("ping_ms"),
                "signal_age_sec": (self.server_now() - self.last_signal["created_broker_time"])
                if self.last_signal else None,
                "tick_rate": self.spread.tick_rate() if self.spread else None},
            "strategies": {sid: {**v, "eligibility_reason": None, "diagnostics": self.diagnostics.get(sid, [])}
                           for sid, v in self._strategy_stats.items()},
            "pending_signals": [{"signal_id": i["signal"].id, "reason": i["reason"]} for i in self.pending],
            "counterfactuals_open": len(self.cf.open) if self.cf else 0,
            "slippage": self.cost.slippage.stats(),
            "server_time_utc": utc.isoformat(),
        }
        portfolio = getattr(self, "portfolio_cores", None)
        names = [c.symbol for c in portfolio if getattr(c, "symbol", "")] if portfolio else [self.symbol]
        state["symbol_trading"] = self.symbol_control.snapshot(names)
        if not portfolio:
            state["microstructure"] = {self.symbol: self.micro_snapshot()}
            state["adaptive"] = {self.symbol: {**self.adaptive.to_dict(), "enabled": self.cfg.adaptive.enabled}}
            state["news_straddle"] = {self.symbol: self.straddle.status()} if getattr(self, "straddle", None) else {}
        self.bridge.update_state(**state)
        self._emit("account_update", {"account": state["account"], "positions": positions})
        if tick is not None:
            self._emit("tick_update", {"bid": tick.bid, "ask": tick.ask, "spread_points": sp_pts})

    # ================================================================== shutdown
    def shutdown(self) -> None:
        log.info("shutting down (broker-side protection stays in place)")
        try:
            self.repo.set_flag("risk_tracker", self.dd.state_for_persistence(), "shutdown")
        except Exception:
            pass
        try:
            self._flush_spreads()
        except Exception:
            pass
        if self.straddle is not None:
            try:      # an OCO cannot run without the bot, so do not leave resting orders behind
                self.straddle.cancel_resting("SHUTDOWN")
            except Exception:
                log.exception("could not cancel resting straddle orders at shutdown")
        self.state = StartupState.STOPPED
        self.bridge.update_state(startup_state="STOPPED", system_status="STOPPED")
        if self.shutdown_broker:
            try:
                self.data.shutdown()
            except Exception:
                pass
