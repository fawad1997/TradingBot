"""One exit rule everywhere: what research simulates is what the trade manager does.

Until October 2026 the profit lock (lock 70% of the peak once a trade is 0.5R up) and breakeven existed only in the
live trade manager. The lab, the backtester and the counterfactual tracker judged a different exit, and the
selector pooled the two. These tests pin the shared rule, its live behaviour, the switch that lets trend
following run without it, and the daily trend strategy against the research rule it calls.
"""
import inspect
import random
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from app.alpha.exits import (OpenTradeState, SimulatedTrade, profit_lock_enabled, profit_lock_target,
                             trail_candidate, with_execution_exits)
from app.config import ConfigManager
from app.core.types import Side, Tick
from app.features.frame import FeatureFrame


def _old_ladder(peak, small_activation, small_floor, ambition_activation, ambition_floor, giveback, previous,
                was_active, base_floor):
    """The inline ladder TradeManager.on_tick carried before it moved to exits.py - verbatim logic."""
    if ambition_activation > 0 and peak >= ambition_activation:
        if giveback > 0:
            lock_target = max(ambition_floor, peak * (1.0 - giveback))
        else:
            lock_target = max(ambition_floor, peak - (ambition_activation - ambition_floor))
        lock_active = True
    elif small_activation > 0 and peak >= small_activation:
        lock_target = max(small_floor, peak * (1.0 - giveback)) if giveback > 0 else small_floor
        lock_active = True
    else:
        lock_target = base_floor
        lock_active = was_active
    return lock_active, max(lock_target, previous or 0.0)


class TestSharedLadder(unittest.TestCase):
    def test_it_matches_the_old_inline_code_exactly(self):
        rng = random.Random(7)
        for _ in range(5000):
            sa = rng.choice([0.0, 0.2, 2.5, 9.48])
            args = (rng.uniform(-5, 40), sa, rng.uniform(0, 3), rng.choice([0.0, 0.9, 15.18]), rng.uniform(0, 8),
                    rng.choice([0.0, 0.3, 0.9]), rng.choice([None, 0.0, rng.uniform(0, 20)]),
                    rng.choice([True, False]), rng.choice([0.0, 0.05]))
            self.assertEqual(profit_lock_target(*args[:6], previous_target=args[6], was_active=args[7],
                                                base_floor=args[8]), _old_ladder(*args))

    def test_the_live_manager_calls_it_and_honours_the_switch(self):
        from app.execution.trade_manager import TradeManager
        src = inspect.getsource(TradeManager.on_tick)
        self.assertIn("profit_lock_target(", src)
        self.assertIn("profit_lock_enabled(trade.get(\"exit_model\"))", src)


class TestTradedExitModel(unittest.TestCase):
    def setUp(self):
        self.ex = ConfigManager().app.execution

    def test_the_account_levels_are_copied_in_R(self):
        m = with_execution_exits({"type": "fixed"}, self.ex)
        self.assertTrue(m["profit_lock"]["enabled"])
        self.assertEqual(m["profit_lock"]["small_win_activation_r"], self.ex.small_win_activation_r)
        self.assertEqual(m["profit_lock"]["giveback"], self.ex.profit_giveback_fraction)
        self.assertTrue(m["breakeven"]["enabled"], "the manager applies breakeven when a model is silent")

    def test_a_strategy_can_switch_the_lock_off_and_its_model_is_not_mutated(self):
        own = {"type": "chandelier", "profit_lock": {"enabled": False}, "breakeven": {"enabled": False}}
        m = with_execution_exits(own, self.ex)
        self.assertFalse(m["profit_lock"]["enabled"])
        self.assertFalse(m["breakeven"]["enabled"])
        self.assertNotIn("small_win_activation_r", own["profit_lock"])
        self.assertFalse(profit_lock_enabled(m))
        self.assertTrue(profit_lock_enabled({}), "trades opened before the switch keep their lock")

    def test_the_engine_attaches_it_to_every_plan(self):
        from app.core.engine import TradingCore
        self.assertIn("with_execution_exits(res.plan.exit_model", inspect.getsource(TradingCore))


