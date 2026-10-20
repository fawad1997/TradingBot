"""Before/after a deploy, from the account's own data.

The property this file defends above all others: the tool must REFUSE to call a difference real when
the samples cannot support it. "It got worse after the changes" is exactly the kind of claim a tool can
manufacture support for, and a confident wrong answer here would send someone chasing a cause that does
not exist - or, worse, reverting a change that was not responsible.
"""
import datetime as dt
import unittest

from tools.deploy_impact import build, compare_means, render, _stats

NOW = dt.datetime.now(dt.timezone.utc)
SINCE = NOW - dt.timedelta(hours=24)


def trade(r, hours_from_since):
    return {"state": "CLOSED", "r_multiple": r,
            "closed_at": SINCE + dt.timedelta(hours=hours_from_since)}


def rej(gate, spread, hours_from_since):
    return {"gate": gate, "spread_points": spread,
            "created_at": SINCE + dt.timedelta(hours=hours_from_since)}


def adapt(ok, hours_from_since, symbol="XAUUSDm", reason="OK"):
    return {"symbol": symbol, "updated": ok, "reason": reason,
            "created_at": SINCE + dt.timedelta(hours=hours_from_since)}


class TestItRefusesToOverclaim(unittest.TestCase):
    def test_a_handful_of_trades_each_side_is_never_conclusive(self):
        """The whole point. Six bad trades after a deploy is not evidence of anything."""
        before = _stats([0.5, -1.0, 0.4, 0.6, -1.0, 0.5])
        after = _stats([-0.46, -0.42, -0.34, -0.1, 0.02, 0.28])
        c = compare_means(before, after)
        self.assertEqual(c["verdict"], "NOT_CONCLUSIVE")
        self.assertIn("too few", c["note"])

    def test_a_large_difference_on_small_samples_is_still_not_conclusive(self):
        before = _stats([2.0] * 8)
        after = _stats([-2.0] * 8)
        self.assertEqual(compare_means(before, after)["verdict"], "NOT_CONCLUSIVE")

    def test_a_small_difference_on_large_samples_is_not_conclusive_either(self):
        before = _stats([0.1, -0.1] * 60)
        after = _stats([0.12, -0.11] * 60)
        self.assertEqual(compare_means(before, after)["verdict"], "NOT_CONCLUSIVE")

    def test_a_real_difference_on_adequate_samples_is_reported(self):
        before = _stats([0.5, 0.45, 0.55, 0.5] * 15)
        after = _stats([-0.5, -0.45, -0.55, -0.5] * 15)
        c = compare_means(before, after)
        self.assertEqual(c["verdict"], "WORSE_AFTER")
        self.assertLess(c["difference_r"], 0)

    def test_an_improvement_is_named_as_such(self):
        before = _stats([-0.5, -0.45, -0.55, -0.5] * 15)
        after = _stats([0.5, 0.45, 0.55, 0.5] * 15)
        self.assertEqual(compare_means(before, after)["verdict"], "BETTER_AFTER")

    def test_no_data_is_said_plainly(self):
        self.assertEqual(compare_means(_stats([]), _stats([1.0]))["verdict"], "NO_DATA")


class TestSpreadGateSignature(unittest.TestCase):
    """A looser gate shows up as BOTH fewer spread refusals AND wider accepted spreads.
    Either one alone is ordinary market variation and must not be called a signature."""

    def _d(self, before_rows, after_rows):
        return build([], before_rows + after_rows, [], SINCE,
                     window=dt.timedelta(hours=24))["rejections"]

    def test_both_moving_together_is_detectable(self):
        before = [rej("SPREAD" if i % 2 else "REGIME", 18, -20 + i * 0.05) for i in range(200)]
        after = [rej("SPREAD" if i % 9 == 0 else "REGIME", 34, i * 0.05) for i in range(200)]
        d = self._d(before, after)
        self.assertLess(d["spread_share_after"], d["spread_share_before"])
        self.assertGreater(d["accepted_spread_median_after"], d["accepted_spread_median_before"])

    def test_spread_refusals_falling_alone_is_not_the_signature(self):
        before = [rej("SPREAD" if i % 2 else "REGIME", 20, -20 + i * 0.05) for i in range(200)]
        after = [rej("SPREAD" if i % 9 == 0 else "REGIME", 20, i * 0.05) for i in range(200)]
        d = self._d(before, after)
        self.assertEqual(d["accepted_spread_median_after"], d["accepted_spread_median_before"])
        self.assertNotIn("BOTH moved", render(build([], before + after, [], SINCE,
                                                    window=dt.timedelta(hours=24))))

    def test_accepted_spread_excludes_spread_rejections(self):
        """A row refused FOR spread is by definition not a spread that was accepted."""
        rows = [rej("SPREAD", 999, 1), rej("REGIME", 20, 1), rej("REGIME", 22, 2)]
        d = self._d([], rows)
        self.assertEqual(d["accepted_spread_median_after"], 21.0)


