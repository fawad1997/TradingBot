"""Each kind of message reaches the channel where someone would look for it.

One webhook meant a kill switch arrived between two routine fills. That is not a monitoring surface, it is a
log you stop reading - and an alert you stop reading is worse than none, because you believe you are covered.

The property that matters most here is not elegance of classification but that NOTHING IS EVER LOST: a
category with no webhook falls back to the default, a broken channel falls back to the default, and a
routing mistake of any kind still delivers the message somewhere.

The sharpest bug found while building this: "lost " as a keyword matched "Lost the connection to
MetaTrader" and filed a broker outage under #trades, the one place nobody would look for it. Trade messages
are now matched on the exact shape of their first line instead.
"""
import unittest

from app.notifications.base import Notifier
from app.notifications.routing import CATEGORIES, RoutedNotifier, build_routed, categorise


class Spy(Notifier):
    def __init__(self):
        self.got = []
        self.started = self.stopped = 0

    def alert(self, level, text, key=None):
        self.got.append(text)

    def send(self, text):
        self.got.append(text)

    def stop(self):
        self.stopped += 1


class Exploding(Notifier):
    def alert(self, *a, **k):
        raise RuntimeError("webhook down")

    def send(self, *a, **k):
        raise RuntimeError("webhook down")


class TestWhereEachMessageBelongs(unittest.TestCase):
    def test_a_broker_outage_is_health_not_trades(self):
        """The bug this test exists for: 'Lost the connection' is not a losing trade."""
        self.assertEqual(categorise("CRITICAL", "Lost the connection to MetaTrader. No new trades."), "health")

    def test_a_losing_trade_is_trades(self):
        self.assertEqual(categorise("INFO", "Lost 18.96 on XAUUSDm\nBought 0.07 at 4,138.88"), "trades")

    def test_a_winning_trade_is_trades(self):
        self.assertEqual(categorise("INFO", "Made 5.94 on XAUUSDm\nThe trailing stop caught it."), "trades")

    def test_an_opening_trade_is_trades_even_with_no_number_on_the_first_line(self):
        self.assertEqual(categorise("INFO", "Opened a buy on GBPUSDm\nSize 0.06, in at 1.32442"), "trades")
        self.assertEqual(categorise("INFO", "Opened a sell on EURUSDm\nSize 0.03"), "trades")

    def test_risk_events_are_risk(self):
        for m in ("The account has fallen far enough to trigger the emergency stop.",
                  "Emergency stop: daily loss limit reached",
                  "Margin level too low to add risk"):
            self.assertEqual(categorise("CRITICAL", m), "risk", m)

    def test_plumbing_is_health(self):
        for m in ("Could not save to the database, so new trades are stopped",
                  "Reconnected to MetaTrader.",
                  "Price feed has gone stale"):
            self.assertEqual(categorise("WARNING", m), "health", m)

    def test_research_results_are_research(self):
        self.assertEqual(categorise("WARNING", "Strategy micro_scalper RESTRICTED: expectancy negative"),
                         "research")

    def test_the_daily_summary_is_review(self):
        self.assertEqual(categorise("INFO", "Daily report: 12 trades, +7.09R"), "review")

    def test_an_urgent_message_matching_nothing_goes_somewhere_you_watch(self):
        self.assertEqual(categorise("CRITICAL", "something entirely unexpected happened"), "risk")

    def test_categorise_always_returns_a_real_category(self):
        for level in ("CRITICAL", "WARNING", "INFO", "", None):
            for text in ("", None, "x", "a" * 500, "\n\n\n"):
                self.assertIn(categorise(level, text), CATEGORIES)


class TestNothingIsEverLost(unittest.TestCase):
    def test_a_category_without_a_channel_uses_the_default(self):
        default, trades = Spy(), Spy()
        r = RoutedNotifier(default, {"trades": trades})
        r.alert("CRITICAL", "Lost the connection to MetaTrader.")     # health, which has no channel
        self.assertEqual(len(default.got), 1)
        self.assertEqual(len(trades.got), 0)

    def test_a_broken_channel_falls_back_rather_than_swallowing(self):
        default = Spy()
        r = RoutedNotifier(default, {"risk": Exploding()})
        r.alert("CRITICAL", "Emergency stop: daily loss limit reached")
        self.assertEqual(len(default.got), 1, "a dead webhook must not cost the message")

    def test_send_routes_too_and_also_falls_back(self):
        default, trades = Spy(), Spy()
        RoutedNotifier(default, {"trades": trades}).send("Made 5.94 on XAUUSDm")
        self.assertEqual(len(trades.got), 1)
        broken = Spy()
        RoutedNotifier(broken, {"trades": Exploding()}).send("Made 5.94 on XAUUSDm")
        self.assertEqual(len(broken.got), 1)

    def test_everything_dead_does_not_raise(self):
        """The last line of defence: the alert survives in the log, the caller never sees an exception."""
        RoutedNotifier(Exploding(), {"risk": Exploding()}).alert("CRITICAL", "margin level low")

    def test_stop_reaches_every_distinct_channel_once(self):
        default, a = Spy(), Spy()
        r = RoutedNotifier(default, {"trades": a, "risk": a})      # two categories, one notifier
        r.stop()
        self.assertEqual(default.stopped, 1)
        self.assertEqual(a.stopped, 1, "a shared notifier must be stopped once, not twice")


class TestSetUpOnlyWhenAsked(unittest.TestCase):
    def test_no_category_urls_means_no_routing_at_all(self):
        """An operator who wants one stream must keep exactly what they had."""
        default = Spy()
        secrets = type("S", (), {})()
        self.assertIs(build_routed("discord", secrets, 300, default), default)

    def test_the_secret_fields_exist_for_every_category(self):
        from app.config import Secrets
        for c in CATEGORIES:
            self.assertIn(f"notify_url_{c}", Secrets.model_fields, c)

    def test_the_env_template_documents_them(self):
        from pathlib import Path
        env = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
        for c in CATEGORIES:
            self.assertIn(f"NOTIFY_URL_{c.upper()}=", env, c)

    def test_routing_cannot_be_used_to_act_on_the_account(self):
        """A monitoring surface that could bypass the risk controls would be a liability, not a feature.

        Checked against CALLS, not prose - the module's own docstring says it cannot resume trading, and a
        naive substring search flagged that sentence as the violation it was describing.
        """
        import ast
        import inspect
        from app.notifications import routing
        forbidden = {"order_send", "close_position", "modify_sltp", "set_config", "resume",
                     "engage", "release", "close_all"}
        called = set()
        for node in ast.walk(ast.parse(inspect.getsource(routing))):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name:
                    called.add(name)
        self.assertEqual(called & forbidden, set(),
                         "the routing layer must be able to send messages and nothing else")


if __name__ == "__main__":
    unittest.main()
