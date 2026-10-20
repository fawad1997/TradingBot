"""Cost (account types, commission, slower triggers), better data (ticks, more history, related markets) and the
learned trade filter."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from app.config import MLFilterCfg, ResearchLabCfg
from app.core.types import Bar, Gate
from app.features.frame import FeatureFrame
from app.market.symbols import base_name, contains, lookup, resolve
from tests.helpers import eligible_route, make_signal, rows, started_core, trades


def random_m1(n=20000, seed=1):
    rng = np.random.default_rng(seed)
    t = 1_700_000_000 // 3600 * 3600 + np.arange(n) * 60
    c = 2000 + np.cumsum(rng.normal(0, 0.5, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.exponential(0.3, n)
    l = np.minimum(o, c) - rng.exponential(0.3, n)
    return FeatureFrame(t, o, h, l, c, np.full(n, 20.0), rng.integers(50, 200, n).astype(float), "M1")


class FakeAdapter:
    def __init__(self, names, closes=None):
        self.names = list(names)
        self.closes = closes or {}

    def select_symbol(self, s):
        return s in self.names

    def find_symbols(self, pattern):
        core = pattern.strip("*")
        return [s for s in self.names if core in s]

    def rates(self, symbol, tf, count):
        c = self.closes.get(symbol)
        if c is None:
            return []
        return [Bar(i * 900, x, x, x, x, 1, 0, 0) for i, x in enumerate(c[-count:])]


# ------------------------------------------------------------------ 1. lower cost
class TestAccountTypeNames(unittest.TestCase):
    def test_base_names(self):
        self.assertEqual(base_name("XAUUSDm"), "XAUUSD")
        self.assertEqual(base_name("XAUUSD247m"), "XAUUSD247")
        self.assertEqual(base_name("EURUSD.raw"), "EURUSD")
        self.assertEqual(base_name("XAUUSD"), "XAUUSD")

    def test_standard_config_finds_raw_account_symbols(self):
        raw = FakeAdapter(["XAUUSD", "XAUUSD247", "BTCUSD"])
        self.assertEqual(resolve(raw, "XAUUSDm"), "XAUUSD")
        self.assertEqual(resolve(raw, "XAUUSD247m"), "XAUUSD247")      # not confused with XAUUSD
        self.assertIsNone(resolve(raw, "NVDAm"))

    def test_per_symbol_settings_follow_the_instrument(self):
        self.assertEqual(lookup({"BTCUSDm": 1000}, "BTCUSD", 300), 1000)
        self.assertEqual(lookup({"BTCUSDm": 1000}, "ETHUSD", 300), 300)
        self.assertTrue(contains(["XAUUSD247m"], "XAUUSD247"))
        self.assertFalse(contains(["XAUUSD247m"], "XAUUSD"))


class TestCommission(unittest.TestCase):
    def test_per_symbol_commission_wins_over_default(self):
        core, *_ = started_core(overrides={"cost.commission_per_lot_round_trip": 7.0,
                                           "cost.symbol_commission_per_lot_round_trip": {"XAUUSDm": 16.0}})
        self.assertEqual(core.commission_rt(), 16.0)          # simulator symbol XAUUSD matches XAUUSDm

    def test_lab_converts_commission_to_price(self):
        from app.research.lab.worker import _commission_price
        cfg = SimpleNamespace(cost=SimpleNamespace(symbol_commission_per_lot_round_trip={},
                                                   commission_per_lot_round_trip=7.0))
        spec = SimpleNamespace(tick_value=1.0, tick_size=0.01)       # $1 per 0.01 per lot -> $100 per 1.00
        self.assertAlmostEqual(_commission_price(cfg, "XAUUSD", spec), 0.07)


class TestSlowerTrigger(unittest.TestCase):
    def test_trigger_timeframe_selects_frames(self):
        from app.strategies.micro_scalper import MicroScalper
        v = SimpleNamespace(m1="m1", j=1, m15="m15", i=2, h1="h1", k=3, h4="h4", q=4)
        s = MicroScalper.__new__(MicroScalper)
        for tf, want in (("M1", ("m1", 1, "m15", 2, "M15")), ("M15", ("m15", 2, "h1", 3, "H1")),
                         ("H1", ("h1", 3, "h4", 4, "H4"))):
            s.params = {"trigger_timeframe": tf}
            self.assertEqual(s._frames(v), want)

    def test_gate_follows_the_live_trigger_timeframe(self):
        core, *_ = started_core()
        core.cfgm.strategy("micro_scalper").params["trigger_timeframe"].value = "M15"
        bad = {"verdict": "NOT_ROBUST_ON_TEST", "train": {"n_trades": 100, "expectancy_r": -1.0},
               "test": {"n_trades": 40, "expectancy_r": -1.0}}
        good = {"verdict": "ROBUST_ON_TEST", "train": {"n_trades": 100, "expectancy_r": 0.2},
                "test": {"n_trades": 40, "expectancy_r": 0.2}}
        core.lab_hint = {**bad, "timeframes": {"M15": good}}
        core.lab_hint_created_at = (core.now_utc() - timedelta(hours=1)).isoformat()
        state = core.research_gate()
        self.assertEqual(state["timeframe"], "M15")
        self.assertFalse(state["blocked"])                      # M1 is bad, but the bot trades M15
        core.cfgm.strategy("micro_scalper").params["trigger_timeframe"].value = "M1"
        self.assertTrue(core.research_gate()["blocked"])


# ------------------------------------------------------------------ 2. research lab
class TestLabData(unittest.TestCase):
    def test_resample(self):
        from app.research.lab.data import resample
        m1 = random_m1(60)
        m15, sp = resample(m1, 900, np.arange(60.0))
        self.assertEqual(len(m15), 4)
        self.assertEqual(m15.o[1], m1.o[15])
        self.assertEqual(m15.c[1], m1.c[29])
        self.assertEqual(m15.h[1], m1.h[15:30].max())
        self.assertEqual(m15.tick_volume[1], m1.tick_volume[15:30].sum())
        self.assertEqual(sp[1], 15.0)

    def test_tick_open_spreads(self):
        from app.research.lab.data import tick_open_spreads
        now = datetime.now(timezone.utc)
        minute = int(now.timestamp()) // 60 * 60 - 3600
        bars = np.array([minute, minute + 60, minute + 120])
        tms = np.array([minute * 1000 + 5, minute * 1000 + 900, (minute + 120) * 1000 + 1])
        adapter = SimpleNamespace(ticks_array=lambda s, a, b: (tms, np.array([1.0, 1.0, 1.0]),
                                                               np.array([1.3, 1.9, 1.5]))
                                  if a.timestamp() <= minute <= b.timestamp() else None)
        out = tick_open_spreads(adapter, "X", 0.0, 2, bars)
        self.assertAlmostEqual(out[0], 0.3)                    # FIRST tick of the bar, not the widest
        self.assertTrue(np.isnan(out[1]))
        self.assertAlmostEqual(out[2], 0.5)

    def test_fast_simulator_agrees_with_vectorbt(self):
        from app.research.lab.data import resample
        from app.research.lab.fastsim import grid_train_test
        from app.research.lab.signals import micro_signals
        from app.research.lab.vbt_sweep import run_sweep
        m1 = random_m1()
        m15, _ = resample(m1, 900)
        sig = micro_signals(m1, m15, {"minimum_relative_volume": 1.0})
        v = run_sweep(m1, sig, 0.01, [1.0, 3.0], [1.0, 2.0], 0.7, 30)
        f = grid_train_test(m1.o, m1.h, m1.l, m1.c, sig["long"], sig["short"], sig["atr"], [1.0, 3.0], [1.0, 2.0],
                            0.7, 30, np.full(len(m1), 0.2), 0.0)
        for a, b in zip(v["combos"], f["combos"]):
            self.assertLess(abs(a["train"]["n_trades"] - b["train"]["n_trades"]), 0.1 * a["train"]["n_trades"])
            self.assertLess(abs(a["train"]["expectancy_r"] - b["train"]["expectancy_r"]), 0.12)

    def test_commission_lowers_results(self):
        from app.research.lab.fastsim import simulate
        m1 = random_m1(5000)
        longs = np.zeros(len(m1), bool)
        longs[100::50] = True
        atr = np.full(len(m1), 1.0)
        a = simulate(m1.o, m1.h, m1.l, m1.c, longs, ~longs & False, atr, 2.0, 1.0, None, 0.0)
        b = simulate(m1.o, m1.h, m1.l, m1.c, longs, ~longs & False, atr, 2.0, 1.0, None, 0.2)
        self.assertAlmostEqual(np.mean(a) - np.mean(b), 0.1, places=6)     # 0.2 price / 2.0 risk

    def test_replay_uses_the_live_stop_floor(self):
        # quiet market: ATR far below the spread. Without the floor every trade pays several R in spread - trades the
        # live strategy never takes, because it never sets a stop closer than min_stop_spread_multiple spreads.
        from app.research.lab.data import resample
        from app.research.lab.fastsim import simulate
        from app.research.lab.signals import micro_signals
        from app.research.lab.vbt_sweep import run_sweep
        m1 = random_m1(8000)
        m15, _ = resample(m1, 900)
        sig = micro_signals(m1, m15, {"minimum_relative_volume": 1.0})
        atr = np.full(len(m1), 0.05)
        spread = np.full(len(m1), 0.5)
        bare = simulate(m1.o, m1.h, m1.l, m1.c, sig["long"], sig["short"], atr, 1.0, 1.0, spread)
        floored = simulate(m1.o, m1.h, m1.l, m1.c, sig["long"], sig["short"], atr, 1.0, 1.0, spread,
                           min_stop_spread_multiple=10.0)
        self.assertLess(np.mean(bare), -5.0)
        self.assertGreater(np.mean(floored), -1.2)
        self.assertTrue(all(r >= -1.11 for r in floored))      # cost is at most 1/10 R
        v = run_sweep(m1, {**sig, "atr": atr}, 0.01, [1.0], [1.0], 0.7, 10, spread, 0.0, 10.0)
        self.assertGreater(v["combos"][0]["train"]["expectancy_r"], -1.2)

    def test_symbol_report_has_timeframes_costs_and_walk_forward(self):
        from app.research.lab.data import resample
        from app.research.lab.store import _compact_symbol
        from app.research.lab.worker import analyse_symbol
        m1 = random_m1()
        m15, _ = resample(m1, 900)
        cfg = ResearchLabCfg(optuna_trials=10, optuna_folds=2, run_backtrader=False)
        e = _compact_symbol(analyse_symbol("T", m1, m15, 0.01, cfg, {"minimum_relative_volume": 1.0}, None, 0.02))
        self.assertEqual(set(e["timeframes"]), {"M1", "M5", "M15"})
        self.assertIn(e["timeframes"]["M1"]["optuna"]["verdict"], ("WALK_FORWARD_POSITIVE", "WALK_FORWARD_NEGATIVE"))
        self.assertEqual(e["costs"]["commission_price_round_trip"], 0.02)
        self.assertIn("test", e["timeframes"]["M5"])


# ------------------------------------------------------------------ 3. related markets
class TestContext(unittest.TestCase):
    def cfg(self):
        from app.config import ContextCfg
        return SimpleNamespace(context=ContextCfg(refresh_seconds=15.0))

    def test_synthetic_dollar_index_when_broker_has_no_dxy(self):
        from app.market.context import ContextFeed
        up = list(np.linspace(1.10, 1.08, 40))                  # EURUSD falling -> dollar rising
        adapter = FakeAdapter(["EURUSDm", "USDJPYm", "GBPUSDm"],
                              {"EURUSDm": up, "USDJPYm": [150.0] * 40, "GBPUSDm": [1.30] * 40})
        f = ContextFeed(adapter, self.cfg()).features()
        self.assertEqual(f["usd_index_source"], "synthetic")
        self.assertGreater(f["usd_index_ret_4h_pct"], 0)
        self.assertNotIn("volatility_ret_1h_pct", f)            # no VIX on this account: simply absent

    def test_listed_symbol_is_used(self):
        from app.market.context import ContextFeed
        adapter = FakeAdapter(["US500m"], {"US500m": list(np.linspace(5000, 5100, 40))})
        f = ContextFeed(adapter, self.cfg()).features()
        self.assertEqual(f["us_stocks_source"], "US500m")
        self.assertGreater(f["us_stocks_z20"], 0)

    def test_signals_carry_context_and_simulator_does_not_break(self):
        core, *_ = started_core()
        sig = make_signal(core)
        core._process_signal(sig, eligible_route(), core.cfgm.strategy("pullback_v1"), core.last_tick)
        self.assertIsInstance(sig.meta.get("stored_features"), dict)


# ------------------------------------------------------------------ 4. learned filter
def synthetic_rows(n=1500, informative=True, seed=3):
    rng = np.random.default_rng(seed)
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        rv, frac = rng.uniform(0.5, 3), rng.uniform(0.02, 0.3)
        edge = (0.6 * (rv > 1.8) - 2 * frac) if informative else 0.0
        out.append({"id": str(i), "created_at": t0 + timedelta(minutes=17 * i), "direction": "BUY",
                    "regime": "TRANSITION", "session": "LONDON", "quality": 0.5,
                    "features": {"relative_tick_volume": rv, "spread_fraction_of_stop": frac, "noise": rng.normal(),
                                 "vision_shadow_grade": "A", "ml_score": 0.9},
                    "outcome_r": 1.0 if rng.random() < 0.5 + 0.3 * edge else -1.0})
    return out


class TestLearnedFilter(unittest.TestCase):
    def test_learns_a_real_pattern(self):
        from app.ml.filter import train_strategy
        st = train_strategy(synthetic_rows(), MLFilterCfg())
        self.assertEqual(st["status"], "VALIDATED")
        self.assertGreater(st["test"]["accepted_r"], st["test"]["baseline_r"])

    def test_does_not_validate_noise(self):
        from app.ml.filter import train_strategy
        for seed in (3, 4, 5):
            st = train_strategy(synthetic_rows(informative=False, seed=seed), MLFilterCfg())
            self.assertFalse(st["validated"], st.get("test"))

    def test_never_learns_from_after_the_fact_fields(self):
        from app.ml.filter import flatten
        f = flatten({"vision_shadow_grade": "A", "ml_score": 0.9, "research_lab": {"verdict": "x"}, "c": 2000.0,
                     "relative_tick_volume": 1.5, "context": {"usd_index_z20": 1.2}}, "BUY", "RANGE", "ASIA")
        self.assertNotIn("ml_score", f)
        self.assertNotIn("c", f)
        self.assertEqual(f["relative_tick_volume"], 1.5)
        self.assertEqual(f["context.usd_index_z20"], 1.2)
        self.assertEqual(f["regime=RANGE"], 1.0)

    def test_too_little_data(self):
        from app.ml.filter import train_strategy
        st = train_strategy(synthetic_rows(200), MLFilterCfg())
        self.assertEqual(st["status"], "INSUFFICIENT_DATA")

    def test_outcomes_join_trades_and_counterfactuals(self):
        from app.ml.filter import load_rows
        from app.storage.repo import MemoryRepo
        repo = MemoryRepo()
        now = datetime.now(timezone.utc)
        for i in range(3):
            repo.insert("signals", {"id": f"s{i}", "created_at": now - timedelta(hours=3 - i),
                                    "strategy_id": "micro_scalper", "features": {}})
        repo.insert("trades", {"id": "t", "signal_id": "s0", "state": "CLOSED", "r_multiple": 1.2})
        repo.insert("counterfactuals", {"id": "c", "signal_id": "s1", "status": "RESOLVED", "hypothetical_r": -1.0})
        got = load_rows(repo, "micro_scalper", 30)
        self.assertEqual([(r["id"], r["outcome_r"]) for r in got], [("s0", 1.2), ("s1", -1.0)])

    def _core_with_model(self, validated, mode="auto"):
        from app.ml.filter import LiveFilter, save, train_strategy
        st = train_strategy(synthetic_rows(), MLFilterCfg())
        st["validated"] = validated
        root = Path(tempfile.mkdtemp())
        save("micro_scalper", st, root)
        core, *_ = started_core(overrides={"ml_filter.mode": mode})
        core._ml = LiveFilter(root)
        return core

    def _scalper_signal(self, core, rv, frac):
        sig = make_signal(core)
        sig.strategy_id = "micro_scalper"
        sig.plan.features.update({"relative_tick_volume": rv, "spread_fraction_of_stop": frac})
        return sig

    def test_validated_model_blocks_weak_signals(self):
        core = self._core_with_model(True)
        sig = self._scalper_signal(core, 0.6, 0.29)
        core._process_signal(sig, eligible_route("micro_scalper"), core.cfgm.strategy("micro_scalper"),
                             core.last_tick)
        rej = rows(core, "rejections", signal_id=sig.id)
        self.assertTrue(rej and rej[-1]["gate"] == str(Gate.ML_FILTER), rej)
        self.assertEqual(sig.plan.features["ml_mode"], "ENFORCED")

    def test_unvalidated_model_only_records_its_score(self):
        core = self._core_with_model(False)
        sig = self._scalper_signal(core, 0.6, 0.29)
        core._process_signal(sig, eligible_route("micro_scalper"), core.cfgm.strategy("micro_scalper"),
                             core.last_tick)
        self.assertFalse([r for r in rows(core, "rejections", signal_id=sig.id) if r["gate"] == str(Gate.ML_FILTER)])
        self.assertEqual(sig.plan.features["ml_mode"], "SHADOW")
        self.assertIn("ml_score", sig.plan.features)


if __name__ == "__main__":
    unittest.main()