def _walk(sim, bars, spread=0.0):
    for o, h, l, c in bars:
        res = sim.step(o, h, l, c, spread)
        if res is not None:
            return res
    return None


class TestSimulatedExits(unittest.TestCase):
    LOCK = {"enabled": True, "small_win_activation_r": 0.5, "small_win_floor_r": 0.1, "ambition_activation_r": 0.8,
            "ambition_floor_r": 0.4, "giveback": 0.3, "cost_multiple": 1.5}

    def test_a_lock_armed_by_an_earlier_peak_closes_at_seventy_percent_of_it(self):
        sim = SimulatedTrade(Side.BUY, 100.0, 99.0, 103.0, {"type": "fixed", "profit_lock": self.LOCK}, 0, 0.0, 0.0)
        res = _walk(sim, [(100.0, 101.0, 100.4, 100.9), (100.9, 100.95, 100.2, 100.3)])
        self.assertEqual(res.exit_reason, "PROFIT_LOCK")
        self.assertAlmostEqual(res.r_multiple, 0.7, places=6)        # peak 1.0R, give back 30%

    def test_a_close_below_the_new_level_means_it_was_crossed_inside_the_bar(self):
        sim = SimulatedTrade(Side.BUY, 100.0, 99.0, 103.0, {"type": "fixed", "profit_lock": self.LOCK}, 0, 0.0, 0.0)
        res = _walk(sim, [(100.0, 101.0, 100.4, 100.5)])               # high first (a close is after the high)
        self.assertEqual(res.exit_reason, "PROFIT_LOCK")
        self.assertAlmostEqual(res.r_multiple, 0.7, places=6)

    def test_breakeven_moves_the_stop_once_half_a_risk_unit_is_reached(self):
        model = {"type": "fixed", "breakeven": {"enabled": True, "activate_r": 0.5, "cover_spread": True}}
        sim = SimulatedTrade(Side.BUY, 100.0, 99.0, 103.0, model, 0, 0.0, 0.02)
        res = _walk(sim, [(100.0, 100.6, 100.1, 100.4), (100.4, 100.45, 99.5, 99.6)])
        self.assertEqual(res.exit_reason, "BREAKEVEN")
        self.assertAlmostEqual(res.r_multiple, 0.02, places=6)        # entry + the spread it covers

    def test_sell_trades_lock_on_the_ask(self):
        sim = SimulatedTrade(Side.SELL, 100.0, 101.0, 97.0, {"type": "fixed", "profit_lock": self.LOCK}, 0, 0.0, 0.0)
        res = _walk(sim, [(100.0, 100.1, 99.0, 99.1), (99.1, 99.8, 99.05, 99.7)])
        self.assertEqual(res.exit_reason, "PROFIT_LOCK")
        self.assertAlmostEqual(res.r_multiple, 0.7, places=6)

    def test_a_model_without_the_keys_is_still_a_plain_bracket(self):
        sim = SimulatedTrade(Side.BUY, 100.0, 99.0, 102.0, {"type": "fixed"}, 0, 0.0, 0.0)
        res = _walk(sim, [(100.0, 101.0, 100.4, 100.9), (100.9, 100.95, 98.9, 99.0)])
        self.assertEqual(res.exit_reason, "SL")
        self.assertAlmostEqual(res.r_multiple, -1.0, places=6)

    def test_a_fixed_trail_distance_ignores_the_m15_atr(self):
        st = OpenTradeState(Side.BUY, 100.0, 94.0, 94.0, None, 1.0, 1.0, 0)
        st.observe(110.0, 100.0)
        ff = FeatureFrame(np.arange(30) * 900, np.full(30, 110.0), np.full(30, 110.2), np.full(30, 109.8),
                          np.full(30, 110.0), np.zeros(30), np.ones(30), "M15")
        cand = trail_candidate(st, {"type": "chandelier", "trail": {"activate_r": 0.0, "distance_price": 6.0}}, ff, 29)
        self.assertAlmostEqual(cand, 104.0)


