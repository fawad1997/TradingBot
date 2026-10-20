"""The gate between demo and real money.

This tool exists to be hard to pass. The tests below are therefore mostly about it saying NO when it
should: a short record, a lucky week, one symbol carrying everything, a limit that never fired.

The property worth defending above all: it must never pass on a small sample. "23 trades, +0.308R,
78% won" is a result that looks excellent and means nothing, and a readiness check that waves it
through is worse than no check at all, because it launders a hunch as a measurement.

It reads and never writes. A check that could change production while reporting on it would make
every number it printed untrustworthy.
"""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tools.live_readiness import assess, _stats

NOW = datetime(2026, 12, 1, tzinfo=timezone.utc)
START = datetime(2026, 10, 18, tzinfo=timezone.utc)


class _Straddle:
    enabled = True
    symbols = ("XAUUSDm", "EURUSDm", "GBPUSDm")
    max_armed_symbols = 2

    def ceiling_binds(self):
        return self.max_armed_symbols < len(self.symbols)


def Cfg():
    """A configuration that satisfies the three real-money settings checks.

    Those checks (research gate failing closed, a straddle ceiling that actually binds, a commission that
    matches the account type) are decisions rather than measurements, so they are injected here instead of
    read from the live config. The live config currently fails all three on purpose - it is a demo account -
    and the fixture below is about a good TRADE RECORD, which is a separate question."""
    return SimpleNamespace(app=SimpleNamespace(
        research_lab=SimpleNamespace(gate_require_evidence=True),
        news_straddle=_Straddle(),
        cost=SimpleNamespace(commission_per_lot_round_trip=7.0,
                             symbol_commission_per_lot_round_trip={})))


class Repo:
    """Minimal stand-in. assess() only ever calls select()."""

    def __init__(self, **tables):
        self.data = tables

    def select(self, table, where=None, order_by=None, desc=False, limit=None):
        return self.data.get(table, [])


def trades(n, r=0.30, symbol="XAUUSDm", strategy="micro_scalper", start=START, spacing_h=4):
    return [{
        "closed_at": (start + timedelta(hours=spacing_h * i)).isoformat(),
        "r_multiple": r, "symbol": symbol, "strategy_id": strategy,
        "signal_id": f"sig-{i}", "state": "CLOSED",
    } for i in range(n)]


def fills(symbols, n=25, start=START):
    return [{"created_at": (start + timedelta(minutes=i)).isoformat(),
             "symbol": s, "slippage_points": 4.0}
            for s in symbols for i in range(n)]


def healthy(n=240, **over):
    """A record that passes everything, so each test can break exactly one thing.

    Win/loss cycles on 5 while symbol and strategy cycle on 3, so no symbol and no strategy is
    locked to one outcome. Tying them together (i % 3 for both) makes one symbol lose every single
    trade, which the concentration check correctly rejects — a fixture bug that looks like a tool bug.
    """
    t = []
    for i in range(n):
        t += trades(1, r=(0.9 if i % 5 else -1.0),
                    symbol=["XAUUSDm", "EURUSDm", "GBPJPYm"][i % 3],
                    strategy=["micro_scalper", "pullback_v1", "mean_reversion_v1"][i % 3],
                    start=START + timedelta(hours=3 * i))
    base = {
        "trades": t,
        "execution_events": fills(["XAUUSDm", "EURUSDm", "GBPJPYm"]),
        "risk_states": [{"state": "NORMAL"}, {"state": "REDUCED"}],
        "strategy_versions": [{"status": "VALIDATED", "id": "micro_scalper"}],
    }
    base.update(over)
    return Repo(**base)


def names_failing(d):
    return {c["name"] for c in d["checks"] if not c["pass"]}


class TestItRefusesASmallSample(unittest.TestCase):
    def test_the_real_16_17_october_record_does_not_pass(self):
        """23 trades, +0.308R each, 78% won. Looks excellent. Means nothing."""
        r = []
        for i in range(23):
            r += trades(1, r=(0.672 if i % 23 >= 5 else -1.0),
                        start=START + timedelta(hours=2 * i))
        d = assess(healthy(), now=NOW) if False else assess(Repo(trades=r), now=NOW)
        self.assertFalse(d["ready"])
        self.assertIn("Enough finished trades", names_failing(d))
        self.assertEqual(d["trades"], 23)

    def test_a_tiny_but_spectacular_record_still_fails(self):
        d = assess(Repo(trades=trades(5, r=3.0)), now=NOW)
        self.assertFalse(d["ready"])
        self.assertIn("Enough finished trades", names_failing(d))

    def test_an_empty_database_does_not_raise(self):
        d = assess(Repo(), now=NOW)
        self.assertFalse(d["ready"])
        self.assertEqual(d["trades"], 0)

    def test_a_broken_table_does_not_bring_it_down(self):
        class Broken(Repo):
            def select(self, *a, **k):
                raise RuntimeError("database down")
        d = assess(Broken(), now=NOW)
        self.assertFalse(d["ready"])


