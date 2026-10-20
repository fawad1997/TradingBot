"""Judging the portfolio rather than the strategies one at a time.

The finding this tool exists to surface is the one a per-strategy win-rate table structurally cannot
show: a strategy can lose money overall and still be worth keeping because it wins when everything else
loses, and a strategy can look excellent while deepening the drawdown it helped create.
"""
import datetime as dt
import unittest

from tools.portfolio_report import (build, correlated_losses, drawdown, load,
                                    marginal_contribution, render)

BASE = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)


def t(sid, r, day, hour=0):
    return {"state": "CLOSED", "strategy_id": sid, "symbol": "X", "r_multiple": r,
            "closed_at": BASE + dt.timedelta(days=day, hours=hour)}


class TestDrawdown(unittest.TestCase):
    def test_a_rising_curve_has_no_drawdown(self):
        self.assertEqual(drawdown([1.0, 1.0, 1.0])["max_drawdown_r"], 0.0)

    def test_depth_is_peak_to_trough_not_start_to_end(self):
        d = drawdown([5.0, -2.0, -1.0, 4.0])
        self.assertAlmostEqual(d["max_drawdown_r"], 3.0)

    def test_an_open_drawdown_at_the_end_counts(self):
        d = drawdown([5.0, -1.0, -1.0])
        self.assertTrue(d["still_open"])
        self.assertAlmostEqual(d["max_drawdown_r"], 2.0)

    def test_duration_is_measured_in_trades(self):
        d = drawdown([5.0, -1.0, -1.0, -1.0, 9.0])
        self.assertEqual(d["longest_drawdown_trades"], 3)

    def test_an_empty_record_does_not_explode(self):
        self.assertEqual(drawdown([])["max_drawdown_r"], 0.0)


class TestLoad(unittest.TestCase):
    def test_open_trades_are_excluded(self):
        rows = [t("a", 1.0, 0), {**t("a", 99.0, 1), "state": "OPEN"}]
        self.assertEqual(len(load(rows)), 1)

    def test_trades_without_a_timestamp_are_excluded(self):
        rows = [t("a", 1.0, 0), {"state": "CLOSED", "strategy_id": "a", "r_multiple": 1.0}]
        self.assertEqual(len(load(rows)), 1)

    def test_results_come_back_oldest_first(self):
        rows = [t("a", 1.0, 5), t("a", 2.0, 1), t("a", 3.0, 3)]
        self.assertEqual([x["r"] for x in load(rows)], [2.0, 3.0, 1.0])


class TestCorrelatedLosses(unittest.TestCase):
    def test_strategies_losing_on_the_same_day_are_counted(self):
        rows = [t("a", -1.0, 1), t("b", -1.0, 1), t("a", 1.0, 2), t("b", 1.0, 2)]
        c = correlated_losses(load(rows))
        self.assertEqual(c["losing_buckets"], 1)
        self.assertEqual(c["multi_strategy_loss_buckets"], 1)
        self.assertEqual(c["together_rate"], 1.0)

    def test_strategies_losing_on_different_days_are_not(self):
        rows = [t("a", -1.0, 1), t("b", 1.0, 1), t("b", -1.0, 2), t("a", 1.0, 2)]
        c = correlated_losses(load(rows))
        self.assertEqual(c["multi_strategy_loss_buckets"], 0)

    def test_the_pair_rate_is_out_of_days_both_actually_traded(self):
        rows = [t("a", -1.0, 1), t("b", -1.0, 1),
                t("a", -1.0, 2), t("b", 1.0, 2),
                t("a", -1.0, 3)]                      # b did not trade on day 3
        c = correlated_losses(load(rows))
        pair = next(p for p in c["pairs"] if sorted(p["pair"]) == ["a", "b"])
        self.assertEqual(pair["both_traded"], 2, "day 3 must not count against the pair")
        self.assertEqual(pair["lost_together"], 1)

    def test_weekly_buckets_group_more_than_daily(self):
        # BASE is 2026-08-01, a Saturday, so days 2 and 4 are Mon and Wed of the SAME ISO week.
        rows = [t("a", -1.0, 2), t("b", -1.0, 4)]
        self.assertEqual(correlated_losses(load(rows), "D")["multi_strategy_loss_buckets"], 0)
        self.assertEqual(correlated_losses(load(rows), "W")["multi_strategy_loss_buckets"], 1)


