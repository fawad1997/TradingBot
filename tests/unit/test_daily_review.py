"""Daily review report and backup retention: the records the owner reads every morning."""
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.storage.repo import MemoryRepo
from tools.backup_db import prune
from tools.daily_review import build, days_to_write, render

DAY = date(2026, 10, 21)
T0 = datetime(2026, 10, 21, tzinfo=timezone.utc)


def at(hours):
    return T0 + timedelta(hours=hours)


def seeded():
    repo = MemoryRepo()
    for h, eq in ((0.1, 500.0), (12, 503.0), (23.9, 505.5)):
        repo.insert("account_snapshots", {"created_at": at(h), "environment": "DEMO", "balance": eq, "equity": eq})
    for sid, sym, r, h in (("pullback_v1", "XAUUSDm", 1.5, 3), ("pullback_v1", "XAUUSDm", -1.0, 5),
                           ("vwap_reversion_v1", "EURUSDm", 0.5, 9)):
        repo.insert("trades", {"id": f"t{h}", "strategy_id": sid, "symbol": sym, "state": "CLOSED", "r_multiple": r,
                               "net_pnl": r * 2.5, "closed_at": at(h), "environment": "DEMO", "direction": "BUY"})
    repo.insert("trades", {"id": "paper", "strategy_id": "pullback_v1", "symbol": "XAUUSDm", "state": "CLOSED",
                           "r_multiple": -5.0, "closed_at": at(4), "environment": "DRY_RUN"})
    repo.insert("trades", {"id": "open", "strategy_id": "vwap_reversion_v1", "symbol": "GBPUSDm", "state": "OPEN",
                           "environment": "DEMO", "direction": "SELL", "entry_price": 1.3})
    for i in range(1, 6):
        repo.insert("signals", {"id": f"s{i}", "created_at": at(i), "environment": "DEMO", "status": "REJECTED",
                                "strategy_id": "pullback_v1"})
    repo.insert("signals", {"id": "s6", "created_at": at(6), "environment": "DRY_RUN", "status": "REJECTED"})
    for sid, gate, why in (("s1", "SIZE", "POSITION_SIZE_INFEASIBLE"), ("s2", "SIZE", "POSITION_SIZE_INFEASIBLE"),
                           ("s3", "SIZE", "POSITION_SIZE_INFEASIBLE"), ("s4", "COST", "COST_TOO_HIGH"),
                           ("s6", "SIZE", "POSITION_SIZE_INFEASIBLE")):
        repo.insert("rejections", {"created_at": at(int(sid[1]) + 0.1), "signal_id": sid, "gate": gate, "reason": why})
    for sid, r in (("s1", -1.0), ("s2", 2.0)):
        repo.insert("counterfactuals", {"id": f"c{sid}", "signal_id": sid, "status": "RESOLVED", "gate": "SIZE",
                                        "strategy_id": "pullback_v1", "hypothetical_r": r, "resolved_at": at(20)})
    repo.set_flag("selector:XAUUSDm", {"allocations": {"pullback_v1": {
        "strategy_id": "pullback_v1", "symbol": "XAUUSDm", "state": "BENCHED", "multiplier": 0.0,
        "posterior_r": -0.4, "reason": "negative after costs", "vetoed_regimes": []}}}, "test")
    repo.insert("selector_history", {"created_at": at(7), "symbol": "XAUUSDm", "strategy_id": "pullback_v1",
                                     "previous_state": "UNPROVEN", "state": "BENCHED", "reason": "negative",
                                     "applied": True})
    repo.insert("config_versions", {"created_at": at(8), "actor": "dashboard", "action": "KILL_SWITCH_RELEASE",
                                    "reason": "test resume"})
    return repo