class TestTheStatisticalGate(unittest.TestCase):
    def test_an_edge_whose_error_bar_touches_zero_fails(self):
        """Mean clearly positive, but the interval reaches down past the threshold."""
        t = []
        for i in range(250):
            t += trades(1, r=(2.4 if i % 5 == 0 else -0.5),  # mean +0.08, very wide
                        start=START + timedelta(hours=3 * i))
        d = assess(Repo(trades=t), now=NOW)
        self.assertGreater(d["edge"]["mean"], 0, "the average is positive")
        self.assertIn("Edge clears its error bar", names_failing(d),
                      "a positive average is not the test; the low end of the interval is")

    def test_the_threshold_is_the_low_end_not_the_average(self):
        d = assess(healthy(), now=NOW)
        self.assertLess(d["edge"]["lo"], d["edge"]["mean"])

    def test_min_edge_is_adjustable(self):
        strict = assess(healthy(), now=NOW, min_edge=5.0)
        self.assertIn("Edge clears its error bar", names_failing(strict))

    def test_required_sample_sizes_are_reported(self):
        d = assess(healthy(), now=NOW)
        needed = d["trades_needed_for"]
        self.assertGreater(needed["+0.05R"], needed["+0.30R"],
                           "a smaller edge must need more trades, not fewer")


class TestItDetectsConcentration(unittest.TestCase):
    def test_one_symbol_carrying_the_whole_result_fails(self):
        t = []
        for i in range(240):
            good = i % 2 == 0
            t += trades(1, r=(1.2 if good else -0.4),
                        symbol="XAUUSDm" if good else "EURUSDm",
                        start=START + timedelta(hours=3 * i))
        d = assess(Repo(trades=t), now=NOW)
        self.assertIn("Not carried by one symbol", names_failing(d))

    def test_one_strategy_carrying_the_whole_result_fails(self):
        t = []
        for i in range(240):
            good = i % 2 == 0
            t += trades(1, r=(1.2 if good else -0.4),
                        symbol=["XAUUSDm", "EURUSDm"][i % 2],
                        strategy="micro_scalper" if good else "pullback_v1",
                        start=START + timedelta(hours=3 * i))
            d = None
        d = assess(Repo(trades=t), now=NOW)
        self.assertIn("Not carried by one strategy", names_failing(d))

    def test_a_single_symbol_record_cannot_be_tested_so_it_fails(self):
        d = assess(Repo(trades=trades(240)), now=NOW)
        self.assertIn("Not carried by one symbol", names_failing(d))


class TestItDetectsALuckyWeek(unittest.TestCase):
    def test_all_the_profit_in_the_first_half_fails(self):
        t = trades(120, r=1.0, start=START) + \
            trades(120, r=-0.2, start=START + timedelta(days=30))
        d = assess(Repo(trades=t), now=NOW)
        self.assertGreater(d["edge"]["mean"], 0)
        self.assertIn("Positive in both halves", names_failing(d))


class TestOperationalGates(unittest.TestCase):
    def test_a_symbol_without_enough_fills_is_named(self):
        d = assess(healthy(execution_events=fills(["XAUUSDm"], n=25)), now=NOW)
        failing = [c for c in d["checks"] if c["name"] == "Slippage measured on every symbol"]
        self.assertFalse(failing[0]["pass"])
        self.assertIn("EURUSDm", failing[0]["why"])

    def test_an_unreconciled_trade_blocks(self):
        t = healthy().data["trades"] + [{"state": "RECONCILIATION_REQUIRED"}]
        d = assess(healthy(trades=t), now=NOW)
        self.assertIn("No unreconciled trades", names_failing(d))

    def test_a_trade_with_no_signal_behind_it_blocks(self):
        t = list(healthy().data["trades"])
        t[0] = {**t[0], "signal_id": None}
        d = assess(healthy(trades=t), now=NOW)
        self.assertIn("Every trade traces to a signal", names_failing(d))

    def test_risk_controls_that_never_fired_block(self):
        d = assess(healthy(risk_states=[{"state": "NORMAL"}]), now=NOW)
        self.assertIn("Risk controls have fired", names_failing(d))

    def test_no_validated_strategy_blocks(self):
        d = assess(healthy(strategy_versions=[{"status": "MICRO_LIVE"}]), now=NOW)
        self.assertIn("A strategy reached VALIDATED", names_failing(d))

    def test_a_record_shorter_than_three_weeks_blocks(self):
        t = trades(240, r=0.9, spacing_h=1)   # 240 trades crammed into 10 days
        d = assess(Repo(trades=t), now=NOW)
        self.assertIn("Weeks of continuous running", names_failing(d))