class TestBacktesterUsesEachPlansExit(unittest.TestCase):
    """The backtester traded every plan with the strategy's STATIC exit model, so the trend strategy's per-trade
    trail distance was ignored and its trades got an M15 trail (the first lab run showed them all near 0R)."""

    def test_a_per_trade_trail_distance_is_honoured(self):
        from unittest import mock
        from app.config import StrategyCfg
        from app.core.types import TradePlan
        from app.research.backtester import BTSettings, run_backtest
        from app.strategies import registry
        from app.strategies.base import EvalResult, Strategy

        class OnePlan(Strategy):
            def lookbacks(self):
                return {"M15": 20}

            def evaluate(self, v):
                if v.i != 100:
                    return EvalResult(None, ["NO_SETUP"])
                e = float(v.ask)
                return EvalResult(TradePlan(Side.BUY, e, e - 5.0, e + 500.0,
                                            {"type": "chandelier", "target_r": None,
                                             "trail": {"activate_r": 0.0, "distance_price": 5.0}},
                                            setup_key="one", ttl_seconds=900), [])
        n = 400
        c = np.r_[np.full(101, 2000.0), 2000.0 + np.arange(1, 41) * 0.5, 2020.0 - np.arange(1, n - 140) * 0.25]
        t = 1_700_000_000 // 3600 * 3600 + np.arange(len(c)) * 900
        m15 = FeatureFrame(t, c, c + 0.2, c - 0.2, c, np.zeros(len(c)), np.ones(len(c)), "M15")
        from app.features.frame import resample
        cfg = StrategyCfg(id="one_plan", class_name="OnePlan", version="0", status="DEMO", params={},
                          exit_model={"type": "fixed"})
        with mock.patch.dict(registry.CLASSES, {"OnePlan": OnePlan}):
            res = run_backtest(cfg, m15, resample(m15, 3600, "H1"),
                               BTSettings(apply_regime_filter=False, apply_session_filter=False, slippage_points=0,
                                          swap_nights=False))
        self.assertEqual(len(res.trades), 1)
        tr = res.trades[0]
        self.assertGreater(tr["r_multiple"], 2.5, "the 5-point trail from the 2020.2 peak, not a fixed stop/target")
        self.assertLess(tr["r_multiple"], 3.5)


