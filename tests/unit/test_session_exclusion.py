"""Excluding a switched-off rule from the session and hour tables.

Why this exists: a session table is a weighted average of whatever was trading. On 19 Oct 2026 one rule
(micro_scalper) supplied 198 of 279 trades - 71% - and was then disabled. Everything those tables said
about "which hours work" was really a description of that one rule's habits, and acting on it after
switching the rule off would mean acting on a picture of something that no longer trades.

The filter turns a question needing a month of fresh data into one the existing record already answers.
"""
import datetime as dt
import unittest

from starlette.testclient import TestClient

from app.config import ConfigManager
from app.core.bridge import Bridge
from app.dashboard.analysis import hour_breakdown, session_breakdown
from app.dashboard.api import create_app
from app.storage.repo import MemoryRepo

NOW = dt.datetime(2026, 10, 19, tzinfo=dt.timezone.utc)


def tr(sid, hour, r, session="LONDON", symbol="X"):
    return {"strategy_id": sid, "symbol": symbol, "r_multiple": r, "session": session,
            "state": "CLOSED", "opened_at": NOW.replace(hour=hour).isoformat(),
            "closed_at": NOW.replace(hour=hour).isoformat()}


def record():
    """One hour dominated by a bad rule, alongside two good ones."""
    rows = [tr("micro_scalper", 9, -1.45) for _ in range(22)]
    rows += [tr("htf_continuation_v1", 9, 0.55), tr("vwap_reversion_v1", 9, 0.40)]
    rows += [tr("micro_scalper", 13, -0.60) for _ in range(30)]
    rows += [tr("htf_continuation_v1", 13, 0.45) for _ in range(16)]
    return rows


class TestHourBreakdown(unittest.TestCase):
    def _hours(self, excl=None):
        return {h["hour_utc"]: h for h in hour_breakdown(record(), None, excl)}

    def test_without_the_filter_the_bad_rule_dominates(self):
        self.assertLess(self._hours()[9]["mean_r"], -1.0)

    def test_excluding_it_changes_the_verdict_on_that_hour(self):
        """The finding this whole filter exists for."""
        self.assertGreater(self._hours(["micro_scalper"])[9]["mean_r"], 0)

    def test_the_excluded_rules_trades_are_gone_from_the_counts(self):
        self.assertEqual(self._hours()[9]["n"], 24)
        self.assertEqual(self._hours(["micro_scalper"])[9]["n"], 2)

    def test_other_rules_are_untouched(self):
        self.assertEqual(self._hours(["micro_scalper"])[13]["n"], 16)

    def test_no_filter_and_an_empty_filter_agree(self):
        self.assertEqual(self._hours(), self._hours([]))

    def test_excluding_everything_leaves_nothing_rather_than_crashing(self):
        out = hour_breakdown(record(), None,
                             ["micro_scalper", "htf_continuation_v1", "vwap_reversion_v1"])
        self.assertEqual(out, [])


class TestSessionBreakdown(unittest.TestCase):
    def _sessions(self, excl=None):
        d = session_breakdown(record(), {}, None, excl)
        return {s["session"]: s for s in d["sessions"]}

    def test_the_session_verdict_flips_too(self):
        self.assertLess(self._sessions()["LONDON"]["mean_r"], 0)
        self.assertGreater(self._sessions(["micro_scalper"])["LONDON"]["mean_r"], 0)

    def test_the_symbol_filter_still_works_alongside_it(self):
        rows = record() + [tr("htf_continuation_v1", 9, 0.9, symbol="OTHER")]
        d = session_breakdown(rows, {}, "OTHER", ["micro_scalper"])
        self.assertEqual(sum(s["n"] for s in d["sessions"]), 1)

    def test_an_unknown_strategy_name_excludes_nothing(self):
        self.assertEqual(self._sessions(["not_a_real_rule"])["LONDON"]["n"],
                         self._sessions()["LONDON"]["n"])


class TestEndpoint(unittest.TestCase):
    def _client(self):
        repo = MemoryRepo()
        for i, t in enumerate(record()):
            repo.insert("trades", {"id": f"t{i}", "signal_id": None, **t})
        return TestClient(create_app(Bridge(), repo, ConfigManager(), "tok"))

    def test_it_reports_which_rules_are_switched_off(self):
        """The page needs this to offer the checkbox at all."""
        d = self._client().get("/api/analysis/sessions?hours=99999").json()
        self.assertIn("disabled_strategies", d)
        self.assertIn("micro_scalper", d["disabled_strategies"],
                      "micro_scalper was disabled on 19 Oct and the page must be able to say so")

    def test_the_exclude_parameter_is_honoured(self):
        c = self._client()
        base = c.get("/api/analysis/sessions?hours=99999").json()
        cut = c.get("/api/analysis/sessions?hours=99999&exclude=micro_scalper").json()
        self.assertEqual(cut["excluded"], ["micro_scalper"])
        self.assertLess(sum(s["n"] for s in cut["sessions"]),
                        sum(s["n"] for s in base["sessions"]))

    def test_several_rules_can_be_excluded_at_once(self):
        d = self._client().get(
            "/api/analysis/sessions?hours=99999&exclude=micro_scalper,vwap_reversion_v1").json()
        self.assertEqual(d["excluded"], ["micro_scalper", "vwap_reversion_v1"])

    def test_the_hours_block_is_filtered_the_same_way(self):
        d = self._client().get("/api/analysis/sessions?hours=99999&exclude=micro_scalper").json()
        nine = next((h for h in d["hours"] if h["hour_utc"] == 9), None)
        self.assertIsNotNone(nine)
        self.assertEqual(nine["n"], 2)


class TestThePageAsksForIt(unittest.TestCase):
    def _static(self, name):
        from pathlib import Path
        return (Path(__file__).resolve().parents[2] / "app" / "dashboard" / "static" / name).read_text(encoding="utf-8")

    def test_the_checkbox_exists(self):
        html = self._static("analysis.html")
        self.assertIn('id="sessexcl"', html)
        self.assertIn('id="sessexclwrap"', html)

    def test_it_is_hidden_until_something_is_actually_disabled(self):
        self.assertIn('id="sessexclwrap" hidden', self._static("analysis.html"))

    def test_the_renderer_sends_it_and_reloads_on_change(self):
        js = self._static("app.js")
        self.assertIn("&exclude=", js)
        self.assertIn('$("#sessexcl").onchange = loadSessions', js)

    def test_the_page_warns_when_switched_off_rules_are_included(self):
        self.assertIn("describe what used to trade", self._static("app.js"))


if __name__ == "__main__":
    unittest.main()
