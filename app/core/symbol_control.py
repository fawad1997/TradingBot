"""Per-symbol Start / Stop switch (dashboard "Symbols" card).

* STOPPED  -> the symbol gets no NEW entries: no strategy evaluation, pending
              (spread-WAIT) signals are rejected, resting straddle orders are cancelled.
              Positions that are already open keep their broker-side SL/TP and normal trade management -
              stopping a symbol never abandons an open position.
* RUNNING  -> normal behaviour (every other safety gate still applies).

State is durable (system_flags / "symbol_trading") so a restart does not silently re-enable a symbol
you stopped. A symbol that was never touched is RUNNING, i.e. existing behaviour is unchanged.
The object is shared by all per-symbol trading cores and is thread safe.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

log = logging.getLogger("core.symbol_control")

FLAG_KEY = "symbol_trading"


def parse_enabled(value: Any) -> bool:
    """Accepts True/False, 1/0 and the strings start/stop/true/false/on/off/yes/no."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "start", "started", "run", "running", "enable", "enabled"):
        return True
    if text in ("0", "false", "no", "off", "stop", "stopped", "disable", "disabled"):
        return False
    raise ValueError(f"cannot interpret {value!r} as start/stop")


class SymbolTradingControl:
    def __init__(self, repo=None):
        self.repo = repo
        self._lock = threading.RLock()
        self._state: Dict[str, dict] = {}
        self._loaded = False

    # ------------------------------------------------------------------ persistence
    def load(self, force: bool = False) -> None:
        """Read the durable state once (every core calls this at startup; only the first call reads)."""
        with self._lock:
            if self._loaded and not force:
                return
            self._loaded = True
            if self.repo is None:
                return
            try:
                saved = self.repo.get_flag(FLAG_KEY)
            except Exception as exc:        # a read failure must not stop the bot; default is RUNNING
                log.warning("could not read symbol trading switches: %s", exc)
                return
            symbols = (saved or {}).get("symbols") if isinstance(saved, dict) else None
            if isinstance(symbols, dict):
                self._state = {str(k): dict(v) for k, v in symbols.items() if isinstance(v, dict)}
                stopped = sorted(k for k, v in self._state.items() if not v.get("enabled", True))
                if stopped:
                    log.warning("symbols STOPPED by operator (persisted): %s", ", ".join(stopped))

    def _persist(self, actor: str) -> bool:
        if self.repo is None:
            return False
        try:
            self.repo.set_flag(FLAG_KEY, {"symbols": self._state}, actor)
            return True
        except Exception as exc:
            log.error("could not persist symbol trading switch (kept in memory only): %s", exc)
            return False

    # ------------------------------------------------------------------ queries
    def is_enabled(self, symbol: str) -> bool:
        with self._lock:
            return bool(self._state.get(symbol, {}).get("enabled", True))

    def record(self, symbol: str) -> dict:
        with self._lock:
            return dict(self._state.get(symbol, {"enabled": True, "reason": "", "by": "", "at": None}))

    def snapshot(self, symbols: Optional[Iterable[str]] = None) -> Dict[str, dict]:
        """{symbol: {enabled, reason, by, at}} for the requested symbols (default RUNNING if untouched)."""
        with self._lock:
            names = list(dict.fromkeys(symbols)) if symbols is not None else list(self._state)
            return {s: {"enabled": bool(self._state.get(s, {}).get("enabled", True)),
                        "reason": self._state.get(s, {}).get("reason", ""),
                        "by": self._state.get(s, {}).get("by", ""),
                        "at": self._state.get(s, {}).get("at")} for s in names}

    # ------------------------------------------------------------------ change
    def set(self, symbol: str, enabled: bool, actor: str = "dashboard", reason: str = "") -> dict:
        """Returns {changed, previous, current, persisted}. Applied in memory even if persisting fails."""
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")
        with self._lock:
            previous = self.is_enabled(symbol)
            rec = {"enabled": bool(enabled), "reason": (reason or "").strip(), "by": actor,
                   "at": datetime.now(timezone.utc).isoformat()}
            self._state[symbol] = rec
            persisted = self._persist(actor)
            log.warning("symbol %s trading %s by %s: %s", symbol, "STARTED" if enabled else "STOPPED", actor,
                        rec["reason"] or "-")
            return {"changed": previous != bool(enabled), "previous": previous, "current": dict(rec),
                    "persisted": persisted}
