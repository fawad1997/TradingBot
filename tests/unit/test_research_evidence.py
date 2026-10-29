"""The lab's evidence statistics: each one must say "luck" on noise and "edge" on a planted edge.

The October 2026 lab looked at 195 cells and called half of them robust - what noise does. These tests pin the
four corrections on cases where the right answer is known: pure noise, one setting genuinely better than the
rest, a rule that knows the direction, and a rule that does not.
"""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np

from app.features.frame import FeatureFrame
from app.research.lab import stats


def trending_h4(days=900, seed=3, drift=0.004, vol=0.01):
    """H4 bars of a market with persistent multi-week trends (regime-switching drift)."""
    rng = np.random.default_rng(seed)
    n = days * 6
    regime = np.repeat(rng.choice([1.0, -1.0], size=n // 240 + 1), 240)[:n]
    r = rng.normal(0, vol / np.sqrt(6), n) + regime * drift / 6
    c = 100 * np.exp(np.cumsum(r))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, vol / 8, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, vol / 8, n)))
    t = 1_600_000_000 // 86400 * 86400 + np.arange(n) * 14400
    return FeatureFrame(t, o, h, l, c, np.full(n, 2.0), np.full(n, 100.0), "H4")


class TestRangeAndSharpe(unittest.TestCase):
    def test_a_constant_result_has_no_uncertainty(self):
        ci = stats.mean_ci([0.5] * 40)
        self.assertEqual((ci["lo"], ci["hi"], ci["mean"]), (0.5, 0.5, 0.5))

    def test_the_95_percent_range_covers_the_truth_about_95_percent_of_the_time(self):
        rng = np.random.default_rng(1)
        hits = 0
        for k in range(200):
            ci = stats.mean_ci(rng.normal(0, 1, 200), n_boot=400, seed=k)
            hits += ci["lo"] <= 0 <= ci["hi"]
        self.assertTrue(0.88 <= hits / 200 <= 0.99, hits)

    def test_too_few_trades_give_no_range(self):
        self.assertIsNone(stats.mean_ci([1.0])["lo"])

    def test_psr_is_a_coin_flip_at_zero_sharpe(self):
        self.assertAlmostEqual(stats.psr(0.0, 100, 0.0, 3.0), 0.5, places=6)

    def test_deflated_equals_probabilistic_for_one_trial(self):
        self.assertAlmostEqual(stats.dsr(0.2, 100, 0.0, 3.0, 1, 0.01), stats.psr(0.2, 100, 0.0, 3.0), places=9)

    def test_more_trials_need_a_bigger_sharpe(self):
        values = [stats.dsr(0.2, 100, 0.0, 3.0, n, 0.01) for n in (1, 10, 100, 1000)]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertLess(values[-1], 0.5, "the best of 1000 noise trials beats a 0.2 per-trade Sharpe on 100 trades")

    def test_the_noise_benchmark_grows_with_the_trial_count(self):
        bench = [stats.expected_max_sr(n, 0.01) for n in (2, 20, 200)]
        self.assertEqual(bench, sorted(bench))
        self.assertEqual(stats.expected_max_sr(1, 0.01), 0.0)


class TestOverfitting(unittest.TestCase):
    def _matrix(self, means, blocks=8, trades=30, seed=0):
        rng = np.random.default_rng(seed)
        sums = np.zeros((blocks, len(means)))
        counts = np.full((blocks, len(means)), float(trades))
        for b in range(blocks):
            for m, mu in enumerate(means):
                sums[b, m] = rng.normal(mu, 1.0, trades).sum()
        return sums, counts

    def test_picking_the_best_of_pure_noise_is_a_coin_flip_out_of_sample(self):
        pbos = [stats.pbo_cscv(*self._matrix([0.0] * 16, seed=s))["pbo"] for s in range(6)]
        self.assertTrue(0.25 < float(np.mean(pbos)) < 0.75, pbos)

    def test_a_setting_that_is_really_better_is_not_overfit(self):
        out = stats.pbo_cscv(*self._matrix([0.0] * 15 + [0.6]))
        self.assertLess(out["pbo"], 0.1)
        self.assertEqual(out["splits"], 70)            # C(8, 4)

    def test_one_setting_cannot_be_ranked(self):
        self.assertIsNone(stats.pbo_cscv(*self._matrix([0.1])))

    def test_block_matrix_buckets_by_entry_bar(self):
        sums, counts = stats.block_matrix([([0, 10, 99], [1.0, 2.0, 3.0])], 100, blocks=4)
        self.assertEqual(counts[:, 0].tolist(), [2, 0, 0, 1])
        self.assertEqual(sums[:, 0].tolist(), [3.0, 0.0, 0.0, 3.0])


