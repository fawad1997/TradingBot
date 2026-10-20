"""The changes taken from the trading literature, and the properties each is supposed to hold.

Grouped by source so a future reader can tell which book an assertion is defending:

  Lopez de Prado   sample uniqueness, purged/embargoed splits, triple-barrier labels, strategy risk
  Chan             drawdown duration, bad-tick filtering, free-parameter budget, look-ahead by truncation
  Kissell          the cost-model z-score, effective/realized spread, slippage normalisation
  Liu              a random-search control arm for the tuner

The most important tests here are the ones that check a measure REFUSES to flatter: a readiness check
that passes a 23-trade sample, or a cost audit that declares a model honest on four fills, would be
worse than no check at all.
"""
import math
import unittest
from datetime import datetime, timedelta, timezone

from app.execution.cost_audit import (audit_cost_model, cost_zscore, execution_cost, mean_sd,
                                      signed_bp, timing_risk_r)
from app.ml.sampling import (average_uniqueness, concurrency, effective_sample_size, label_report,
                             label_spans, purge_and_embargo, sample_weights, triple_barrier_label)
from app.research.lab.idle import deep_research_plan, is_weekend_closed, minutes_until_open, run_deep_research
from app.research.metrics import max_drawdown, max_drawdown_duration
from tools.parameter_audit import audit as parameter_audit
from tools.strategy_risk import assess as strategy_risk, breakeven_precision, sharpe, trades_needed
from tools.lookahead_check import check_frame, compare_signals

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)


def sig(minutes_in, minutes_out, **extra):
    return {"created_at": (T0 + timedelta(minutes=minutes_in)).isoformat(),
            "closed_at": (T0 + timedelta(minutes=minutes_out)).isoformat(), **extra}


# ===================================================================== Lopez de Prado ch.4
class TestSampleUniqueness(unittest.TestCase):
    def test_non_overlapping_trades_are_fully_unique(self):
        rows = [sig(0, 10), sig(20, 30), sig(40, 50)]
        self.assertEqual([round(u, 6) for u in average_uniqueness(rows)], [1.0, 1.0, 1.0])

    def test_perfectly_overlapping_trades_share_their_weight(self):
        """Three trades over the same bars carry one trade's worth of information between them."""
        rows = [sig(0, 60), sig(0, 60), sig(0, 60)]
        u = average_uniqueness(rows)
        self.assertEqual([round(x, 4) for x in u], [0.3333, 0.3333, 0.3333])
        self.assertAlmostEqual(effective_sample_size(rows), 1.0, places=3)

    def test_effective_sample_size_is_below_the_row_count_when_trades_overlap(self):
        """The number that matters for any sample-size rule, and the point of the whole exercise."""
        rows = [sig(i * 5, i * 5 + 60) for i in range(12)]      # heavy overlap, as 13 symbols produce
        self.assertLess(effective_sample_size(rows), len(rows) / 2)

    def test_weights_average_to_one(self):
        """Rescaled so weighting does not silently change regularisation strength too."""
        rows = [sig(0, 60), sig(0, 60), sig(100, 110)]
        w = sample_weights(rows)
        self.assertAlmostEqual(sum(w) / len(w), 1.0, places=6)

    def test_the_unique_trade_outweighs_the_overlapping_ones(self):
        rows = [sig(0, 60), sig(0, 60), sig(100, 110)]
        w = sample_weights(rows)
        self.assertGreater(w[2], w[0])

    def test_a_missing_close_time_is_treated_conservatively(self):
        """An unknown span must not look instantaneous, which would make the row falsely unique."""
        rows = [{"created_at": T0.isoformat()}, sig(1, 50)]
        spans = label_spans(rows)
        self.assertGreater(spans[0][1], spans[0][0])

    def test_concurrency_counts_the_label_itself(self):
        self.assertEqual(concurrency(label_spans([sig(0, 10)])), [1])