def log_file():
    local = lambda h: at(h).astimezone().strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731  (the log is local time)
    lines = [f"{local(2)},100 CRITICAL app.notifications.base: ALERT[CRITICAL] Lost the connection to MetaTrader.",
             f"{local(14)},100 CRITICAL app.notifications.base: ALERT[CRITICAL] Lost the connection to MetaTrader.",
             f"{local(3)},100 WARNING app: ALERT[WARNING] spread 41.5 points on EURUSDm",
             f"{local(4)},100 WARNING app: ALERT[WARNING] spread 38 points on EURUSDm",
             f"{local(5)},100 INFO app: ALERT[INFO] Trading core ACTIVE - DEMO on XAUUSDm",
             f"{local(30)},100 CRITICAL app: ALERT[CRITICAL] the next day"]
    path = Path(tempfile.mkdtemp(prefix="qlog_")) / "quantara.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = build(seeded(), DAY, "DEMO", now=at(30), log_paths=[log_file()])

    def test_trades_are_the_days_account_trades_only(self):
        t = self.d["trades"]
        self.assertEqual(t["summary"]["n"], 3, "the DRY_RUN trade belongs to another environment")
        self.assertAlmostEqual(t["summary"]["total_r"], 1.0)
        self.assertEqual(set(t["by_strategy"]), {"pullback_v1", "vwap_reversion_v1"})
        self.assertEqual(len(self.d["open_positions"]), 1)

    def test_account_change_over_the_day(self):
        self.assertAlmostEqual(self.d["account"]["equity_change"], 5.5)
        self.assertFalse(self.d["partial"])

    def test_refusals_are_counted_by_gate_and_flagged_when_one_dominates(self):
        r = self.d["rejections"]
        self.assertEqual((r["total"], r["by_gate"]), (4, {"SIZE": 3, "COST": 1}))
        self.assertTrue(any("3 of 4 refused signals (75%) stopped at SIZE" in x for x in self.d["look_at"]))

    def test_paper_results_selector_and_operator_actions(self):
        self.assertEqual(self.d["shadow"]["summary"]["n"], 2)
        self.assertEqual(len(self.d["selector"]["benched"]), 1)
        self.assertEqual(self.d["selector"]["changes"][0]["state"], "BENCHED")
        self.assertTrue(any("KILL_SWITCH_RELEASE" in x for x in self.d["look_at"]))

    def test_alerts_are_grouped_with_numbers_masked(self):
        a = self.d["alerts"]
        self.assertEqual(a["CRITICAL: Lost the connection to MetaTrader."], 2)
        self.assertEqual(a["WARNING: spread # points on EURUSDm"], 2)
        self.assertEqual(sum(1 for k in a if k.startswith("INFO")), 1, "INFO alerts are only counted")
        self.assertTrue(any("dropped 2 time(s)" in x for x in self.d["look_at"]))

    def test_render(self):
        md = render(self.d)
        for heading in ("## Look at", "## Trades closed", "## Signals and why they were refused",
                        "## Strategy selector", "## Record so far (all days)"):
            self.assertIn(heading, md)
        self.assertIn("| pullback_v1 | 2 |", md)

    def test_a_day_without_the_bot_says_so(self):
        d = build(MemoryRepo(), DAY, "DEMO", now=at(30), log_paths=[])
        self.assertTrue(any("not running" in x for x in d["look_at"]))
        self.assertIn("No account snapshots", render(d))