class TestManagerSwitch(unittest.TestCase):
    """The trade manager itself, driven like test_live_protection: a 12R-money peak that would lock 8.40."""

    def _run(self, exit_model):
        from app.execution.trade_manager import TradeManager
        state = OpenTradeState(Side.BUY, entry=1.0, initial_sl=0.99, sl=0.99, tp=1.1, volume=0.01,
                               initial_volume=0.01, entry_time=1)
        trade = {"id": "t", "position_id": 1, "entry_price": 1.0, "direction": "BUY", "state": "OPEN", "extra": {},
                 "tp": 1.1, "exit_model": exit_model, "initial_risk_money": 20.0}
        position = SimpleNamespace(ticket=1, symbol="EURUSDm", side=Side.BUY, price_open=1.0, sl=0.99, tp=1.1,
                                   price_current=1.01, profit=12.0, swap=0.0, volume=0.01)
        m = TradeManager.__new__(TradeManager)
        m.symbol, m.broker = "EURUSDm", SimpleNamespace(calc_profit=lambda *a: (a[4] - a[3]) * 100.0)
        m.book = SimpleNamespace(open_positions=lambda: [trade], ensure_mstate=lambda _: state,
                                 update=Mock(side_effect=lambda _t, **ch: trade.update(ch)))
        m.spec_fn = lambda: SimpleNamespace(point=0.00001, tick_size=0.00001, digits=5,
                                            round_price=lambda p: round(p, 5), stops_level=0, freeze_level=0)
        for k, v in dict(profit_trail_activation_money=0.0, profit_lock_floor_money=0.05,
                         small_win_activation_money=0.20, small_win_floor_money=0.05, ambition_activation_money=0.90,
                         ambition_floor_money=0.50, hard_profit_target_money=0.0, profit_giveback_fraction=0.3,
                         commission_per_lot_round_trip=0.0, slippage_points=0.0,
                         profit_lock_stop_buffer_points=3.0).items():
            setattr(m, k, v)
        m.floor_r = {"small_win_activation": 0.5, "small_win_floor": 0.1, "ambition_activation": 0.8,
                     "ambition_floor": 0.4}
        m.close = Mock(return_value=True)
        m._ensure_bracket = Mock(return_value=position)
        m._modify_sl_result = Mock(return_value=SimpleNamespace(ok=True, already_closed=False, ambiguous=False))
        m.on_tick(Tick(time_msc=1, bid=1.01, ask=1.0101))
        position.profit = 1.5
        m.on_tick(Tick(time_msc=2, bid=1.005, ask=1.0051))
        return m, state

    def test_the_lock_still_closes_an_ordinary_trade(self):
        m, state = self._run({"breakeven": {"enabled": False}})
        self.assertAlmostEqual(state.profit_lock_target_money, 8.4)
        m.close.assert_called_once()

    def test_a_strategy_that_switches_it_off_is_never_locked(self):
        m, state = self._run({"breakeven": {"enabled": False}, "profit_lock": {"enabled": False}})
        self.assertFalse(state.profit_lock_active)
        m.close.assert_not_called()
        m._modify_sl_result.assert_not_called()


class TestTrendDailyStrategy(unittest.TestCase):
    def setUp(self):
        from tests.unit.test_research_evidence import trending_h4
        self.h4 = trending_h4(days=420)                    # 2,520 H4 bars: inside the strategy's window
        self.cfg = ConfigManager().strategy("trend_daily_v1")

    def _view(self, day_start):
        from app.strategies.base import StrategyView
        h4 = self.h4
        m15 = FeatureFrame([day_start], [1.0], [1.0], [1.0], [float(h4.c[0])], [0.0], [1.0], "M15")
        q = int(np.searchsorted(np.asarray(h4.t) + 14400, day_start + 900, side="right") - 1)
        px = float(h4.c[q])
        return StrategyView(m15=m15, i=0, h1=m15, k=0, h4=h4, q=q, symbol="EURUSDm", point=0.00001,
                            spread_price=0.0001, bid=px, ask=px + 0.0001, spread_safe=True)

    def test_live_signals_match_the_research_rule_day_for_day(self):
        from app.research.lab.hypotheses import daily_bars, trend_daily
        from app.strategies.registry import build
        strat = build(self.cfg)
        daily = daily_bars(self.h4)
        lab = trend_daily(daily, None, strat.rule_params())
        checked = fired = 0
        for j in range(160, len(daily)):
            res = strat.evaluate(self._view(int(daily.t[j]) + 86400))     # the first M15 bar after day j ended
            want = 1 if lab["long"][j] else (-1 if lab["short"][j] else 0)
            got = 0 if res.plan is None else res.plan.direction.sign
            self.assertEqual(got, want, f"day {j}")
            checked += 1
            fired += want != 0
        self.assertGreater(fired, 5)
        self.assertGreater(checked, 200)

    def test_its_trades_carry_the_trend_exit(self):
        from app.research.lab.hypotheses import daily_bars, trend_daily
        from app.strategies.registry import build
        strat = build(self.cfg)
        daily = daily_bars(self.h4)
        lab = trend_daily(daily, None, strat.rule_params())
        j = int(np.flatnonzero(lab["long"] | lab["short"])[-1])
        plan = strat.evaluate(self._view(int(daily.t[j]) + 86400)).plan
        m = with_execution_exits(plan.exit_model, ConfigManager().app.execution)
        self.assertFalse(m["profit_lock"]["enabled"])
        self.assertFalse(m["breakeven"]["enabled"])
        self.assertEqual(m["type"], "chandelier")
        self.assertAlmostEqual(m["trail"]["distance_price"], plan.stop_distance)
        self.assertAlmostEqual(abs(plan.tp - plan.entry) / plan.stop_distance, 20.0)   # the broker needs a target
        self.assertEqual(plan.setup_key, f"trend_daily_v1:{plan.direction}:{int(daily.t[j])}")

    def test_config_holds_one_position_and_leaves_out_natural_gas(self):
        self.assertEqual(self.cfg.status, "DEMO")
        self.assertFalse(self.cfg.params["allow_scale_in"].value)
        self.assertNotIn("XNGUSDm", self.cfg.symbols)
        self.assertNotIn("*", self.cfg.symbols)

    def test_hydration_never_asks_more_h4_than_the_thinnest_symbol_has(self):
        from app.strategies.registry import build
        self.assertLessEqual(build(self.cfg).lookbacks()["H4"], 900, "stock CFDs had ~980 H4 bars on 20 Oct 2026")

    def test_a_thin_h4_history_cannot_stop_the_symbol_starting(self):
        """Hydration refuses to start below the H4 bars a strategy REQUIRES. The first full test run failed 100
        simulated cores that way; trend following needs no H4 to start and degrades to INSUFFICIENT_HISTORY."""
        from app.core.engine import TradingCore
        from app.strategies.registry import build
        self.assertNotIn("H4", build(self.cfg).min_history())
        self.assertIn('required.get("H4")', inspect.getsource(TradingCore))
        strat = build(self.cfg)
        short = FeatureFrame(self.h4.t[:200], self.h4.o[:200], self.h4.h[:200], self.h4.l[:200], self.h4.c[:200],
                             self.h4.spread[:200], self.h4.tick_volume[:200], "H4")
        self.h4 = short
        self.assertEqual(strat.evaluate(self._view(int(short.t[-1]) + 14400)).reasons, ["INSUFFICIENT_HISTORY"])