# ===================================================================== Lopez de Prado ch.7
class TestPurgeAndEmbargo(unittest.TestCase):
    def test_a_trade_spanning_the_boundary_is_purged(self):
        rows = [sig(0, 10), sig(20, 400), sig(300, 310), sig(320, 330)]
        keep = purge_and_embargo(rows, split=2, embargo_fraction=0.0)
        self.assertIn(0, keep)
        self.assertNotIn(1, keep, "row 1 is still open deep into the test window and must be purged")

    def test_training_rows_safely_before_the_test_window_survive(self):
        rows = [sig(0, 10), sig(20, 30), sig(1000, 1010), sig(1020, 1030)]
        keep = purge_and_embargo(rows, split=2, embargo_fraction=0.0)
        self.assertEqual(keep, [0, 1])

    def test_a_degenerate_split_does_not_raise(self):
        rows = [sig(0, 10)]
        self.assertEqual(purge_and_embargo(rows, split=0), [])
        self.assertEqual(purge_and_embargo(rows, split=5), [0])


# ===================================================================== Lopez de Prado ch.3
class TestTripleBarrier(unittest.TestCase):
    def test_profit_barrier_reached_first_is_a_win(self):
        self.assertEqual(triple_barrier_label(outcome_r=1.2, mfe_r=1.5, mae_r=-0.2), 1)

    def test_stop_barrier_reached_first_is_a_loss(self):
        self.assertEqual(triple_barrier_label(outcome_r=-1.0, mfe_r=0.1, mae_r=-1.4), 0)

    def test_neither_barrier_reached_falls_to_the_vertical_barrier(self):
        """Expiry: the holding period ran out, so the final result decides."""
        self.assertEqual(triple_barrier_label(outcome_r=0.3, mfe_r=0.5, mae_r=-0.4), 1)
        self.assertEqual(triple_barrier_label(outcome_r=-0.2, mfe_r=0.5, mae_r=-0.4), 0)

    def test_without_path_data_it_falls_back_to_the_sign_rule(self):
        self.assertEqual(triple_barrier_label(outcome_r=0.4, mfe_r=None, mae_r=None), 1)

    def test_it_reports_how_many_labels_the_change_actually_moved(self):
        """If the new labels agree with the old everywhere, the change bought nothing - say so."""
        rows = [{"outcome_r": 1.0, "mfe_r": 1.5, "mae_r": -0.1, **sig(0, 10)},
                {"outcome_r": -1.0, "mfe_r": 0.1, "mae_r": -1.2, **sig(20, 30)}]
        rep = label_report(rows)
        self.assertEqual(rep["labels_changed_vs_sign_rule"], 0)
        self.assertIn("effective_sample_size", rep)


# ===================================================================== Kissell ch.3
class TestCostModelAudit(unittest.TestCase):
    def test_an_honest_model_scores_about_zero(self):
        rows = [{"estimated_cost_r": 0.10, "actual_cost_r": 0.10 + (0.01 if i % 2 else -0.01),
                 "timing_risk": 0.05, "strategy_id": "s", "symbol": "X"} for i in range(60)]
        d = audit_cost_model(rows)
        self.assertEqual(d["verdict"], "HONEST")

    def test_a_model_that_overestimates_is_named_as_such(self):
        """The case that matters: the gate is refusing trades it should take."""
        rows = [{"estimated_cost_r": 0.30, "actual_cost_r": 0.10, "timing_risk": 0.05,
                 "strategy_id": "s", "symbol": "X"} for _ in range(60)]
        d = audit_cost_model(rows)
        self.assertEqual(d["verdict"], "OVERESTIMATES")
        self.assertIn("refusing trades it should take", d["what_to_do"])

    def test_a_model_that_underestimates_is_named_as_such(self):
        rows = [{"estimated_cost_r": 0.05, "actual_cost_r": 0.25, "timing_risk": 0.05,
                 "strategy_id": "s", "symbol": "X"} for _ in range(60)]
        self.assertEqual(audit_cost_model(rows)["verdict"], "UNDERESTIMATES")

    def test_it_refuses_to_judge_a_small_sample(self):
        rows = [{"estimated_cost_r": 0.3, "actual_cost_r": 0.1, "timing_risk": 0.05} for _ in range(4)]
        d = audit_cost_model(rows)
        self.assertEqual(d["verdict"], "TOO_FEW")
        self.assertFalse(d["enough_data"])

    def test_no_data_is_not_an_error(self):
        self.assertEqual(audit_cost_model([])["verdict"], "NO_DATA")

    def test_a_collapsed_denominator_is_reported_rather_than_believed(self):
        """Found by running the tool: near-identical costs drive the empirical timing risk toward zero,
        which inflates z into the hundreds and looks like overwhelming evidence. It is an artefact."""
        rows = [{"estimated_cost_r": 0.30, "actual_cost_r": 0.10, "timing_risk": 0.001,
                 "strategy_id": "s", "symbol": "X"} for _ in range(60)]
        d = audit_cost_model(rows)
        self.assertEqual(d["verdict"], "DENOMINATOR_TOO_SMALL")
        self.assertIn("collapsed", d["what_to_do"])

    def test_the_sign_follows_kissell_not_the_textbook(self):
        """Positive must mean BETTER than expected, so every number in the module reads the same way."""
        self.assertGreater(cost_zscore(0.3, 0.1, 0.05), 0)
        self.assertLess(cost_zscore(0.1, 0.3, 0.05), 0)

    def test_a_missing_denominator_yields_no_score_rather_than_a_wrong_one(self):
        self.assertIsNone(cost_zscore(0.3, 0.1, None))
        self.assertIsNone(cost_zscore(0.3, 0.1, 0.0))
        self.assertIsNone(timing_risk_r(None, 1.0, 5.0))


