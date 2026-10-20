"""The pre-session check and the post-session review.

Both read only. That is the property worth defending: a routine that quietly retuned production while
reporting on it would turn every morning check into an unlogged configuration change, and the audit trail
would stop meaning anything.

The readiness check asks the LIVE system for the same state the dashboard renders, so it cannot disagree
with the screen. It distinguishes things that stop a trade (blocking) from things merely worth knowing -
a wide spread right now is not a reason to say the bot is broken.
"""
import unittest
from datetime import datetime, timezone

from app.reports_session import readiness, review, review_message


def healthy_state(**over):
    s = {"components": {"MT5": "HEALTHY", "DATA": "HEALTHY", "DATABASE": "HEALTHY", "EXECUTION": "HEALTHY"},
         "account": {"balance": 1184.22},
         "kill_switch": {"engaged": False, "volatile": []},
         "risk": {"state": "NORMAL", "daily_loss_remaining": 59.2},
         "symbol_trading": {f"s{i}": {"enabled": True} for i in range(13)},
         "symbol_market_status": {f"s{i}": {"status": "OPEN"} for i in range(13)},
         "market": {"spread_points": 20, "spread_limit_points": 300}, "symbol": "XAUUSDm"}
    s.update(over)
    return s


class TestReadiness(unittest.TestCase):
    def test_a_healthy_system_is_ready(self):
        d = readiness(healthy_state())
        self.assertTrue(d["ready"])
        self.assertEqual(d["blockers"], [])
        self.assertEqual(d["summary"], "Ready to trade.")

    def test_a_broken_link_blocks(self):
        d = readiness(healthy_state(components={"MT5": "DOWN", "DATA": "HEALTHY",
                                                "DATABASE": "HEALTHY", "EXECUTION": "HEALTHY"}))
        self.assertFalse(d["ready"])
        self.assertIn("MetaTrader link", d["blockers"])

    def test_the_emergency_stop_blocks_and_says_why(self):
        d = readiness(healthy_state(kill_switch={"engaged": True, "reason": "manual halt"}))
        self.assertFalse(d["ready"])
        self.assertIn("manual halt", d["summary"])

    def test_a_spent_daily_budget_blocks(self):
        d = readiness(healthy_state(risk={"state": "NORMAL", "daily_loss_remaining": 0.0}))
        self.assertFalse(d["ready"])
        self.assertIn("Daily loss budget", d["blockers"])

    def test_reduced_risk_is_noted_but_does_not_block(self):
        """Trading at half size is the system working as designed, not a fault."""
        d = readiness(healthy_state(risk={"state": "REDUCED", "daily_loss_remaining": 59.2}))
        self.assertTrue(d["ready"])
        self.assertIn("Risk state", d["warnings"])

    def test_a_wide_spread_right_now_is_a_warning_not_a_blocker(self):
        d = readiness(healthy_state(market={"spread_points": 340, "spread_limit_points": 300}))
        self.assertTrue(d["ready"], "a momentarily wide spread is not a broken bot")
        self.assertIn("Buy/sell gap", d["warnings"])

    def test_a_stale_news_calendar_is_a_warning(self):
        d = readiness(healthy_state(), calendar_age_hours=100)
        self.assertTrue(d["ready"])
        self.assertIn("News calendar", d["warnings"])

    def test_every_symbol_stopped_blocks(self):
        d = readiness(healthy_state(symbol_trading={f"s{i}": {"enabled": False} for i in range(13)}))
        self.assertFalse(d["ready"])
        self.assertIn("Symbols running", d["blockers"])

    def test_an_empty_state_does_not_raise(self):
        d = readiness({})
        self.assertIn("ready", d)
        self.assertFalse(d["ready"])


class Repo:
    def __init__(self, trades=None, execs=None, rejections=None):
        self.data = {"trades": trades or [], "execution_events": execs or [],
                     "rejections": rejections or []}

    def select(self, table, where=None, order_by=None, desc=False, limit=None):
        return self.data.get(table, [])


NOW = datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc)
T = "2026-10-16T18:00:00+00:00"


