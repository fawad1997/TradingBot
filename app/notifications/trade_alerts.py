"""Per-trade Discord messages: what opened, what closed, for how much, and on what evidence.

The system alerts were deliberately "wake me up" only - kill switch, daily loss, broker link - because a
message per tick would train you to ignore the channel. But a message per OPEN and per CLOSE is a different
thing: a dozen or so a day, each one an event you would want to know about anyway.

What is included is chosen so a message answers the questions you would otherwise open the dashboard for:
which strategy, which direction and size, where the stop and target are, how much is at risk, what the market
looked like when it fired, and on the way out what it made or lost and why it closed.

Two rules this module must never break:

  * it cannot raise. It is called from inside TradeBook.transition, which is on the path of every state change
    a trade makes. A formatting bug here must never be able to interrupt an order or a close.
  * it cannot block. It hands text to the notifier's queue, which a background thread drains.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

OPEN_STATES = {"OPEN"}
CLOSE_STATES = {"CLOSED", "BROKER_CLOSED"}

# indicator keys worth showing, in the order a reader wants them; anything else in features is skipped so one
# message stays readable on a phone
FEATURE_ORDER = ("rule", "h1_adx", "h1_slope", "rsi", "depth", "impulse_atr", "ema_gap_atr", "h1_slope_atr",
                 "range_high", "range_low", "h1_ema", "atr", "stop_atr", "reward_risk",
                 "spread_fraction_of_stop")


def _f(v, nd=2) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f:
        return "-"
    return f"{f:,.{nd}f}"


def _money(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    return f"{'+' if f >= 0 else ''}{f:,.2f}"


def _auto(v) -> str:
    """Decimals by magnitude: an FX ATR of 0.00118 must not be shown as 0.001."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f:
        return "-"
    a = abs(f)
    if a == 0:
        return "0"
    nd = 2 if a >= 10 else (3 if a >= 1 else (5 if a >= 0.001 else 7))
    return f"{f:,.{nd}f}".rstrip("0").rstrip(".") or "0"


def _digits_for(price) -> int:
    """Enough decimals to be meaningful: FX needs 5, gold needs 2."""
    try:
        p = abs(float(price))
    except (TypeError, ValueError):
        return 2
    return 5 if p < 20 else (3 if p < 500 else 2)


# What each exit reason means, said the way you would say it out loud. The raw code never appears in the
# message: a phone notification is read once, in a hurry, and "SMALL_WIN_PROFIT_FLOOR" tells you nothing.
EXIT_PLAIN = {
    "SL": "The stop was hit.",
    "TP": "It reached its target.",
    "STOP_OUT": "The broker closed it — not enough margin.",
    "SMALL_WIN_PROFIT_FLOOR": "It was up enough to be worth protecting, so the stop was moved to keep most of it.",
    "AMBITION_PROFIT_FLOOR": "It ran far enough to lock in a bigger profit.",
    "DYNAMIC_PROFIT_FLOOR": "The trailing stop caught it after the price turned back.",
    "HARD_PROFIT_TARGET": "It hit the fixed profit target.",
    "TARGET_REACHED": "It reached its target.",
    "BREAKEVEN_AFTER_PARTIAL": "Part was banked earlier; the rest closed at break even.",
    "REMOVED_EXTERNALLY": "It was closed outside the bot.",
    "CLOSE_ALL": "You closed everything.",
    "SHUTDOWN": "It closed because the bot was shutting down.",
}

DIRECTION_WORD = {"BUY": "buy", "SELL": "sell"}
DIRECTION_PAST = {"BUY": "Bought", "SELL": "Sold"}
REGIME_PLAIN = {"TREND_UP": "trending up", "TREND_DOWN": "trending down", "RANGE": "going sideways",
                "RANGING": "going sideways", "VOLATILE": "choppy", "QUIET": "quiet", "UNKNOWN": ""}


def _past(trade: dict) -> str:
    return DIRECTION_PAST.get(str(trade.get("direction", "")).upper().split(".")[-1], "Traded")


def _dir(trade: dict) -> str:
    return DIRECTION_WORD.get(str(trade.get("direction", "")).upper().split(".")[-1], "trade")