class TestExecutionBenchmarks(unittest.TestCase):
    def test_buying_above_the_midpoint_is_a_positive_cost(self):
        self.assertGreater(signed_bp(100.10, 100.00, "BUY"), 0)

    def test_selling_below_the_midpoint_is_also_a_positive_cost(self):
        """Sign must flip with side, or buys and sells cannot be pooled."""
        self.assertGreater(signed_bp(99.90, 100.00, "SELL"), 0)

    def test_basis_points_make_instruments_comparable(self):
        """The whole reason for not using points: 10 points of gold and of EURUSD are not the same."""
        gold = signed_bp(2000.20, 2000.00, "BUY")
        eur = signed_bp(1.10011, 1.10000, "BUY")
        self.assertAlmostEqual(gold, 1.0, places=2)
        self.assertAlmostEqual(eur, 1.0, places=2)

    def test_adverse_selection_is_effective_minus_realized(self):
        c = execution_cost(fill_price=100.10, direction="BUY", mid_at_fill=100.00,
                           mid_at_decision=99.95, mid_after=99.80)
        self.assertIsNotNone(c.adverse_selection_bp)
        self.assertAlmostEqual(c.adverse_selection_bp, c.effective_bp - c.realized_bp, places=6)

    def test_delay_is_shortfall_minus_effective(self):
        c = execution_cost(fill_price=100.10, direction="BUY", mid_at_fill=100.00, mid_at_decision=99.95)
        self.assertAlmostEqual(c.delay_bp, c.shortfall_bp - c.effective_bp, places=6)

    def test_missing_benchmarks_leave_gaps_rather_than_guesses(self):
        c = execution_cost(fill_price=100.0, direction="BUY")
        self.assertIsNone(c.effective_bp)
        self.assertIsNone(c.realized_bp)


# ===================================================================== Chan ch.2 / ch.3
class TestDrawdownDuration(unittest.TestCase):
    def test_a_monotonic_curve_has_no_drawdown_at_all(self):
        self.assertEqual(max_drawdown_duration([1, 2, 3, 4, 5]), 0)

    def test_it_counts_the_longest_underwater_stretch(self):
        # peak 5, then 4/3/2 are below it (3 trades), then 6 sets a new high and 5 is 1 trade below it
        curve = [5, 4, 3, 2, 6, 5, 7]
        self.assertEqual(max_drawdown_duration(curve), 3)

    def test_depth_and_duration_are_different_failures(self):
        """A shallow four-month drawdown ends more systems than a sharp one-week drop."""
        shallow_long = [10] + [9.9] * 50 + [11]
        deep_short = [10, 2, 11]
        self.assertGreater(max_drawdown_duration(shallow_long), max_drawdown_duration(deep_short))
        self.assertGreater(max_drawdown(deep_short), max_drawdown(shallow_long))

    def test_a_drawdown_still_open_at_the_end_counts(self):
        self.assertEqual(max_drawdown_duration([10, 9, 8, 7]), 3)


