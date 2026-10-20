"""Send each kind of message to its own Discord channel.

One webhook meant everything landed in one stream: a kill switch beside a routine fill beside a research
result. That is not a monitoring surface, it is a log you stop reading - and an alert you stop reading is
worse than no alert, because you believe you are covered.

Five categories, each with its own optional webhook:

  health      MetaTrader, database, process - "is it alive"
  research    experiment results, promotion decisions, the research lab
  trades      fills and closures, the ordinary flow of the day
  risk        refused orders, risk limits, execution anomalies - things to act on
  review      the daily summary

A category with no webhook of its own falls back to NOTIFY_URL, so configuring none behaves exactly as
before and configuring one splits only that stream out. Nothing is ever dropped for want of a URL.

This is a routing layer and nothing else. It cannot send an order, change a limit or resume trading; the
dashboard token guards all of that. A monitoring surface that could bypass the risk controls would be a
liability dressed as a convenience.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, Optional

from app.notifications.base import Notifier

log = logging.getLogger(__name__)

CATEGORIES = ("health", "research", "trades", "risk", "review")

# Which category a message belongs to, worked out from the alert level and the words in it. The system only
# ever had "CRITICAL / WARNING / INFO", so the category has to be inferred rather than passed - inference is
# imperfect, which is why the fallback is "put it somewhere" rather than "drop it".
RISK_WORDS = ("risk", "margin", "drawdown", "loss limit", "kill switch", "halted", "rejected", "refused",
              "circuit breaker", "exposure", "stopped out", "emergency")
HEALTH_WORDS = ("metatrader", "mt5", "broker", "database", "connection", "disconnect", "reconnect",
                "startup", "shutting down", "stale", "feed", "latency")
RESEARCH_WORDS = ("research", "backtest", "walk-forward", "experiment", "promoted", "strategy", "lab",
                  "learned filter", "model")
# Trade messages are recognised by the exact SHAPE of their first line, not by loose keywords. "lost " as a
# keyword matched "Lost the connection to MetaTrader" and filed a broker outage under #trades - the one
# category where nobody would think to look for it. These two patterns are precisely what format_open and
# format_close emit, so a message either is a trade report or it is not; nothing is inferred.
TRADE_FIRST_LINE = re.compile(
    r"^(opened a (buy|sell) on \S+"            # "Opened a buy on GBPUSDm"
    r"|(made|lost) [\d.,]+ on \S+"            # "Lost 18.96 on XAUUSDm"
    r"|closed a (buy|sell) on \S+)",           # "Closed a sell on EURUSDm"
    re.I)
REVIEW_WORDS = ("daily report", "daily summary", "session summary", "end of day")


def _looks_like_a_trade(text: str) -> bool:
    body = (text or "").strip()
    if not body:
        return False
    return bool(TRADE_FIRST_LINE.match(body.splitlines()[0].strip()))


def categorise(level: str, text: str) -> str:
    """Best guess at where a message belongs. Never raises, always returns a real category."""
    t = (text or "").lower()
    lvl = (level or "").upper()
    # Order matters, and it is deliberate: the categories you must not miss are tested first. A message
    # wrongly filed under #trades is a message nobody reads.
    if any(w in t for w in REVIEW_WORDS):
        return "review"
    if _looks_like_a_trade(text):
        return "trades"
    if any(w in t for w in HEALTH_WORDS):
        return "health"
    if any(w in t for w in RISK_WORDS):
        return "risk"
    if any(w in t for w in RESEARCH_WORDS):
        return "research"
    # anything urgent that matched nothing is worth seeing in the channel you actually watch
    return "risk" if lvl == "CRITICAL" else "health"


class RoutedNotifier(Notifier):
    """Fans messages out to per-category notifiers, falling back to a default for anything unconfigured."""

    def __init__(self, default: Notifier, by_category: Dict[str, Notifier] = None):
        self.default = default or Notifier()
        self.by_category = {k: v for k, v in (by_category or {}).items() if v is not None}
        self.sent: Dict[str, int] = {}

    def _pick(self, category: str) -> Notifier:
        return self.by_category.get(category, self.default)

    def alert(self, level: str, text: str, key: Optional[str] = None) -> None:
        try:
            category = categorise(level, text)
            self.sent[category] = self.sent.get(category, 0) + 1
            self._pick(category).alert(level, text, key)
        except Exception:      # a routing mistake must never cost the message
            log.exception("alert routing failed; sending to the default channel")
            try:
                self.default.alert(level, text, key)
            except Exception:
                log.exception("default channel failed too; the alert survives only in the log")

    def send(self, text: str) -> None:
        try:
            category = categorise("INFO", text)
            self.sent[category] = self.sent.get(category, 0) + 1
            self._pick(category).send(text)
        except Exception:
            log.exception("message routing failed; sending to the default channel")
            try:
                self.default.send(text)
            except Exception:
                log.exception("default channel failed too; the message survives only in the log")

    def start(self) -> None:
        for n in {id(x): x for x in [self.default, *self.by_category.values()]}.values():
            if hasattr(n, "start") and hasattr(n, "daemon"):
                try:
                    n.start()
                except RuntimeError:
                    pass        # already started: several categories may share one notifier
        return None

    def stop(self) -> None:
        for n in {id(x): x for x in [self.default, *self.by_category.values()]}.values():
            try:
                n.stop()
            except Exception:
                log.debug("could not stop a notifier cleanly", exc_info=True)


def build_routed(channel: str, secrets, min_interval: int, default: Notifier) -> Notifier:
    """Wrap `default` in per-category routing, using NOTIFY_URL_<CATEGORY> wherever one is set.

    Returns `default` unchanged when no category URL is configured, so an operator who wants one stream keeps
    exactly what they had.
    """
    from app.notifications.channels import build
    try:
        per: Dict[str, Notifier] = {}
        for cat in CATEGORIES:
            url = getattr(secrets, f"notify_url_{cat}", None)
            if not url:
                continue
            made = build(channel, type("S", (), {"notify_url": url})(), min_interval)
            if made is not None:
                per[cat] = made
        if not per:
            return default
        log.info("alerts split across %d channel(s): %s", len(per), ", ".join(sorted(per)))
        return RoutedNotifier(default, per)
    except Exception:
        log.exception("could not set up channel routing; keeping the single channel")
        return default