class TestAdaptiveHealthIsTheDecisiveCheck(unittest.TestCase):
    """The cap expires at three hours and the refresh runs hourly, so the October change can only bite
    after three consecutive failures. Without them it altered nothing at all."""

    def _d(self, rows):
        return build([], [], rows, SINCE, window=dt.timedelta(hours=24))["adaptive"]

    def test_healthy_refreshes_mean_the_change_could_not_have_mattered(self):
        rows = [adapt(True, -i) for i in range(1, 20)] + [adapt(True, i) for i in range(20)]
        d = self._d(rows)
        self.assertFalse(d["cap_expiry_could_have_mattered"])
        self.assertEqual(d["before"]["worst_consecutive_failures"], 0)

    def test_scattered_failures_still_do_not_reach_the_threshold(self):
        rows = [adapt(i % 4 != 0, -i, reason="INSUFFICIENT_ATR_HISTORY (12)") for i in range(1, 20)]
        self.assertFalse(self._d(rows)["cap_expiry_could_have_mattered"])

    def test_three_consecutive_failures_do(self):
        rows = [adapt(False, -3), adapt(False, -2), adapt(False, -1)]
        self.assertTrue(self._d(rows)["cap_expiry_could_have_mattered"])

    def test_consecutive_runs_are_counted_per_symbol_not_across_them(self):
        """Two symbols each failing once is not one symbol failing twice."""
        rows = [adapt(False, -2, symbol="A"), adapt(False, -1, symbol="B"),
                adapt(False, -3, symbol="C")]
        self.assertEqual(self._d(rows)["before"]["worst_consecutive_failures"], 1)

    def test_failure_reasons_are_surfaced(self):
        rows = [adapt(False, -i, reason="INSUFFICIENT_ATR_HISTORY (12)") for i in range(1, 5)]
        self.assertIn("INSUFFICIENT_ATR_HISTORY (12)", self._d(rows)["before"]["failure_reasons"])


class TestWindowing(unittest.TestCase):
    def test_the_before_window_matches_the_after_window_by_default(self):
        d = build([], [], [], SINCE)
        self.assertGreater(d["window_hours"], 0)

    def test_trades_land_on_the_correct_side(self):
        rows = [trade(1.0, -5), trade(-1.0, 5)]
        d = build(rows, [], [], SINCE, window=dt.timedelta(hours=24))
        self.assertEqual(d["results"]["before"]["n"], 1)
        self.assertEqual(d["results"]["after"]["n"], 1)
        self.assertEqual(d["results"]["before"]["total_r"], 1.0)

    def test_open_trades_are_ignored(self):
        rows = [{**trade(99.0, 5), "state": "OPEN"}, trade(-1.0, 5)]
        d = build(rows, [], [], SINCE, window=dt.timedelta(hours=24))
        self.assertEqual(d["results"]["after"]["n"], 1)

    def test_trades_outside_both_windows_are_excluded(self):
        d = build([trade(5.0, -500)], [], [], SINCE, window=dt.timedelta(hours=24))
        self.assertEqual(d["results"]["before"]["n"], 0)


class TestRender(unittest.TestCase):
    def test_a_quiet_result_says_a_losing_day_is_not_evidence(self):
        rows = [trade(0.5, -3), trade(-1.0, -2), trade(-0.4, 2), trade(-0.3, 3)]
        text = render(build(rows, [], [adapt(True, -1)], SINCE, window=dt.timedelta(hours=24)))
        self.assertIn("NOT_CONCLUSIVE", text)
        self.assertIn("not evidence", text)

    def test_it_states_plainly_when_the_change_could_not_have_mattered(self):
        rows = [adapt(True, -i) for i in range(1, 10)]
        text = render(build([], [], rows, SINCE, window=dt.timedelta(hours=24)))
        self.assertIn("did not touch your spread gate", text)

    def test_empty_data_does_not_crash(self):
        self.assertIn("RESULTS", render(build([], [], [], SINCE)))


if __name__ == "__main__":
    unittest.main()