class TestParameterBudget(unittest.TestCase):
    def test_a_lean_strategy_passes(self):
        st = [{"id": "lean", "status": "DEMO", "params": {"a": 1.0, "b": 2.0}, "exit_model": {}}]
        d = parameter_audit(st)
        self.assertEqual(d["over_budget"], [])

    def test_too_many_free_parameters_is_flagged(self):
        st = [{"id": "fat", "status": "DEMO",
               "params": {f"p{i}": float(i) for i in range(9)}, "exit_model": {}}]
        d = parameter_audit(st)
        self.assertIn("fat", d["over_budget"])

    def test_a_tunable_range_makes_a_parameter_free_whatever_its_python_type(self):
        """Config values arrive as ParamSpec objects. A type check sees no numbers and reports zero,
        which is both wrong and reassuring - the worst combination."""
        class ParamSpec:
            def __init__(self, value, minimum=None, maximum=None):
                self.value, self.minimum, self.maximum = value, minimum, maximum

        st = [{"id": "spec", "status": "DEMO",
               "params": {f"p{i}": ParamSpec(1.0, 0.0, 2.0) for i in range(7)}, "exit_model": {}}]
        self.assertIn("spec", parameter_audit(st)["over_budget"])

    def test_a_fixed_setting_is_not_a_free_parameter(self):
        class ParamSpec:
            def __init__(self, value, minimum=None, maximum=None):
                self.value, self.minimum, self.maximum = value, minimum, maximum

        st = [{"id": "fixed", "status": "DEMO",
               "params": {f"p{i}": ParamSpec("M1") for i in range(9)}, "exit_model": {}}]
        self.assertEqual(parameter_audit(st)["over_budget"], [])


class TestLookAheadDetection(unittest.TestCase):
    class Frame:
        def __init__(self, c):
            self.c = list(c)
            self.o = self.h = self.l = self.c

    def test_an_honest_signal_builder_passes(self):
        frame = self.Frame(range(400))
        clean = lambda f: [1 if x % 3 == 0 else 0 for x in f.c]      # depends only on the bar itself
        d = check_frame(frame, clean, truncate=100)
        self.assertTrue(d["ok"], d.get("verdict"))

    def test_a_builder_that_peeks_at_the_future_is_caught(self):
        """Normalising by the whole series is the classic version of this bug."""
        frame = self.Frame(range(400))
        def leaky(f):
            top = max(f.c) or 1
            return [1 if x > top * 0.5 else 0 for x in f.c]
        d = check_frame(frame, leaky, truncate=100)
        self.assertFalse(d["ok"])
        self.assertIn("LOOK-AHEAD BIAS", d["verdict"])

    def test_too_little_data_reports_rather_than_pretending(self):
        self.assertFalse(check_frame(self.Frame(range(20)), lambda f: f.c, truncate=100)["ok"])

    def test_comparison_only_covers_the_overlap(self):
        d = compare_signals([1, 2, 3, 4, 5], [1, 2, 3], overlap=3)
        self.assertEqual(d["compared"], 3)
        self.assertTrue(d["clean"])