def format_open(trade: dict) -> str:
    """A message that says what was done and what is at stake, without a single abbreviation."""
    entry = trade.get("entry_price") or trade.get("entry_requested")
    d = _digits_for(entry)
    sl, tp = trade.get("sl"), trade.get("tp")
    risk = trade.get("initial_risk_money")
    vol = trade.get("open_volume") or trade.get("filled_volume")
    side = _dir(trade)

    lines = [f"Opened a {side} on {trade.get('symbol', 'a symbol')}",
             f"Size {_f(vol, 2)}, in at {_f(entry, d)}"]

    reward = None
    if sl is not None and entry is not None:
        try:
            stop_dist = abs(float(entry) - float(sl))
            if tp is not None and stop_dist:
                reward = abs(float(tp) - float(entry)) / stop_dist
        except (TypeError, ValueError, ZeroDivisionError):
            reward = None

    if sl is not None:
        loss = f", losing {_f(risk, 2)}" if risk else ""
        lines.append(f"Stop at {_f(sl, d)}{loss} if it goes wrong")
    if tp is not None:
        gain = ""
        if risk and reward:
            try:
                gain = f", making about {_f(float(risk) * reward, 2)}"
            except (TypeError, ValueError):
                gain = ""
        lines.append(f"Target {_f(tp, d)}{gain} if it works")

    tail = [f"Rule: {trade.get('strategy_id', 'unknown')}"]
    raw_regime = str(trade.get("regime") or "").upper().split(".")[-1]
    regime = REGIME_PLAIN.get(raw_regime, raw_regime.replace("_", " ").lower())
    session = str(trade.get("session") or "").replace("_", " ").lower()
    if regime:
        tail.append(f"market {regime}")
    if session:
        tail.append(f"{session} session")
    lines.append(" · ".join(tail))
    return "\n".join(lines)


def format_close(trade: dict) -> str:
    """Leads with the money, because that is the question the notification is answering."""
    d = _digits_for(trade.get("entry_price"))
    pnl = trade.get("net_pnl")
    r = trade.get("r_multiple")
    sym = trade.get("symbol", "a symbol")

    if pnl is None:
        head = f"Closed a {_dir(trade)} on {sym}"
    elif float(pnl) >= 0:
        head = f"Made {_f(abs(float(pnl)), 2)} on {sym}"
    else:
        head = f"Lost {_f(abs(float(pnl)), 2)} on {sym}"
    lines = [head,
             f"{_past(trade)} {_f(trade.get('filled_volume'), 2)} at "
             f"{_f(trade.get('entry_price'), d)}, out at {_f(trade.get('exit_price'), d)}"]

    reason = str(trade.get("exit_reason") or "").upper()
    why = EXIT_PLAIN.get(reason)
    # "SL" only means the stop ORDER filled. On a trade whose stop had been trailed into profit, saying
    # "The stop was hit." makes a win read as a loss, which is the opposite of the truth.
    if reason in ("SL", "STOP_OUT") and pnl is not None and float(pnl) > 0:
        why = "The trailing stop caught it, keeping the profit."
    if why:
        lines.append(why)
    elif reason:
        lines.append(reason.replace("_", " ").capitalize() + ".")

    if r is not None:
        try:
            rv = float(r)
            if rv <= -0.98:
                lines.append("That is the full amount it had put at risk.")
            elif rv < 0:
                lines.append(f"That is {abs(rv):.0%} of what it had put at risk.")
            else:
                lines.append(f"That is {rv:.2f} times what it had put at risk.")
        except (TypeError, ValueError):
            pass

    mfe = trade.get("mfe_r")
    if mfe is not None and pnl is not None:
        try:
            if float(mfe) >= 0.3 and float(pnl) < 0:
                lines.append(f"It was ahead by {float(mfe):.2f} times the risk before it turned.")
        except (TypeError, ValueError):
            pass
    lines.append(f"Rule: {trade.get('strategy_id', 'unknown')}")
    return "\n".join(lines)


class TradeAlerts:
    """Formats OPEN and CLOSE messages and hands them to the notifier. Never raises, never blocks."""

    def __init__(self, notifier, on_open: bool = True, on_close: bool = True, min_abs_r: float = 0.0):
        self.notifier = notifier
        self.on_open = bool(on_open)
        self.on_close = bool(on_close)
        self.min_abs_r = max(0.0, float(min_abs_r))
        self.sent = 0

    def on_transition(self, trade: dict, new_state: str, event_type: str) -> None:
        try:
            state = str(new_state).split(".")[-1]
            if state in OPEN_STATES and self.on_open:
                self._send(format_open(trade), level="INFO")
            elif state in CLOSE_STATES and self.on_close:
                r = trade.get("r_multiple")
                if self.min_abs_r and r is not None and abs(float(r)) < self.min_abs_r:
                    return
                self._send(format_close(trade), level="INFO")
        except Exception:       # a trade transition must never fail because of a notification
            log.exception("trade alert formatting failed; trade unaffected")

    def _send(self, text: str, level: str) -> None:
        try:
            self.notifier.send(text)
            self.sent += 1
        except Exception:
            log.exception("trade alert send failed; trade unaffected")


def attach(book, notifier, cfg) -> Optional[TradeAlerts]:
    """Wire alerts onto a TradeBook. Returns None when they are switched off or no notifier can deliver."""
    try:
        ta = getattr(cfg, "trade_alerts", None)
        if ta is None or not getattr(ta, "enabled", False):
            return None
        alerts = TradeAlerts(notifier, getattr(ta, "on_open", True), getattr(ta, "on_close", True),
                             getattr(ta, "min_abs_r", 0.0))
        book.on_transition = alerts.on_transition
        return alerts
    except Exception:
        log.exception("could not attach trade alerts; trading is unaffected")
        return None
