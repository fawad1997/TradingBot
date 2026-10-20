"""Route approved signals to execution handlers by venue and strategy."""
from __future__ import annotations

from typing import Callable, Protocol

from app.core.types import Signal, Tick
from app.execution.engine import ExecOutcome
from app.risk.sizing import SizingResult


class ExecutionHandler(Protocol):
    def execute(self, signal: Signal, tick: Tick, now_broker: float,
                resize: Callable[[float, float], SizingResult], spread_ok: Callable[[Tick], bool],
                session: str, regime: str) -> ExecOutcome: ...


class ExecutionDispatcher:
    def __init__(self, handlers: dict[tuple[str, str], ExecutionHandler]):
        self.handlers = dict(handlers)

    def dispatch(self, category: str, signal: Signal, tick: Tick, now_broker: float,
                 resize: Callable[[float, float], SizingResult], spread_ok: Callable[[Tick], bool],
                 session: str, regime: str) -> ExecOutcome:
        handler = (self.handlers.get((category, signal.strategy_id))
                   or self.handlers.get((category, "*")))
        if handler is None:
            return ExecOutcome(
                None, "REJECTED",
                f"UNKNOWN_EXECUTION_ROUTE:{category}:{signal.strategy_id}",
            )
        return handler.execute(signal, tick, now_broker, resize, spread_ok, session, regime)