class TestRandomDirection(unittest.TestCase):
    def test_a_rule_with_no_directional_information_is_not_better_than_random(self):
        rng = np.random.default_rng(5)
        r_long = rng.normal(0, 1, 300)
        out = stats.random_direction(r_long, -r_long, rng.choice([-1.0, 1.0], 300), 200, 0)
        self.assertGreater(out["p_value"], 0.05)

    def test_a_rule_that_knows_the_direction_beats_random(self):
        rng = np.random.default_rng(6)
        r_long = rng.normal(0, 1, 300)
        sides = np.where(r_long > 0, 1.0, -1.0)            # picks the winning side every time
        out = stats.random_direction(r_long, -r_long, sides, 200, 0)
        self.assertLess(out["p_value"], 0.01)
        self.assertGreater(out["edge_vs_random"], 0.5)


class TestGrade(unittest.TestCase):
    CI_UP = {"n": 100, "mean": 0.3, "lo": 0.1, "hi": 0.5}

    def test_edge_needs_all_three(self):
        rnd = {"p_value": 0.01}
        self.assertEqual(stats.grade(self.CI_UP, 0.97, rnd), stats.EDGE)
        self.assertEqual(stats.grade(self.CI_UP, 0.90, rnd), stats.NO_EDGE_SHOWN)       # deflated Sharpe short
        self.assertEqual(stats.grade(self.CI_UP, 0.97, {"p_value": 0.2}), stats.NO_EDGE_SHOWN)  # random as good
        self.assertEqual(stats.grade({**self.CI_UP, "lo": -0.1}, 0.97, rnd), stats.NO_EDGE_SHOWN)

    def test_a_range_below_zero_is_negative_and_few_trades_are_thin(self):
        self.assertEqual(stats.grade({"n": 50, "mean": -0.3, "lo": -0.5, "hi": -0.1}, None, None), stats.NEGATIVE)
        self.assertEqual(stats.grade({"n": 5, "mean": 1.0, "lo": 0.5, "hi": 1.5}, 0.99, {"p_value": 0.0}),
                         stats.THIN)

    def test_deflation_counts_every_cell_looked_at(self):
        rng = np.random.default_rng(9)
        cells = {}
        for k in range(40):                                  # forty noise cells
            rs = rng.normal(0, 1, 60)
            ci, sh = stats.mean_ci(rs), stats.sharpe_stats(rs)
            cells[f"c{k}"] = {"stats": {"n": 60, "ci": ci, "sr": sh["sr"], "skew": sh["skew"], "kurt": sh["kurt"],
                                        "random": {"p_value": 0.5}}}
        summary = stats.deflate_cells(cells)
        self.assertEqual(summary["n_trials"], 40)
        self.assertGreater(summary["benchmark_sr"], 0)
        self.assertEqual(summary["grades"].get(stats.EDGE, 0), 0, "forty noise cells must not produce an edge")
        self.assertTrue(all("dsr" in c["stats"] and "grade" in c["stats"] for c in cells.values()))


