"""The live record, per strategy and per strategy+symbol.

The property this file defends: the tool must never let a good-looking mean pass as a finding. "+0.308R
per trade over 23 trades, 78% won" is the number that started this whole project, and it means nothing.
Any row whose 95% interval contains zero must read NOT_PROVEN however attractive the mean is.
"""
import math
import unittest

from tools.live_breakdown import build, render, summarise, trades_needed


def closed(sid, sym, rs):
    return [{"state": "CLOSED", "strategy_id": sid, "symbol": sym, "r_multiple": r} for r in rs]


class TestSummarise(unittest.TestCase):
    def test_no_trades(self):
        self.assertEqual(summarise([])["verdict"], "NO_TRADES")

    def test_a_single_trade_gets_no_interval(self):
        s = summarise([3.0])
        self.assertEqual(s["verdict"], "TOO_FEW")
        self.assertIsNone(s["lo"])

    def test_a_small_flattering_sample_is_not_proven(self):
        """The founding error of this project, encoded as a test.

        These are the real shape of the 23-trade record that first suggested the system was profitable:
        mean about +0.33R, 78% won. Wins vary in size and losses cluster near -1R, as they actually do.
        The mean looks excellent and the interval still spans zero."""
        wins = [0.4, 0.5, 0.6, 0.3, 1.8, 0.45, 0.35, 2.9, 0.5, 0.4,
                0.55, 0.3, 0.45, 1.2, 0.4, 0.35, 0.5, 0.6]
        losses = [-1.0, -1.0, -1.05, -0.95, -1.0]
        s = summarise(wins + losses)
        self.assertEqual(s["n"], 23)
        self.assertGreater(s["mean_r"], 0.3)
        self.assertGreater(s["win_rate"], 0.75)
        self.assertEqual(s["verdict"], "NOT_PROVEN",
                         "a 78% win rate over 23 trades must not be reported as an edge")
        self.assertLess(s["lo"], 0)

    def test_consistent_outcomes_can_be_proven_even_in_a_small_sample(self):
        """The opposite guard: the rule is about the INTERVAL, not about sample size alone.

        18 wins of exactly +1R and 5 losses of exactly -1R really is significant, because the outcomes
        barely vary. Real trades are not that tidy - which is why the test above is the realistic one -
        but the tool must follow the arithmetic rather than a blanket 'small = unproven' rule."""
        s = summarise([1.0] * 18 + [-1.0] * 5)
        self.assertEqual(s["verdict"], "POSITIVE")
        self.assertGreater(s["lo"], 0)

    def test_a_large_consistent_sample_is_proven(self):
        s = summarise([0.3, 0.35, 0.25, 0.4, 0.2] * 100)
        self.assertEqual(s["verdict"], "POSITIVE")
        self.assertGreater(s["lo"], 0)

    def test_a_clearly_losing_sample_is_negative(self):
        s = summarise([-0.5, -0.6, -0.4, -0.55, -0.45] * 100)
        self.assertEqual(s["verdict"], "NEGATIVE")
        self.assertLess(s["hi"], 0)

    def test_the_interval_is_symmetric_about_the_mean(self):
        s = summarise([1.0, -1.0, 0.5, -0.5, 2.0, -2.0, 0.25])
        self.assertAlmostEqual((s["lo"] + s["hi"]) / 2, s["mean_r"], places=6)

    def test_non_finite_values_are_dropped_not_propagated(self):
        s = summarise([1.0, float("nan"), 2.0, None, float("inf")])
        self.assertEqual(s["n"], 2)
        self.assertTrue(math.isfinite(s["mean_r"]))


class TestTradesNeeded(unittest.TestCase):
    def test_more_noise_needs_more_trades(self):
        self.assertGreater(trades_needed(1.2, 0.1), trades_needed(0.6, 0.1))

    def test_a_bigger_edge_needs_fewer_trades(self):
        self.assertLess(trades_needed(1.0, 0.3), trades_needed(1.0, 0.1))

    def test_degenerate_inputs_return_none_rather_than_a_fake_number(self):
        self.assertIsNone(trades_needed(0, 0.1))
        self.assertIsNone(trades_needed(1.0, 0))
        self.assertIsNone(trades_needed(1.0, -0.2))


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.rows = (closed("micro_scalper", "XAUUSDm", [-0.4] * 30)
                     + closed("micro_scalper", "EURUSDm", [-0.3] * 21)
                     + closed("pullback_v1", "XAUUSDm", [0.2] * 22))

    def test_open_trades_are_excluded(self):
        rows = self.rows + [{"state": "OPEN", "strategy_id": "pullback_v1",
                             "symbol": "XAUUSDm", "r_multiple": 99.0}]
        self.assertEqual(build(rows)["overall"]["n"], 73)

    def test_broker_closed_counts_as_closed(self):
        rows = [{"state": "BROKER_CLOSED", "strategy_id": "a", "symbol": "X", "r_multiple": 1.0}]
        self.assertEqual(build(rows)["overall"]["n"], 1)

    def test_strategy_and_cell_totals_agree(self):
        d = build(self.rows)
        self.assertAlmostEqual(d["by_strategy"]["micro_scalper"]["total_r"],
                               d["by_cell"]["micro_scalper | XAUUSDm"]["total_r"]
                               + d["by_cell"]["micro_scalper | EURUSDm"]["total_r"], places=2)

    def test_min_trades_hides_cells_without_losing_the_count(self):
        d = build(self.rows, min_trades=25)
        self.assertLess(d["cells_shown"], d["cells_total"])

    def test_a_trade_with_no_r_multiple_is_skipped(self):
        rows = self.rows + [{"state": "CLOSED", "strategy_id": "a", "symbol": "X", "r_multiple": None}]
        self.assertEqual(build(rows)["overall"]["n"], 73)

    def test_render_marks_the_unproven_cells(self):
        text = render(build(self.rows))
        self.assertIn("BY STRATEGY", text)
        self.assertIn("interval excludes zero", text)


if __name__ == "__main__":
    unittest.main()
