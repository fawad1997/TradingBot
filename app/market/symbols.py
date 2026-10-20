"""Broker symbol names across Exness account types.

The same instrument is named differently per account type: "XAUUSDm" on a Standard account, "XAUUSD" (or another
short suffix) on Raw Spread / Zero / Pro accounts. Config written for one account therefore must still work on another,
so names are compared by their BASE (the name without a trailing lowercase suffix or ".xxx" suffix).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, Optional

log = logging.getLogger("market.symbols")

_SUFFIX = re.compile(r"(\.[A-Za-z0-9]+|[a-z]+)$")


def base_name(symbol: str) -> str:
    """'XAUUSDm' -> 'XAUUSD', 'XAUUSD247m' -> 'XAUUSD247', 'EURUSD.raw' -> 'EURUSD', 'XAUUSD' -> 'XAUUSD'."""
    s = (symbol or "").strip()
    b = _SUFFIX.sub("", s)
    return b or s


def same_instrument(a: str, b: str) -> bool:
    return bool(a) and bool(b) and base_name(a).upper() == base_name(b).upper()


def lookup(mapping: Dict[str, Any], symbol: str, default: Any = None) -> Any:
    """mapping[symbol], or the entry configured for the same instrument under another account's name."""
    if not mapping:
        return default
    if symbol in mapping:
        return mapping[symbol]
    for key, value in mapping.items():
        if same_instrument(key, symbol):
            return value
    return default


def contains(symbols: Iterable[str], symbol: str) -> bool:
    return any(s == symbol or same_instrument(s, symbol) for s in (symbols or []))


def resolve(adapter, wanted: str) -> Optional[str]:
    """The name this broker account actually uses for `wanted`, or None when it has no such instrument."""
    try:
        if adapter.select_symbol(wanted):
            return wanted
    except Exception:
        pass
    base = base_name(wanted)
    try:
        found = adapter.find_symbols(f"*{base}*") or []
    except Exception:
        found = []
    matches = [s for s in found if base_name(s).upper() == base.upper()]
    # prefer the bare name, then the shortest suffix
    for cand in sorted(matches, key=lambda s: (s != base, len(s), s)):
        try:
            if adapter.select_symbol(cand):
                if cand != wanted:
                    log.info("symbol %s is named %s on this account", wanted, cand)
                return cand
        except Exception:
            continue
    return None