class TestDepositsAndEvidence(unittest.TestCase):
    """The demo was topped up from $500 to $5,000 on 20 Oct 2026: that must read as a deposit, not as profit."""

    def _repo(self, start, end, trade_pnl=None):
        repo = MemoryRepo()
        for h, bal in ((0.1, start), (23.9, end)):
            repo.insert("account_snapshots", {"created_at": at(h), "environment": "DEMO", "balance": bal,
                                              "equity": bal})
        if trade_pnl is not None:
            repo.insert("trades", {"id": "t1", "strategy_id": "pullback_v1", "symbol": "EURUSDm", "state": "CLOSED",
                                   "r_multiple": 1.0, "net_pnl": trade_pnl, "closed_at": at(5),
                                   "environment": "DEMO", "direction": "BUY"})
        return repo

    def test_a_demo_top_up_is_reported_as_a_deposit(self):
        d = build(self._repo(500.0, 5000.0), DAY, "DEMO", now=at(30), log_paths=[])
        self.assertAlmostEqual(d["account"]["deposit"], 4500.0)
        self.assertTrue(any("not trading profit" in x for x in d["look_at"]))
        self.assertIn("**Deposit / top-up +4500.00**", render(d))

    def test_a_balance_change_the_trades_explain_is_not_a_deposit(self):
        d = build(self._repo(5000.0, 5012.5, trade_pnl=12.5), DAY, "DEMO", now=at(30), log_paths=[])
        self.assertIsNone(d["account"]["deposit"])
        self.assertNotIn("Deposit / top-up", render(d))

    def test_the_lab_grades_are_summarised_and_rendered(self):
        repo = self._repo(5000.0, 5000.0)
        st = lambda g, lo: {"grade": g, "n": 80, "mean": lo + 0.1, "lo": lo, "hi": lo + 0.2, "sr": 0.1, "dsr": 0.4,  # noqa: E731
                            "pbo": 0.5, "random_p": 0.3, "edge_vs_random": 0.01,
                            "exits": {"bracket": 0.05, "as_traded": -0.02}}
        repo.set_flag("research_lab_latest", {
            "created_at": "2026-10-21T01:00:00+00:00",
            "evidence": {"n_trials": 3, "benchmark_sr": 0.12, "grades": {"NO_EDGE_SHOWN": 3}},
            "symbols": {"EURUSDm": {"stats": st("NO_EDGE_SHOWN", -0.1),
                                    "hypotheses": {"vwap_reversion": {"M15": {"stats": st("NO_EDGE_SHOWN", 0.02)}}},
                                    "trend_daily": {"stats": st("NO_EDGE_SHOWN", -0.05), "avg_swap_r": 0.03}}},
            "strategies": {"pullback_v1": {"EURUSDm": {"verdict": "NOT_ROBUST_ON_TEST", "stats": st("NEGATIVE", -0.4)}}},
            "trend_daily": {"symbols": 1, "n_trades": 40, "ci": {"mean": 0.08, "lo": -0.01, "hi": 0.2},
                            "dsr": 0.94, "random": {"edge_vs_random": 0.13, "p_value": 0.005},
                            "grade": "NO_EDGE_SHOWN"}}, "test")
        d = build(repo, DAY, "DEMO", now=at(30), log_paths=[])
        ev = d["research"]["evidence"]
        self.assertEqual(ev["cells"], 2)
        self.assertEqual(ev["best"][0]["rule"], "vwap_reversion")          # ranked by the bottom of its range
        self.assertAlmostEqual(ev["exit_effect_r"], -0.07)
        self.assertTrue(any("none of 2 rule cells shows an edge" in x for x in d["look_at"]))
        md = render(d)
        self.assertIn("## Research evidence", md)
        self.assertIn("| EURUSDm | vwap_reversion | M15 |", md)
        self.assertIn("Daily trend following (decade replay", md)
        self.assertIn("| pullback_v1 | 1 | 0 | 1 |", md)


class TestSchedule(unittest.TestCase):
    def test_yesterday_plus_missing_days_since_the_first_report(self):
        today = date(2026, 10, 22)
        self.assertEqual(days_to_write(today, [], 7), [date(2026, 10, 21)])
        self.assertEqual(days_to_write(today, [date(2026, 10, 18), date(2026, 10, 20)], 7),
                         [date(2026, 10, 19), date(2026, 10, 21)])


class TestBackupRetention(unittest.TestCase):
    def test_keeps_the_newest_and_the_first_of_each_month(self):
        d = Path(tempfile.mkdtemp(prefix="bk_"))
        names = [f"quantara_2026-09-{i:02d}_0015.dump" for i in range(1, 31)] + \
                [f"quantara_2026-10-{i:02d}_0015.dump" for i in range(1, 21)]
        for n in names:
            (d / n).write_bytes(b"x")
        removed = prune(d, keep_daily=3)
        left = sorted(p.name for p in d.glob("*.dump"))
        self.assertEqual(left, ["quantara_2026-09-01_0015.dump", "quantara_2026-10-01_0015.dump",
                                "quantara_2026-10-18_0015.dump", "quantara_2026-10-19_0015.dump",
                                "quantara_2026-10-20_0015.dump"])
        self.assertEqual(len(removed), len(names) - 5)


if __name__ == "__main__":
    unittest.main()