class TestMarginalContribution(unittest.TestCase):
    def test_marginal_r_equals_the_strategys_own_total(self):
        rows = [t("a", 2.0, 1), t("b", -1.0, 2), t("a", 1.0, 3)]
        m = marginal_contribution(load(rows))
        self.assertAlmostEqual(m["a"]["marginal_r"], 3.0)
        self.assertAlmostEqual(m["b"]["marginal_r"], -1.0)

    def test_a_losing_strategy_that_wins_on_bad_days_cushions_the_drawdown(self):
        """The finding a win-rate table cannot produce.

        `hedge` loses overall, but it wins exactly when `main` has its worst run, so removing it makes
        the portfolio's worst stretch deeper."""
        # Timing is the whole point. The hedge takes its loss EARLY, while the book is climbing and can
        # absorb it, and does its winning during the collapse. Net it loses money; it still makes the
        # worst stretch shallower, which is invisible in a per-strategy table.
        rows = [t("main", 4.0, 0)]
        rows.append(t("hedge", -14.0, 1))             # early loss, before the peak:  -14
        rows += [t("main", 4.0, i) for i in range(2, 9)]          # climb to the peak: +28
        for i in range(9, 17):
            rows.append(t("main", -3.0, i))           # the collapse:                 -24
            rows.append(t("hedge", 1.5, i, hour=1))   # hedge wins through it:        +12
        m = marginal_contribution(load(rows))
        self.assertLess(m["hedge"]["own_total_r"], 0, "the hedge must be a net loser overall")
        self.assertGreater(m["main"]["own_total_r"], 0)
        self.assertGreater(m["hedge"]["drawdown_delta"], 0,
                           "removing the hedge should deepen the worst drawdown")
        self.assertIn("cushions drawdown", m["hedge"]["verdict"])

    def test_a_tiny_drawdown_shift_is_not_reported_as_a_finding(self):
        """Materiality, not sign: a 0.04R shift in a 15R drawdown is noise."""
        rows = [t("a", 5.0, 1), t("a", -6.0, 2), t("a", 4.0, 3),
                t("b", 0.02, 4), t("b", -0.01, 5)]
        m = marginal_contribution(load(rows))
        self.assertNotIn("drawdown", m["b"]["verdict"])

    def test_every_strategy_gets_a_verdict(self):
        rows = [t("a", 2.0, 1), t("b", -1.0, 2)]
        for sid, m in marginal_contribution(load(rows)).items():
            self.assertTrue(m["verdict"], sid)


class TestBuild(unittest.TestCase):
    def test_total_net_r_is_the_headline(self):
        rows = [t("a", 2.0, 1), t("b", -3.0, 2)]
        self.assertAlmostEqual(build(rows)["total_net_r"], -1.0)

    def test_a_portfolio_whose_interval_spans_zero_is_called_out(self):
        rows = [t("a", r, i) for i, r in enumerate([1.0, -1.0, 2.0, -2.0, 0.5, -0.4])]
        d = build(rows)
        self.assertLess(d["ci95"][0], 0)
        self.assertGreater(d["ci95"][1], 0)
        self.assertIn("has not been shown to have an edge", render(d))

    def test_no_data_is_reported_not_crashed(self):
        self.assertEqual(build([])["n_trades"], 0)
        self.assertIn("no closed trades", render(build([])))

    def test_render_covers_all_four_sections(self):
        rows = [t("a", 2.0, 1), t("b", -3.0, 2), t("a", -1.0, 3), t("b", 1.0, 4)]
        text = render(build(rows))
        for section in ("PORTFOLIO", "DRAWDOWN", "CORRELATED LOSSES", "MARGINAL CONTRIBUTION"):
            self.assertIn(section, text)


if __name__ == "__main__":
    unittest.main()
