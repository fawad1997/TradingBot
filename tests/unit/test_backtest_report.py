"""The replay report: totals, drawdown, streaks and months computed right, pooled = sum of its parts, the true
history start shown per symbol, and the daily review / dashboard reading the same file."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from app.research.lab import replay

DAY = 86400


def ts(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


def tr(t, r, sym="EURUSDm", side="BUY"):
    return {"sym": sym, "t": t, "x": t + 3600, "s": side, "r": r, "why": "TP" if r > 0 else "SL"}


#  +2, -1, -1, -1, +3, -1  ->  total +1, peak 2 then down to -1 (drawdown 3), worst run of losses 3
SEQ = [2.0, -1.0, -1.0, -1.0, 3.0, -1.0]
JAN = [tr(ts(2026, 1, 5) + i * DAY, r) for i, r in enumerate(SEQ)]


class TestSummary(unittest.TestCase):
    def test_totals_drawdown_and_streak(self):
        s = replay.summarise(JAN)
        self.assertEqual(s["n"], 6)
        self.assertAlmostEqual(s["total_r"], 1.0)
        self.assertAlmostEqual(s["max_drawdown_r"], 3.0)
        self.assertEqual(s["worst_losing_streak"], 3)
        self.assertAlmostEqual(s["win_rate"], 2 / 6, places=3)
        self.assertAlmostEqual(s["profit_factor"], 5.0 / 4.0, places=3)
        self.assertEqual(s["best_trade_r"], 3.0)
        self.assertEqual(s["worst_trade_r"], -1.0)
        self.assertEqual(s["curve"][-1][1], 1.0)

    def test_months_and_quarters(self):
        feb = [tr(ts(2026, 2, 10), -2.0), tr(ts(2026, 4, 2), 0.5)]
        s = replay.summarise(JAN + feb)
        self.assertEqual(s["worst_month"], ["2026-02", -2.0])
        self.assertEqual(s["best_month"], ["2026-01", 1.0])
        self.assertEqual(s["losing_months"], 1)
        self.assertEqual(s["by_quarter"], {"2026-Q1": -1.0, "2026-Q2": 0.5})

    def test_order_is_by_exit_time_not_input_order(self):
        self.assertEqual(replay.summarise(list(reversed(JAN)))["max_drawdown_r"], 3.0)

    def test_unseen_stretch_and_money(self):
        split = JAN[3]["t"]
        s = replay.summarise(JAN, split_time=split, risk_money=50.0)
        self.assertEqual(s["unseen"]["n"], 3)
        self.assertAlmostEqual(s["unseen"]["total_r"], 1.0)
        self.assertEqual(s["money"], {"risk_per_trade": 50.0, "total": 50.0, "max_drawdown": 150.0})

    def test_empty(self):
        self.assertEqual(replay.summarise([]), {"n": 0})

    def test_curve_is_thinned_but_keeps_the_extremes(self):
        many = [tr(ts(2025, 1, 1) + i * 3600, (-1.0 if i < 700 else 1.0)) for i in range(1500)]
        c = replay.summarise(many)["curve"]
        self.assertLessEqual(len(c), replay.CURVE_POINTS + 3)
        self.assertIn(-700.0, [p[1] for p in c])
        self.assertEqual(c[-1][1], 100.0)


class TestBuild(unittest.TestCase):
    def cells(self):
        gbp = [tr(ts(2026, 3, 1) + i * DAY, r, "GBPUSDm") for i, r in enumerate([1.5, -1.0, 0.5])]
        return {"pullback_v1": {
            "EURUSDm": {"trades": JAN, "split_time": JAN[3]["t"], "history_from": "2024-10-20", "grade": "NO_EDGE_SHOWN"},
            "GBPUSDm": {"trades": gbp, "split_time": gbp[1]["t"], "history_from": "2024-10-20"},
            "NVDAm": {"trades": [], "history_from": "2025-08-11"}}}

    def test_pooled_equals_the_sum_of_its_parts(self):
        rep = replay.build(self.cells(), risk_money=50.0)
        s = rep["strategies"]["pullback_v1"]
        self.assertEqual(s["pooled"]["n"], 9)
        self.assertAlmostEqual(s["pooled"]["total_r"],
                               sum(v["total_r"] for v in s["symbols"].values() if v.get("n")))
        self.assertEqual(s["pooled"]["unseen"]["n"], 3 + 2)
        self.assertEqual(s["pooled"]["symbols_traded"], 2)
        self.assertEqual(len(rep["trades"]["pullback_v1"]), 9)

    def test_each_symbol_reports_its_true_history_start(self):
        s = replay.build(self.cells())["strategies"]["pullback_v1"]["symbols"]
        self.assertEqual(s["NVDAm"]["history_from"], "2025-08-11")
        self.assertEqual(s["NVDAm"]["n"], 0)
        self.assertEqual(s["EURUSDm"]["grade"], "NO_EDGE_SHOWN")

    def test_save_load_and_the_overview_without_trades(self):
        with tempfile.TemporaryDirectory() as d:
            replay.save(replay.build(self.cells()), Path(d))
            got = replay.load(Path(d))
            self.assertIn("pullback_v1", got["strategies"])
            slim = replay.without_trades(got)
            self.assertNotIn("trades", slim)
            self.assertEqual(slim["trade_counts"], {"pullback_v1": 9})
            self.assertEqual(len(list(Path(d).glob("*.json"))), 2)        # the stamped file and latest.json


class TestFromLab(unittest.TestCase):
    def test_lab_entries_lose_their_trades_and_trend_uses_h4_history(self):
        entry = {"verdict": "NOT_ROBUST_ON_TEST", "split_time": 100, "bars": 5000,
                 "trades": [{"t": 50, "x": 60, "s": "BUY", "r": -1.0, "why": "SL", "sym": ""}],
                 "stats": {"grade": "NEGATIVE"}}
        trend = {"verdict": "NO_EDGE_SHOWN", "stats": {"grade": "NO_EDGE_SHOWN"},
                 "trades": [{"entry_time": 10, "exit_time": 99, "direction": "SELL", "r_multiple": 2.5,
                             "exit_reason": "TRAIL"}]}
        hist = {"EURUSDm": {"M15": {"from": "2024-10-20"}, "H4": {"from": "2016-10-23"}}}
        cells = replay.cells_from_lab({"EURUSDm": {"pullback_v1": entry}}, {"EURUSDm": trend}, hist)
        self.assertNotIn("trades", entry)
        c = cells["pullback_v1"]["EURUSDm"]
        self.assertEqual((c["history_from"], c["grade"], c["trades"][0]["sym"]), ("2024-10-20", "NEGATIVE", "EURUSDm"))
        t = cells["trend_daily_v1"]["EURUSDm"]
        self.assertEqual(t["history_from"], "2016-10-23")
        self.assertEqual(t["trades"][0]["s"], "SELL")
        self.assertIsNone(t.get("split_time"))            # pre-registered: nothing held out

    def test_compact_skips_unfinished_trades(self):
        out = replay.compact([{"entry_time": 1, "exit_time": 2, "direction": "Side.BUY", "r_multiple": None},
                              {"entry_time": 1, "exit_time": 2, "direction": "Side.BUY", "r_multiple": 1.2}], "X")
        self.assertEqual(out, [{"sym": "X", "t": 1, "x": 2, "s": "BUY", "r": 1.2, "why": ""}])


class TestResearchOneKeepsTrades(unittest.TestCase):
    def test_keep_trades_is_off_by_default(self):
        import inspect
        from app.research.lab.strategy_research import research_one
        self.assertIs(inspect.signature(research_one).parameters["keep_trades"].default, False)


class TestDailyReview(unittest.TestCase):
    def test_review_lists_each_rule_from_the_report(self):
        from tools.daily_review import render_replay, replay_summary
        rep = replay.build({"pullback_v1": {"EURUSDm": {"trades": JAN, "split_time": JAN[3]["t"]}},
                            "vwap_reversion_v1": {"EURUSDm": {"trades": []}}})
        rp = replay_summary(rep)
        self.assertEqual([r["strategy"] for r in rp["rows"]], ["pullback_v1"])
        text = "\n".join(render_replay(rp))
        self.assertIn("| pullback_v1 | 6 |", text)
        self.assertIn("3.0R", text)
        self.assertIn("None yet", "\n".join(render_replay(None)))

    def test_missing_report_is_not_an_error(self):
        from tools.daily_review import replay_summary
        with tempfile.TemporaryDirectory() as d, mock.patch.object(replay, "ROOT", Path(d)):
            self.assertIsNone(replay_summary())


if __name__ == "__main__":
    unittest.main()