class TestEngineRules(unittest.TestCase):
    def _core(self, sid):
        from app.core.engine import TradingCore
        cfgm = ConfigManager()
        trade = {"symbol": "EURUSDm", "state": "PROTECTION", "position_id": 1, "direction": "BUY",
                 "entry_price": 1.0, "sl": 1.001}
        fake = SimpleNamespace(cfg=cfgm.app.model_copy(update={"scaling": cfgm.app.scaling.model_copy(
                                   update={"enabled": True})}),
                               cfgm=cfgm, symbol="EURUSDm", last_tick=Tick(time_msc=1, bid=1.02, ask=1.0201),
                               book=SimpleNamespace(ensure_mstate=lambda t: SimpleNamespace(r=lambda px: 2.0)))
        sig = SimpleNamespace(strategy_id=sid, plan=SimpleNamespace(direction=Side.BUY))
        return TradingCore._scale_in_multiplier(fake, sig, [trade], {"active_trades": 1, "broker_positions": 1})

    def test_trend_following_never_adds_to_a_position(self):
        self.assertIsNone(self._core("trend_daily_v1"))

    def test_other_strategies_still_scale_in(self):
        self.assertEqual(self._core("pullback_v1"), ConfigManager().app.scaling.risk_multiplier)

    def test_a_strategy_runs_only_on_its_configured_symbols(self):
        from app.core.engine import TradingCore
        src = inspect.getsource(TradingCore)
        self.assertIn('NOT_CONFIGURED_FOR_THIS_SYMBOL', src)
        from app.market.symbols import contains
        self.assertTrue(contains(["XAUUSDm"], "XAUUSD"), "a broker without the m suffix is the same gold")
        self.assertFalse(contains(["XAUUSDm"], "XAUUSD247m"), "the 24/7 gold is a separate symbol")


if __name__ == "__main__":
    unittest.main()