class TestTrendRule(unittest.TestCase):
    def test_daily_bars_only_include_days_that_have_ended(self):
        h4 = trending_h4(days=10)
        day3_end = int(h4.t[0]) + 3 * 86400
        d = __import__("app.research.lab.hypotheses", fromlist=["daily_bars"]).daily_bars(h4, until=day3_end + 3600)
        self.assertEqual(len(d), 3)                       # day 4 has started but not ended
        self.assertEqual(int(d.t[-1]) % 86400, 0)

    def test_the_rule_is_causal(self):
        from app.research.lab.hypotheses import daily_bars, trend_daily
        d = daily_bars(trending_h4())
        full = trend_daily(d)
        for cut in (300, 450, 600):
            part = trend_daily(FeatureFrame(d.t[:cut], d.o[:cut], d.h[:cut], d.l[:cut], d.c[:cut], d.spread[:cut],
                                            d.tick_volume[:cut], "D1"))
            np.testing.assert_array_equal(part["long"], full["long"][:cut])
            np.testing.assert_array_equal(part["short"], full["short"][:cut])

    def test_on_a_trending_market_it_makes_money_and_beats_random_sides(self):
        from app.research.lab.trend import research_trend
        out = research_trend(trending_h4(days=1500), None, spread_price=0.0, slip_price=0.0)
        self.assertGreater(out["metrics"]["n_trades"], 20)
        self.assertGreater(out["stats"]["ci"]["mean"], 0)
        self.assertGreater(out["stats"]["random"]["edge_vs_random"], 0)

    def test_swap_is_charged_per_night_held(self):
        from app.research.lab.trend import swap_cost_r
        spec = SimpleNamespace(swap_long=-100.0, swap_short=0.0, point=0.01, swap_mode=1,
                               raw={"swap_rollover3days": 3})
        mon = int(datetime(2026, 10, 19, 0, tzinfo=timezone.utc).timestamp())
        nights, cost = swap_cost_r(spec, 1, mon, mon + 7 * 86400, 10.0)
        self.assertEqual(nights, 7)                        # Mon-Fri, Wednesday triple, nothing at the weekend
        self.assertAlmostEqual(cost, 7 * 100 * 0.01 / 10.0)
        self.assertEqual(swap_cost_r(spec, -1, mon, mon + 7 * 86400, 10.0)[1], 0.0)   # the free side

    def test_the_profit_lock_would_cut_the_trend_short(self):
        from app.config import ConfigManager
        from app.research.lab.trend import research_trend
        out = research_trend(trending_h4(days=1500), None, execution=ConfigManager().app.execution)
        self.assertLess(out["exits"]["with_profit_lock"]["mean"], out["exits"]["trail_only"]["mean"])


class TestIntradayMomentum(unittest.TestCase):
    def _m5(self, days=40):
        ny = ZoneInfo("America/New_York")
        t, rng = [], np.random.default_rng(2)
        d0 = datetime(2026, 3, 2, tzinfo=timezone.utc)          # straddles the US daylight-saving change (8 Mar)
        for k in range(days):
            day = d0.date().fromordinal(d0.date().toordinal() + k)
            if day.weekday() >= 5:
                continue
            start = int(datetime(day.year, day.month, day.day, 9, 30, tzinfo=ny).timestamp())
            t.extend(range(start, start + 390 * 60, 300))
        t = np.asarray(t, np.int64)
        c = 100 + np.cumsum(rng.normal(0, 0.1, len(t)))
        o = np.r_[c[0], c[:-1]]
        return FeatureFrame(t, o, np.maximum(o, c) + 0.02, np.minimum(o, c) - 0.02, c, np.zeros(len(t)),
                            np.ones(len(t)), "M5")

    def test_one_signal_per_session_on_the_bar_closing_at_15_30_new_york(self):
        from app.research.lab.hypotheses import intraday_momentum
        f = self._m5()
        sig = intraday_momentum(f)
        ny = ZoneInfo("America/New_York")
        idx = np.flatnonzero(sig["long"] | sig["short"])
        self.assertGreater(len(idx), 20)
        for j in idx:
            close = datetime.fromtimestamp(int(f.t[j]) + 300, tz=ny)
            self.assertEqual((close.hour, close.minute), (15, 30), "entry must be 30 min before the close, DST or not")
        self.assertEqual(sig["hold"], 6)

    def test_the_session_close_is_read_from_the_symbols_own_bars(self):
        """Exness stops its US stock CFDs at 15:45 New York; a rule waiting for a 16:00 bar never fired on them."""
        from app.research.lab.hypotheses import intraday_momentum, session_close_ny
        f = self._m5()
        keep = np.asarray([datetime.fromtimestamp(int(x) + 300, tz=ZoneInfo("America/New_York")).strftime("%H:%M")
                           <= "15:45" for x in f.t])
        g = FeatureFrame(f.t[keep], f.o[keep], f.h[keep], f.l[keep], f.c[keep], f.spread[keep],
                         f.tick_volume[keep], "M5")
        self.assertEqual(session_close_ny(g), (15, 45))
        self.assertEqual(int(intraday_momentum(g)["long"].sum() + intraday_momentum(g)["short"].sum()), 0)
        sig = intraday_momentum(g, None, {"close_hour": 15, "close_minute": 45})
        self.assertGreater(int(sig["long"].sum() + sig["short"].sum()), 20)

    def test_it_is_causal(self):
        from app.research.lab.hypotheses import intraday_momentum
        f = self._m5()
        full = intraday_momentum(f)
        for cut in (900, 1500, 2100):
            part = intraday_momentum(FeatureFrame(f.t[:cut], f.o[:cut], f.h[:cut], f.l[:cut], f.c[:cut],
                                                  f.spread[:cut], f.tick_volume[:cut], "M5"))
            np.testing.assert_array_equal(part["long"], full["long"][:cut])
            np.testing.assert_array_equal(part["short"], full["short"][:cut])