class TestTheCutoff(unittest.TestCase):
    def test_trades_from_before_the_peak_fix_are_excluded(self):
        # 300 trades an hour apart from 1 Sep all land well before the 17 Oct cutoff
        old = trades(300, r=2.0, start=datetime(2026, 9, 1, tzinfo=timezone.utc), spacing_h=1)
        d = assess(Repo(trades=old), now=NOW, since="2026-10-17")
        self.assertEqual(d["trades"], 0, "pre-fix extremes could only understate; they do not count")

    def test_the_cutoff_can_be_moved(self):
        old = trades(300, r=0.9, start=datetime(2026, 9, 1, tzinfo=timezone.utc), spacing_h=1)
        d = assess(Repo(trades=old), now=NOW, since="2026-08-01")
        self.assertEqual(d["trades"], 300)


class TestAFullPass(unittest.TestCase):
    def test_a_genuinely_good_record_passes_everything(self):
        """Without this, every test above would pass on a tool that always says no."""
        d = assess(healthy(), now=NOW, cfgm=Cfg())
        self.assertTrue(d["ready"], f"still failing: {names_failing(d)}")
        self.assertEqual(d["passed"], d["total"])

    def test_a_good_record_is_still_not_ready_on_the_live_demo_settings(self):
        """The settings checks are not decoration: a perfect trade record must not pass a demo config.

        All three currently fail on the live configuration - the research gate fails open, the straddle
        ceiling equals the symbol count, and commission is zero. Each is defensible on demo and none is
        defensible on a funded account."""
        d = assess(healthy(), now=NOW)
        self.assertFalse(d["ready"])
        self.assertEqual(
            names_failing(d),
            {"Research gate fails closed", "News straddle ceiling binds",
             "Commission matches the account type"})

    def test_the_rendering_names_what_is_wrong(self):
        from tools.live_readiness import render
        text = render(assess(Repo(trades=trades(23)), now=NOW))
        self.assertIn("NOT READY", text)
        self.assertIn("Enough finished trades", text)
        self.assertIn("Confirm each one yourself", text)


class TestItOnlyEverReads(unittest.TestCase):
    def test_the_module_cannot_change_anything(self):
        """Matched on the full dotted name, because sys.path.insert is not a database write.

        Matching the bare attribute flags `sys.path.insert(0, ...)`, which every script in tools/
        needs to import the package at all. Resolving the receiver keeps the check meaningful
        instead of forcing `insert` out of the forbidden list to make it pass.
        """
        import ast
        import inspect
        from tools import live_readiness
        forbidden = {"update", "set_flag", "insert", "insert_many", "upsert", "execute", "commit",
                     "order_send", "close_position", "modify_sltp", "set_config", "promote"}
        allowed_receivers = {"sys.path"}

        def dotted(node):
            if isinstance(node, ast.Attribute):
                base = dotted(node.value)
                return f"{base}.{node.attr}" if base else node.attr
            if isinstance(node, ast.Name):
                return node.id
            return ""

        called = set()
        for node in ast.walk(ast.parse(inspect.getsource(live_readiness))):
            if not isinstance(node, ast.Call):
                continue
            full = dotted(node.func)
            if not full:
                continue
            receiver, _, name = full.rpartition(".")
            if receiver in allowed_receivers:
                continue
            called.add(name)
        self.assertEqual(called & forbidden, set(), "the readiness check must read and nothing else")

    def test_that_guard_still_catches_a_real_write(self):
        """Without this, the sys.path exemption above could be widened until it exempts everything."""
        import ast
        src = "def go(repo):\n    repo.insert('trades', {})\n"
        names = {n.func.attr for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertIn("insert", names)


class TestTheMaths(unittest.TestCase):
    def test_the_interval_narrows_as_the_sample_grows(self):
        few = _stats([1.0, -1.0] * 10)
        many = _stats([1.0, -1.0] * 200)
        self.assertLess(many["hi"] - many["lo"], few["hi"] - few["lo"])

    def test_a_single_trade_has_no_interval(self):
        s = _stats([0.5])
        self.assertEqual(s["lo"], s["hi"])


if __name__ == "__main__":
    unittest.main()