# ===================================================================== Lopez de Prado ch.15
class TestStrategyRisk(unittest.TestCase):
    def test_sharpe_rises_with_the_square_root_of_frequency(self):
        """Quadrupling trade count should roughly double Sharpe, not quadruple it."""
        a = sharpe(0.6, 250, -1.0, 1.0)
        b = sharpe(0.6, 1000, -1.0, 1.0)
        self.assertAlmostEqual(b / a, 2.0, places=6)

    def test_breakeven_precision_matches_the_payout_ratio(self):
        self.assertAlmostEqual(breakeven_precision(-1.0, 1.0), 0.5, places=6)
        self.assertAlmostEqual(breakeven_precision(-1.0, 0.672), 0.5981, places=3)

    def test_an_implausible_sharpe_is_called_out(self):
        """The 16-17 Oct record implies a Sharpe far beyond anything ever sustained. That is the finding."""
        trades = []
        for i in range(23):
            trades.append({"r_multiple": 0.672 if i >= 5 else -1.0,
                           "closed_at": (T0 + timedelta(hours=2 * i)).isoformat()})
        d = strategy_risk(trades, trades_per_year=3000)
        self.assertGreater(d["implied_annual_sharpe"], 10)
        self.assertIn("sample too small", d["plausibility"])

    def test_a_smaller_edge_needs_more_trades_to_prove(self):
        self.assertGreater(trades_needed(0.8, 0.05), trades_needed(0.8, 0.30))

    def test_an_empty_record_does_not_raise(self):
        self.assertEqual(strategy_risk([])["trades"], 0)


# ===================================================================== idle-time research
class TestIdleResearch(unittest.TestCase):
    SAT = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    WED = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

    def test_saturday_is_closed_and_wednesday_is_not(self):
        self.assertTrue(is_weekend_closed(self.SAT))
        self.assertFalse(is_weekend_closed(self.WED))

    def test_friday_evening_and_sunday_morning_are_closed(self):
        self.assertTrue(is_weekend_closed(datetime(2026, 10, 16, 22, tzinfo=timezone.utc)))
        self.assertTrue(is_weekend_closed(datetime(2026, 10, 18, 10, tzinfo=timezone.utc)))

    def test_a_long_window_earns_the_expensive_jobs(self):
        self.assertIn("ml_retrain", deep_research_plan(400))
        self.assertNotIn("ml_retrain", deep_research_plan(100))

    def test_the_cheap_audits_come_first_so_a_cut_short_window_still_pays(self):
        plan = deep_research_plan(2000)
        self.assertLess(plan.index("cost_model_audit"), plan.index("ml_retrain"))

    def test_it_does_not_run_while_the_markets_are_open(self):
        out = run_deep_research(None, None, now=self.WED, jobs={})
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "markets are open")

    def test_it_runs_over_the_weekend_and_records_what_it_did(self):
        calls = []

        def job(name):
            def fn(cfgm, repo, adapter):
                calls.append(name)
                return {"ok": True}
            return fn

        jobs = {n: job(n) for n in deep_research_plan(2000)}
        out = run_deep_research(None, None, now=self.SAT, jobs=jobs)
        self.assertTrue(out["ran"])
        self.assertEqual(calls, out["plan"])

    def test_one_failing_job_does_not_stop_the_rest(self):
        def boom(cfgm, repo, adapter):
            raise RuntimeError("no database")

        jobs = {n: boom for n in deep_research_plan(2000)}
        jobs["strategy_risk"] = lambda c, r, a: {"ok": True}
        out = run_deep_research(None, None, now=self.SAT, jobs=jobs)
        self.assertTrue(out["ran"])
        self.assertEqual(out["results"]["strategy_risk"], {"ok": True})
        self.assertIn("error", out["results"]["cost_model_audit"])

    def test_it_will_not_start_expensive_work_just_before_the_open(self):
        nearly_open = datetime(2026, 10, 18, 21, 30, tzinfo=timezone.utc)   # Sunday, 30 min to go
        out = run_deep_research(None, None, now=nearly_open, jobs={})
        self.assertFalse(out["ran"])
        self.assertIn("before open", out["reason"])


class TestIdleResearchOnlyMeasures(unittest.TestCase):
    def test_it_cannot_change_production(self):
        """A research pass that could retune live settings would make its own reports untrustworthy."""
        import ast
        import inspect
        from app.research.lab import idle
        forbidden = {"promote", "set_config", "set_flag", "order_send", "close_position",
                     "modify_sltp", "update"}
        called = set()
        for node in ast.walk(ast.parse(inspect.getsource(idle))):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name:
                    called.add(name)
        self.assertEqual(called & forbidden, set())


if __name__ == "__main__":
    unittest.main()
