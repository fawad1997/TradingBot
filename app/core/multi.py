"""Round-robin orchestration for independent per-symbol trading cores."""
from __future__ import annotations

import threading
import logging
import time
from datetime import datetime, timezone
from typing import Callable, List, Optional

from app.core.bridge import Bridge
from app.core.engine import StartupError, TradingCore
from app.core.symbol_control import SymbolTradingControl
from app.market.sessions import SessionClassifier

log = logging.getLogger("core.multi")


class MultiAssetRunner:
    def __init__(self, cfgm, symbols: List[str], core_factory: Callable[[str], TradingCore], bridge: Bridge,
                 loop_interval: float = 0.25, symbol_control: Optional[SymbolTradingControl] = None):
        self.cfgm = cfgm
        self.symbol_control = symbol_control
        self.symbols = list(dict.fromkeys(symbols))
        self.core_factory = core_factory
        self.bridge = bridge
        self.loop_interval = loop_interval
        self.cores: List[TradingCore] = []
        self.stop_event = threading.Event()

    def start(self) -> None:
        failures = {}
        for symbol in self.symbols:
            core = self.core_factory(symbol)
            try:
                core.start()
            except StartupError as exc:
                if not core.data.is_connected():
                    raise
                failures[symbol] = str(exc)
                log.warning("Skipping %s; symbol core startup failed: %s", symbol, exc)
                self.bridge.emit("health_event", {"level": "WARNING", "message":
                                                  f"Skipping {symbol}: {exc}"})
                continue
            if self.symbol_control is not None:
                core.symbol_control = self.symbol_control      # one shared Start/Stop switchboard
            self.cores.append(core)
        if not self.cores:
            raise StartupError(f"no configured symbol could start: {failures}")
        for core in self.cores:
            core.portfolio_cores = self.cores
        # One kill switch for the whole book. A dashboard command is executed by whichever core drains it, so
        # with a switch per core "Stop all new trades" halted one symbol and "resume" released one symbol.
        shared = self.cores[0].kill
        for core in self.cores[1:]:
            shared.volatile_reasons |= core.kill.volatile_reasons
            core.kill = shared
        shared.restore()        # every engage/release is persisted, so the stored state is the latest one
        self.bridge.update_state(symbols=[core.symbol for core in self.cores],
                                  symbol_scan_mode="round_robin")
        self._update_market_status()
        self._publish_symbol_state()
        log.info("multi-asset runner active on %s", ", ".join(core.symbol for core in self.cores))

    def _always_open(self, symbol: str) -> bool:
        try:
            from app.market.symbols import contains
            return contains(self.cfgm.app.broker.always_open_symbols, symbol)
        except AttributeError:      # unit tests run the runner without a ConfigManager
            return False

    def _update_market_status(self) -> None:
        """Per-symbol market status from each core's own broker quotes, not one shared weekend guess.

        The old version asked SessionClassifier, which only models the FX weekend, so every symbol read OPEN
        on a weekday - including equity CFDs whose session had not started. Each core now answers for its own
        symbol from its last quote; the estimate survives only as the fallback inside market_state().
        """
        now = datetime.now(timezone.utc)
        statuses = {}
        for core in self.cores:
            try:
                statuses[core.symbol] = core.market_state()
            except Exception:                 # a status panel must never take the runner down
                log.exception("market_state failed for %s", core.symbol)
                statuses[core.symbol] = {
                    **SessionClassifier.market_status(now, always_open=self._always_open(core.symbol)),
                    "schedule_type": ("always_open_24x7" if self._always_open(core.symbol)
                                      else "estimated_weekend_hours"),
                    "basis": "weekend estimate (core unavailable)", "quote_age_sec": None,
                }
        self.bridge.update_state(symbol_market_status=statuses)

    def _publish_symbol_state(self) -> None:
        """Dashboard: Start/Stop switch per symbol + the news-straddle status of every symbol."""
        names = [core.symbol for core in self.cores]
        update = {}
        if self.symbol_control is not None:
            update["symbol_trading"] = self.symbol_control.snapshot(names)
        update["adaptive"] = {core.symbol: {**core.adaptive.to_dict(), "enabled": core.cfg.adaptive.enabled}
                              for core in self.cores if getattr(core, "adaptive", None) is not None}
        update["microstructure"] = {core.symbol: core.micro_snapshot() for core in self.cores
                                    if hasattr(core, "micro_snapshot")}
        update["news_straddle"] = {core.symbol: core.straddle.status() for core in self.cores
                                   if getattr(core, "straddle", None) is not None}
        self.bridge.update_state(**update)

    def run(self) -> None:
        if not self.cores:
            raise RuntimeError("MultiAssetRunner.start() must succeed before run()")
        try:
            while not self.stop_event.is_set():
                self._update_market_status()
                self._publish_symbol_state()
                started = time.perf_counter()
                for core in self.cores:
                    if self.stop_event.is_set():
                        break
                    try:
                        core.step()
                        core._consecutive_errors = 0
                    except Exception as exc:
                        core._consecutive_errors += 1
                        log.exception("symbol loop error for %s: %s", core.symbol, exc)
                        if core._consecutive_errors == 10:
                            core.kill.engage("REPEATED_LOOP_ERRORS", "system")
                            core._alert("CRITICAL", f"Repeated loop errors on {core.symbol}; portfolio halted: {exc}")
                elapsed = time.perf_counter() - started
                self.stop_event.wait(max(0.0, self.loop_interval - elapsed))
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        for core in self.cores:
            core.shutdown()
        if self.cores:
            try:
                self.cores[0].data.shutdown()
            except Exception as exc:
                log.warning("broker shutdown failed: %s", exc)
