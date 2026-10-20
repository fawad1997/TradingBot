"""Replaying different stops and targets over trades already taken.

The property defended hardest here is the REFUSAL to simulate what the data cannot support. Recorded
excursions end when a trade closes, so for anything stopped out there is no record of where price went
next - and "wider stops would have worked" is the most tempting wrong conclusion available from this
record. A tool that answered it would be inventing the answer.

Second: mfe and mae do not say which came first, so every result carries two bounds. A conclusion counts
only when it survives the worse ordering.
"""
import datetime as dt
import unittest

from tools.exit_geometry import (DEFAULT_STOPS, MAX_SIMULABLE_STOP, evaluate, grid, load, render,
                                 replay)

NOW = dt.datetime(2026, 10, 19, tzinfo=dt.timezone.utc)


def t(mfe, mae, r, hours=0, sid="x"):
    return {"mfe": mfe, "mae": -abs(mae), "r": r, "strategy": sid,
            "when": NOW - dt.timedelta(hours=hours)}


def row(mfe, mae, r, hours=0, state="CLOSED"):
    return {"state": state, "strategy_id": "x", "mfe_r": mfe, "mae_r": mae, "r_multiple": r,
            "closed_at": (NOW - dt.timedelta(hours=hours)).isoformat()}


class TestReplayOneTrade(unittest.TestCase):
    def test_a_target_the_trade_reached_is_taken(self):
        self.assertAlmostEqual(replay(t(0.80, 0.20, 0.5), stop=1.0, target=0.6, optimistic=True), 0.6)

    def test_a_target_it_never_reached_is_not(self):
        self.assertAlmostEqual(replay(t(0.40, 0.20, 0.3), stop=1.0, target=0.6, optimistic=True), 0.3)

    def test_a_stop_it_reached_costs_a_full_unit(self):
        self.assertAlmostEqual(replay(t(0.10, 1.00, -1.0), stop=0.8, target=1.5, optimistic=True), -1.0)

    def test_tightening_the_stop_rescales_the_reward(self):
        """Halving the risk doubles the R value of the same price move - not just smaller losses."""
        self.assertAlmostEqual(replay(t(0.80, 0.20, 0.5), stop=0.5, target=0.6, optimistic=True), 1.2)

    def test_when_both_barriers_were_reachable_the_bounds_differ(self):
        both = t(0.80, 0.90, -1.0)
        self.assertGreater(replay(both, 0.8, 0.6, optimistic=True), 0)
        self.assertLess(replay(both, 0.8, 0.6, optimistic=False), 0)

    def test_an_untouched_trade_keeps_its_real_outcome(self):
        self.assertAlmostEqual(replay(t(0.30, 0.30, 0.25), stop=1.0, target=1.5, optimistic=True), 0.25)


class TestBounds(unittest.TestCase):
    def test_agreement_is_reported_as_conclusive(self):
        trades = [t(0.9, 0.1, 0.8, i) for i in range(20)]
        self.assertEqual(evaluate(trades, 1.0, 0.5)["verdict"], "POSITIVE_BOTH_WAYS")

    def test_clear_losses_are_conclusive_too(self):
        trades = [t(0.05, 1.1, -1.0, i) for i in range(20)]
        self.assertEqual(evaluate(trades, 1.0, 0.5)["verdict"], "NEGATIVE_BOTH_WAYS")

    def test_disagreement_is_admitted_rather_than_averaged(self):
        """The case where the data genuinely cannot answer."""
        trades = [t(0.9, 1.0, -1.0, i) for i in range(20)]
        self.assertEqual(evaluate(trades, 0.9, 0.6)["verdict"], "DEPENDS_ON_ORDER")

    def test_the_pessimistic_bound_is_never_the_kinder_one(self):
        trades = [t(0.8, 0.9, -0.5, i) for i in range(30)]
        r = evaluate(trades, 0.85, 0.7)
        self.assertLessEqual(r["pessimistic"]["mean_r"], r["optimistic"]["mean_r"])

    def test_no_trades_is_reported_not_crashed(self):
        self.assertEqual(evaluate([], 1.0, 1.0), {"n": 0})


class TestItRefusesWhatItCannotKnow(unittest.TestCase):
    """The most important behaviour in this file."""

    def test_wider_stops_are_never_simulated(self):
        trades = [t(0.5, 0.9, -1.0, i) for i in range(40)]
        d = grid(trades, stops=(0.5, 1.0, 1.5, 2.0), targets=(0.6,))
        self.assertTrue(all(r["stop"] <= MAX_SIMULABLE_STOP for r in d["rows"]),
                        "a stop wider than the recorded path cannot be replayed and must be skipped")

    def test_the_default_grid_contains_no_unsimulable_stop(self):
        self.assertTrue(all(s <= MAX_SIMULABLE_STOP for s in DEFAULT_STOPS))

    def test_the_output_says_so_in_words(self):
        trades = [t(0.5, 0.9, -1.0, i) for i in range(40)]
        self.assertIn("WIDER stops", render(grid(trades, targets=(0.6, 1.0))))


class TestLoad(unittest.TestCase):
    def test_trades_without_excursions_are_dropped(self):
        rows = [row(0.5, 0.5, 0.2), {"state": "CLOSED", "r_multiple": 0.2, "closed_at": NOW.isoformat()}]
        self.assertEqual(len(load(rows)), 1)

    def test_open_trades_are_dropped(self):
        self.assertEqual(len(load([row(0.5, 0.5, 0.2, state="OPEN")])), 0)

    def test_mae_is_normalised_to_negative_either_way(self):
        self.assertLess(load([row(0.5, 0.9, -1.0)])[0]["mae"], 0)
        self.assertLess(load([row(0.5, -0.9, -1.0)])[0]["mae"], 0)

    def test_results_are_ordered_oldest_first_so_the_split_is_in_time(self):
        rows = [row(0.5, 0.5, 0.2, hours=1), row(0.6, 0.5, 0.3, hours=50)]
        self.assertLess(load(rows)[0]["when"], load(rows)[1]["when"])


class TestGrid(unittest.TestCase):
    def _trades(self, n=100):
        out = []
        for i in range(n):
            out.append(t(0.9, 0.2, 0.6, n - i) if i % 2 else t(0.1, 1.0, -1.0, n - i))
        return out

    def test_it_splits_in_time_and_holds_some_back(self):
        d = grid(self._trades())
        self.assertGreater(d["train"], 0)
        self.assertGreater(d["test"], 0)
        self.assertEqual(d["train"] + d["test"], d["n_trades"])

    def test_a_survivor_must_be_positive_on_both_halves(self):
        for r in grid(self._trades())["survivors"]:
            with self.subTest(stop=r["stop"], target=r["target"]):
                self.assertGreater(r["train"]["pessimistic"]["mean_r"], 0)
                self.assertGreater(r["test"]["pessimistic"]["mean_r"], 0)

    def test_a_hopeless_record_produces_no_survivors(self):
        """Entries with no edge must not be rescued by any geometry the tool will endorse."""
        bad = [t(0.2, 1.0, -1.0, i) for i in range(100)]
        self.assertEqual(grid(bad)["survivors"], [])
        self.assertIn("NOTHING SURVIVES", render(grid(bad)))

    def test_survivors_are_labelled_as_candidates_not_settings(self):
        text = render(grid(self._trades()))
        if "NOTHING SURVIVES" not in text:
            self.assertIn("RESEARCH, not settings to adopt", text)

    def test_an_empty_record_is_explained_not_crashed(self):
        self.assertIn("nothing can be replayed", render(grid([])))


if __name__ == "__main__":
    unittest.main()
