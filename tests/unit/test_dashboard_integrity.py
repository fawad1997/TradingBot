"""The dashboard shows the settings that were previously invisible, and the book as a whole.

Everything checked here exists because a setting that silently does nothing reads like a working
control: a straddle ceiling equal to the number of symbols, a research gate that permits whenever
evidence is missing, one evidence bar shared by eight rules that behave nothing alike, a commission of
zero that is right on one Exness account type and wrong on another.

The page is the place those become visible, so these tests assert the data reaches it - not that it
looks a particular way.
"""
import datetime as dt
import unittest
from datetime import datetime, timezone

from starlette.testclient import TestClient

from app.config import ConfigManager
from app.core.bridge import Bridge
from app.dashboard.api import create_app
from app.storage.repo import MemoryRepo

NOW = datetime.now(timezone.utc)


def seed_portfolio() -> MemoryRepo:
    """Two rules: one that loses, one that wins while the other is losing."""
    repo = MemoryRepo()
    i = 0
    for day in range(12):
        when = NOW - dt.timedelta(days=11 - day)
        i += 1
        repo.insert("trades", {"id": f"m{i}", "signal_id": None, "closed_at": when, "state": "CLOSED",
                               "symbol": "EURUSDm", "strategy_id": "micro_scalper", "direction": "BUY",
                               "r_multiple": -0.6})
        i += 1
        repo.insert("trades", {"id": f"p{i}", "signal_id": None,
                               "closed_at": when + dt.timedelta(hours=1), "state": "CLOSED",
                               "symbol": "XAUUSDm", "strategy_id": "pullback_v1", "direction": "SELL",
                               "r_multiple": 0.5})
    return repo


def client(repo=None):
    return TestClient(create_app(Bridge(), repo or MemoryRepo(), ConfigManager(), "tok"))


class TestIntegrityEndpoint(unittest.TestCase):
    def setUp(self):
        self.d = client().get("/api/integrity").json()

    def test_it_reports_researched_and_gated_separately(self):
        """Conflating these is what hid that seven of eight rules had no research at all."""
        r = self.d["research"]
        self.assertGreater(r["researched"], len(r["gated"]))
        self.assertIn("micro_scalper", r["gated"])
        self.assertTrue(r["record_only"])

    def test_it_says_what_happens_when_research_is_missing(self):
        self.assertIn("require_evidence", self.d["research"])
        self.assertIsInstance(self.d["research"]["require_evidence"], bool)

    def test_it_reports_each_rules_own_evidence_bar(self):
        th = self.d["research"]["thresholds"]
        self.assertGreaterEqual(len(th), 8)
        for sid, t in th.items():
            with self.subTest(sid):
                self.assertIn("min_trades", t)
                self.assertIn("block_below_r", t)
                self.assertIn("source", t)

    def test_it_flags_when_every_rule_shares_one_bar(self):
        self.assertTrue(self.d["research"]["all_on_global_bar"],
                        "with no overrides set this must report the shared-bar state, not hide it")

    def test_it_reports_whether_the_straddle_ceiling_binds(self):
        ns = self.d["news_straddle"]
        self.assertIn("ceiling_binds", ns)
        self.assertEqual(ns["ceiling_binds"], ns["max_armed_symbols"] < ns["symbols"] or not ns["symbols"])

    def test_it_reports_the_portfolio_risk_mode_and_its_ceilings(self):
        p = self.d["portfolio_risk"]
        self.assertIn(p["mode"], ("gross", "diversified"))
        self.assertGreaterEqual(p["max_gross_open_risk_fraction"], p["max_total_open_risk_fraction"])
        self.assertIn("stress_correlation", p)

    def test_it_reports_the_commission_setting(self):
        self.assertIn("commission_per_lot_round_trip", self.d["cost"])

    def test_it_carries_the_real_money_checks_with_reasons(self):
        checks = self.d["real_money_checks"]
        self.assertTrue(checks)
        for c in checks:
            with self.subTest(c["name"]):
                self.assertIn("pass", c)
                self.assertTrue(c["why"], "a check with no stated reason cannot be acted on")

    def test_the_checks_cover_all_three_known_gaps(self):
        names = " ".join(c["name"] for c in self.d["real_money_checks"]).lower()
        for needle in ("research gate", "ceiling", "commission"):
            self.assertIn(needle, names)


