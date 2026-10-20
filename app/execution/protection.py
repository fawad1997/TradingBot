"""Shared broker-distance checks for protective stops and targets."""
from __future__ import annotations

from app.core.types import Side, SymbolSpec, Tick


def minimum_protection_distance(spec: SymbolSpec, spread: float, safety_buffer_points: float) -> float:
    broker_points = max(0, spec.stops_level) + max(0, spec.freeze_level)
    return broker_points * spec.point + max(0.0, spread) + max(0.0, safety_buffer_points) * spec.point


def protection_distance_error(side: Side, tick: Tick, sl: float, tp: float,
                              minimum_distance: float) -> str | None:
    if side is Side.BUY:
        if sl > 0 and sl >= tick.bid - minimum_distance:
            return (f"SL_TOO_CLOSE: buy SL {sl:g} must be below bid "
                    f"{tick.bid:g} by at least {minimum_distance:g}")
        if tp > 0 and tp <= tick.ask + minimum_distance:
            return (f"TP_TOO_CLOSE: buy TP {tp:g} must be above ask "
                    f"{tick.ask:g} by at least {minimum_distance:g}")
    else:
        if sl > 0 and sl <= tick.ask + minimum_distance:
            return (f"SL_TOO_CLOSE: sell SL {sl:g} must be above ask "
                    f"{tick.ask:g} by at least {minimum_distance:g}")
        if tp > 0 and tp >= tick.bid - minimum_distance:
            return (f"TP_TOO_CLOSE: sell TP {tp:g} must be below bid "
                    f"{tick.bid:g} by at least {minimum_distance:g}")
    return None