class TestLookahead(unittest.TestCase):
    """The project's own truncation test (tools/lookahead_check.py, Chan ch.3) on both new rules."""

    def test_both_new_rules_pass_the_truncation_check(self):
        from app.research.lab.hypotheses import daily_bars, intraday_momentum, trend_daily
        from tools.lookahead_check import check_frame

        def decisions(rule):
            return lambda f: [int(a) - int(b) for a, b in zip(rule(f)["long"], rule(f)["short"])]
        r = check_frame(daily_bars(trending_h4()), decisions(trend_daily), 200)
        self.assertTrue(r["ok"], r)
        r = check_frame(TestIntradayMomentum()._m5(days=60), decisions(intraday_momentum), 800)
        self.assertTrue(r["ok"], r)


class TestLabData(unittest.TestCase):
    def test_a_refused_long_request_is_fetched_in_chunks(self):
        from app.core.types import Bar
        from app.research.lab.data import fetch_range

        class Picky:
            calls = 0

            def rates_range(self, symbol, tf, start, end):
                Picky.calls += 1
                if (end - start).days > 100:
                    return None                              # what MT5 does with "Invalid params"
                return [Bar(int(start.timestamp()), 1, 1, 1, 1, 1, 1, 0)]
        end = datetime(2026, 10, 20, tzinfo=timezone.utc)
        bars = fetch_range(Picky(), "X", "M5", end.replace(year=2025), end, chunk_days=90)
        self.assertEqual(len(bars), 5)                       # 365 days in 90-day pieces
        self.assertEqual(Picky.calls, 2 + 5)                 # one try, one retry, then the chunks

    def test_bar_spreads_are_scaled_up_to_tick_spreads_never_down(self):
        from app.research.lab.data import spread_ratio
        m1 = FeatureFrame(np.arange(500) * 60, np.ones(500), np.ones(500), np.ones(500), np.ones(500),
                          np.full(500, 10.0), np.ones(500), "M1")
        self.assertAlmostEqual(spread_ratio(m1, 0.01, np.full(500, 0.15)), 1.5)
        self.assertEqual(spread_ratio(m1, 0.01, np.full(500, 0.05)), 1.0)
        self.assertEqual(spread_ratio(m1, 0.01, None), 1.0)


class TestCellStats(unittest.TestCase):
    def test_a_grid_cell_gets_range_overfitting_benchmark_and_exits(self):
        from app.alpha.exits import with_execution_exits
        from app.config import ConfigManager
        from app.research.lab.evidence import cell_stats
        rng = np.random.default_rng(4)
        n = 6000
        c = 100 + np.cumsum(rng.normal(0, 0.2, n))
        o = np.r_[c[0], c[:-1]]
        f = FeatureFrame(np.arange(n) * 300, o, np.maximum(o, c) + 0.05, np.minimum(o, c) - 0.05, c,
                         np.zeros(n), np.ones(n), "M5")
        longs = np.zeros(n, bool)
        shorts = np.zeros(n, bool)
        longs[100::37] = True
        shorts[118::37] = True
        traded = with_execution_exits({"type": "fixed", "breakeven": {"enabled": True, "activate_r": 0.5}},
                                      ConfigManager().app.execution)
        st = cell_stats(f, longs, shorts, np.asarray(f.atr(14), float), [1.0, 2.0], [1.0, 2.0], 0.7,
                        {"sl_atr": 1.0, "rr": 2.0}, np.full(n, 0.02), 0.0, 0.0, traded)
        for key in ("ci", "sr", "pbo", "random", "exits"):
            self.assertIn(key, st)
        self.assertIn("as_traded", st["exits"])
        self.assertGreater(st["random"]["p_value"], 0.01, "random entries on a random walk carry no direction")


if __name__ == "__main__":
    unittest.main()