class TestPortfolioEndpoint(unittest.TestCase):
    def setUp(self):
        self.d = client(seed_portfolio()).get("/api/analysis/portfolio?hours=720").json()

    def test_total_net_r_is_reported(self):
        self.assertEqual(self.d["n_trades"], 24)
        self.assertAlmostEqual(self.d["total_net_r"], 24 * -0.05, places=6)

    def test_drawdown_depth_and_duration_are_reported(self):
        dd = self.d["drawdown"]
        self.assertIn("max_drawdown_r", dd)
        self.assertIn("longest_drawdown_trades", dd)

    def test_correlated_losses_are_reported(self):
        cl = self.d["correlated_losses"]
        self.assertEqual(cl["bucket"], "day")
        self.assertIn("together_rate", cl)

    def test_the_weekly_bucket_is_honoured(self):
        d = client(seed_portfolio()).get("/api/analysis/portfolio?hours=720&bucket=W").json()
        self.assertEqual(d["correlated_losses"]["bucket"], "week")

    def test_marginal_contribution_covers_every_rule(self):
        self.assertEqual(set(self.d["marginal"]), {"micro_scalper", "pullback_v1"})
        for sid, m in self.d["marginal"].items():
            with self.subTest(sid):
                self.assertIn("drawdown_delta", m)
                self.assertTrue(m["verdict"])

    def test_the_winning_rule_is_shown_as_adding_r(self):
        self.assertIn("adds R", self.d["marginal"]["pullback_v1"]["verdict"])
        self.assertIn("costs R", self.d["marginal"]["micro_scalper"]["verdict"])

    def test_an_empty_period_is_reported_not_crashed(self):
        d = client().get("/api/analysis/portfolio?hours=1").json()
        self.assertEqual(d["n_trades"], 0)


class TestResearchLabEndpointCarriesPerStrategy(unittest.TestCase):
    def test_the_per_strategy_block_is_present(self):
        d = client().get("/api/research/lab").json()
        self.assertIn("per_strategy", d)
        self.assertIn("verdict_counts", d)

    def test_each_strategys_research_is_evaluated_against_its_own_thresholds(self):
        repo = MemoryRepo()
        repo.set_flag("research_lab_latest", {
            "created_at": NOW.isoformat(),
            "symbols": {},
            "strategies": {"pullback_v1": {"EURUSDm": {
                "strategy_id": "pullback_v1", "verdict": "NOT_ROBUST_ON_TEST",
                "train": {"n_trades": 200, "expectancy_r": -0.9},
                "test": {"n_trades": 100, "expectancy_r": -1.2}}}},
        }, "research_lab")
        d = client(repo).get("/api/research/lab").json()
        entry = d["per_strategy"]["pullback_v1"]["EURUSDm"]
        self.assertTrue(entry["blocked"])
        self.assertEqual(entry["thresholds"]["strategy_id"], "pullback_v1")


class TestThePagesAskForIt(unittest.TestCase):
    """A renderer nothing calls is the same as no renderer."""

    def _static(self, name):
        from pathlib import Path
        return (Path(__file__).resolve().parents[2] / "app" / "dashboard" / "static" / name).read_text()

    def test_the_config_page_has_somewhere_to_put_it(self):
        html = self._static("config.html")
        self.assertIn('id="integrity"', html)
        self.assertIn('id="readiness"', html)

    def test_the_analysis_page_has_somewhere_to_put_the_book(self):
        html = self._static("analysis.html")
        for anchor in ('id="pfhead"', 'id="pftable"', 'id="pfco"', 'id="pfhours"', 'id="pfbucket"'):
            self.assertIn(anchor, html)

    def test_the_renderers_exist_and_are_wired_up(self):
        js = self._static("app.js")
        self.assertIn("async function loadIntegrity", js)
        self.assertIn("async function loadPortfolio", js)
        self.assertIn("loadIntegrity()", js)
        self.assertIn("loadPortfolio()", js)

    def test_the_renderers_fetch_the_endpoints_that_exist(self):
        js = self._static("app.js")
        self.assertIn('fetch("/api/integrity")', js)
        self.assertIn("/api/analysis/portfolio", js)


if __name__ == "__main__":
    unittest.main()