class TestReview(unittest.TestCase):
    def test_an_empty_day_says_so_plainly(self):
        d = review(Repo(), NOW, hours=8)
        self.assertEqual(d["trades"], 0)
        self.assertIn("no trades finished", review_message(d))

    def test_it_totals_r_and_money(self):
        trades = [{"closed_at": T, "r_multiple": 1.5, "net_pnl": 15.11, "session": "LONDON"},
                  {"closed_at": T, "r_multiple": -1.0, "net_pnl": -10.10, "session": "LONDON"}]
        d = review(Repo(trades), NOW, hours=24)
        self.assertEqual(d["trades"], 2)
        self.assertAlmostEqual(d["total_r"], 0.5, places=2)
        self.assertAlmostEqual(d["money"], 5.01, places=2)
        self.assertAlmostEqual(d["win_rate"], 0.5, places=3)

    def test_our_own_waits_are_counted_separately_from_broker_refusals(self):
        """These used to be indistinguishable: both reported as 10024 THROTTLED."""
        execs = [{"created_at": T, "event_type": "SL_CHANGE", "retcode": 90024, "retcode_text": "OWN_COOLDOWN"},
                 {"created_at": T, "event_type": "SL_CHANGE", "retcode": 90024, "retcode_text": "OWN_COOLDOWN"},
                 {"created_at": T, "event_type": "SL_CHANGE", "retcode": 10016, "retcode_text": "INVALID_STOPS"},
                 {"created_at": T, "event_type": "FILL", "retcode": 10009, "retcode_text": "DONE",
                  "slippage_points": 4.0}]
        d = review(Repo([], execs), NOW, hours=24)
        self.assertEqual(d["stop_moves_we_spaced_out"], 2)
        self.assertEqual(d["orders_refused_by_broker"], 1, "a successful fill is not a refusal")
        self.assertAlmostEqual(d["avg_slippage_points"], 4.0, places=2)

    def test_a_trade_that_gave_back_a_lead_is_surfaced(self):
        """The single most useful thing to look at by eye after a session."""
        trades = [{"closed_at": T, "r_multiple": -1.0, "net_pnl": -18.96, "mfe_r": 3.65,
                   "symbol": "XAUUSDm", "strategy_id": "micro_scalper", "position_id": 1},
                  {"closed_at": T, "r_multiple": -1.0, "net_pnl": -10.0, "mfe_r": 0.04,
                   "symbol": "EURUSDm", "strategy_id": "pullback_v1", "position_id": 2}]
        d = review(Repo(trades), NOW, hours=24)
        self.assertEqual(len(d["gave_back_a_lead"]), 1, "only the one that was genuinely ahead")
        self.assertEqual(d["gave_back_a_lead"][0]["symbol"], "XAUUSDm")
        self.assertIn("reached +3.65R", review_message(d))

    def test_it_groups_by_session(self):
        trades = [{"closed_at": T, "r_multiple": 1.5, "net_pnl": 15.0, "session": "NEW_YORK"},
                  {"closed_at": T, "r_multiple": 0.5, "net_pnl": 5.0, "session": "LONDON"},
                  {"closed_at": T, "r_multiple": -0.5, "net_pnl": -5.0, "session": "LONDON"}]
        d = review(Repo(trades), NOW, hours=24)
        self.assertEqual(d["by_session"]["NEW_YORK"]["n"], 1)
        self.assertEqual(d["by_session"]["LONDON"]["n"], 2)
        self.assertAlmostEqual(d["by_session"]["LONDON"]["mean_r"], 0.0, places=3)

    def test_anything_outside_the_window_is_excluded(self):
        trades = [{"closed_at": "2026-10-01T00:00:00+00:00", "r_multiple": 5.0, "net_pnl": 50.0}]
        self.assertEqual(review(Repo(trades), NOW, hours=8)["trades"], 0)

    def test_an_unreadable_table_does_not_bring_the_review_down(self):
        class Broken(Repo):
            def select(self, *a, **k):
                raise RuntimeError("database down")
        d = review(Broken(), NOW, hours=8)
        self.assertEqual(d["trades"], 0)


class TestItOnlyEverReads(unittest.TestCase):
    def test_the_module_cannot_change_anything(self):
        """A review that could retune production would make the audit trail meaningless."""
        import ast
        import inspect
        from app import reports_session
        forbidden = {"update", "set_flag", "insert", "upsert", "execute", "commit", "order_send",
                     "close_position", "modify_sltp", "set_config"}
        called = set()
        for node in ast.walk(ast.parse(inspect.getsource(reports_session))):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name:
                    called.add(name)
        self.assertEqual(called & forbidden, set(), "the session routine must read and nothing else")

    def test_the_cli_exposes_both_halves(self):
        import inspect
        from app import main
        src = inspect.getsource(main)
        self.assertIn('"session": cmd_session', src)
        self.assertIn('choices=["check", "review"]', src)


if __name__ == "__main__":
    unittest.main()
