import json
import unittest
from pathlib import Path

import numpy as np

from app.broker.sim import xau_spec
from app.core.types import Side
from app.features.frame import FeatureFrame, resample
from app.research.backtester import BTSettings, run_backtest
from app.strategies.base import StrategyView
from app.strategies.macro_filter import evaluate_macro_channel
from app.strategies.micro_scalper import MicroScalper
from app.config import ConfigManager


def make_view(direction: int = 0, *, spread_safe: bool = True, range_scale: float = 1.0) -> StrategyView:
    n = 40
    close = np.full(n, 1.0)
    close[-1] += direction * 0.001 * range_scale
    high = close + 0.001 * range_scale
    low = close - 0.001 * range_scale
    bars = FeatureFrame(
        1_700_000_000 + np.arange(n) * 60,
        close,
        high,
        low,
        close,
        tick_volume=np.r_[np.full(n - 1, 100.0), 120.0],
        timeframe="M1",
    )
    m15_close = np.full(n, 1.0)
    m15_close[-1] += direction * 0.001 * range_scale
    m15 = FeatureFrame(
        1_700_000_000 + np.arange(n) * 900,
        np.full(n, 1.0),
        m15_close + 0.001 * range_scale,
        m15_close - 0.001 * range_scale,
        m15_close,
        timeframe="M15",
    )
    return StrategyView(
        m15=m15,
        i=n - 1,
        h1=bars,
        k=0,
        m1=bars,
        j=n - 1,
        point=0.00001,
        spread_price=0.0001,
        bid=1.0,
        ask=1.0001,
        spread_safe=spread_safe,
        symbol="TEST",
        min_stop_distance=0.0002,
    )


class TestMicroScalper(unittest.TestCase):
    def setUp(self):
        self.strategy = MicroScalper(
            "micro_scalper",
            "1.6.0",
            {"stop_atr_multiple": 1.0, "target_atr_multiple": 1.0,
             "candlestick_triggers_enabled": True, "macro_filter_enabled": False,
             "minimum_relative_volume": 1.0},
            {},
        )

    def test_config_enables_candlestick_momentum_variant_for_demo(self):
        root = Path(__file__).resolve().parents[2]
        config = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
        strategies = json.loads((root / "config" / "strategies.json").read_text(encoding="utf-8"))
        strategy = next(item for item in strategies["strategies"] if item["id"] == "micro_scalper")

        self.assertEqual(config["environment"], "DEMO")
        self.assertEqual(strategy["status"], "DEMO")
        self.assertEqual(strategy["version"], "1.7.0")
        self.assertTrue(strategy["params"]["candlestick_triggers_enabled"]["value"])
        self.assertTrue(strategy["params"]["macro_filter_enabled"]["value"])
        self.assertEqual(strategy["params"]["macro_channel_window"]["value"], 40)
        self.assertEqual(strategy["params"]["macro_extreme_fraction"]["value"], 0.3)
        self.assertEqual(strategy["params"]["minimum_relative_volume"]["value"], 1.0)
        self.assertEqual(strategy["params"]["stop_atr_multiple"]["value"], 1.5)
        self.assertEqual(strategy["params"]["target_atr_multiple"]["value"], 1.5)
        self.assertEqual(strategy["params"]["min_stop_spread_multiple"]["value"], 10.0)
        self.assertNotIn("stop_points", strategy["params"])
        self.assertNotIn("tp_points", strategy["params"])
        self.assertNotIn("confluence_threshold", strategy["params"])
        self.assertNotIn("trigger_fraction", strategy["params"])

    def test_momentum_without_candlestick_pattern_is_rejected(self):
        result = self.strategy.evaluate(make_view(1))

        self.assertIsNone(result.plan)
        self.assertEqual(result.reasons, ["NO_CANDLESTICK_TRIGGER"])

    def test_candlestick_candidate_contains_directional_confirmation_metrics(self):
        view = self._set_last_candles(
            (1.0, 1.0001, 0.9999, 1.0),
            (0.9998, 1.0001, 0.9980, 1.0),
        )
        result = self.strategy.evaluate(view)

        self.assertIsNotNone(result.plan)
        self.assertEqual(result.plan.direction, Side.BUY)
        self.assertEqual(result.plan.features["trigger_pattern"], "BULLISH_PIN_BAR")
        self.assertGreaterEqual(result.plan.features["relative_tick_volume"], 1.2)
        self.assertEqual(result.plan.features["entry_candidate"]["asset_name"], "TEST")
        self.assertEqual(result.plan.features["entry_candidate"]["direction"], "BUY")
        self.assertEqual(result.plan.features["entry_candidate"]["initial_entry_price"], result.plan.entry)

    def test_stop_and_target_follow_live_atr_instead_of_fixed_points(self):
        quiet_view = make_view(range_scale=0.05)
        volatile_view = make_view(range_scale=1.0)
        self._set_bullish_pin(quiet_view, 0.05)
        self._set_bullish_pin(volatile_view, 1.0)
        quiet = self.strategy.evaluate(quiet_view).plan
        volatile = self.strategy.evaluate(volatile_view).plan

        self.assertIsNotNone(quiet)
        self.assertIsNotNone(volatile)
        self.assertGreater(volatile.stop_distance, quiet.stop_distance)
        self.assertGreater(volatile.tp - volatile.entry, quiet.tp - quiet.entry)
        self.assertGreaterEqual(quiet.stop_distance, 0.0003 - 1e-12)

    def test_negative_momentum_triggers_sell(self):
        view = self._set_last_candles(
            (1.0, 1.0001, 0.9999, 1.0),
            (1.0002, 1.0020, 0.9999, 1.0),
        )
        result = self.strategy.evaluate(view)

        self.assertIsNotNone(result.plan)
        self.assertEqual(result.plan.direction, Side.SELL)

    def test_unsafe_spread_remains_a_hard_gate(self):
        result = self.strategy.evaluate(make_view(1, spread_safe=False))

        self.assertIsNone(result.plan)
        self.assertEqual(result.reasons, ["SPREAD_NOT_WITHIN_SAFE_RANGE"])

    def test_small_momentum_does_not_trigger(self):
        result = self.strategy.evaluate(make_view())

        self.assertIsNone(result.plan)
        self.assertEqual(result.reasons, ["NO_CANDLESTICK_TRIGGER"])

    def test_tick_volume_must_reach_prior_median(self):
        view = self._set_last_candles(
            (1.0, 1.0001, 0.9999, 1.0),
            (0.9998, 1.0001, 0.9980, 1.0),
        )
        view.m1.tick_volume[-1] = 99.0

        result = self.strategy.evaluate(view)

        self.assertIsNone(result.plan)
        self.assertEqual(result.reasons, ["LOW_OR_UNAVAILABLE_TICK_VOLUME"])

        view.m1.tick_volume[-1] = 100.0
        self.assertIsNotNone(self.strategy.evaluate(view).plan)

    def test_m15_opposite_direction_rejects_but_neutral_is_allowed(self):
        view = self._set_last_candles(
            (1.0, 1.0001, 0.9999, 1.0),
            (0.9998, 1.0001, 0.9980, 1.0),
        )
        view.m15.c[-1] = view.m15.o[-1] - 0.0001

        result = self.strategy.evaluate(view)

        self.assertIsNone(result.plan)
        self.assertEqual(result.reasons, ["M15_DIRECTION_NOT_CONFIRMED"])
        view.m15.c[-1] = view.m15.o[-1]
        self.assertIsNotNone(self.strategy.evaluate(view).plan)

    def test_enabled_macro_filter_rejects_when_h4_history_is_missing(self):
        strategy = MicroScalper(
            "micro_scalper", "1.6.0",
            {"candlestick_triggers_enabled": True, "macro_filter_enabled": True}, {})
        view = self._set_last_candles(
            (1.0, 1.0001, 0.9999, 1.0),
            (0.9998, 1.0001, 0.9980, 1.0),
        )

        result = strategy.evaluate(view)

        self.assertIsNone(result.plan)
        self.assertEqual(result.reasons, ["H4_MACRO_CHANNEL_UNAVAILABLE"])

    def test_macro_filter_only_accepts_trend_aligned_outer_channel_zone(self):
        from app.features.frame import FeatureFrame

        count = 40
        t = 1_700_000_000 + np.arange(count) * 14_400
        residual = np.sin(np.arange(count) * 2 * np.pi / 10) * 0.5
        rising = 100.0 + np.arange(count) * 0.1 + residual
        h4_up = FeatureFrame(t, rising, rising + 0.1, rising - 0.1, rising, timeframe="H4")
        channel = evaluate_macro_channel(h4_up, count - 1, 100.0, Side.BUY)
        width = channel.upper - channel.lower
        buy = evaluate_macro_channel(h4_up, count - 1, channel.lower + 0.1 * width, Side.BUY)
        sell = evaluate_macro_channel(h4_up, count - 1, channel.upper - 0.1 * width, Side.SELL)
        middle = evaluate_macro_channel(h4_up, count - 1, channel.lower + 0.5 * width, Side.BUY)

        self.assertTrue(buy.is_at_channel_extreme)
        self.assertFalse(sell.is_at_channel_extreme)
        self.assertFalse(middle.is_at_channel_extreme)

    def test_macro_filter_widens_buy_zone_to_outer_thirty_percent(self):
        from app.features.frame import FeatureFrame

        count = 40
        t = 1_700_000_000 + np.arange(count) * 14_400
        residual = np.sin(np.arange(count) * 2 * np.pi / 10) * 0.0005
        rising = 1.0 + np.arange(count) * 0.0001 + residual
        h4 = FeatureFrame(t, rising, rising + 0.0001, rising - 0.0001, rising, timeframe="H4")
        channel = evaluate_macro_channel(h4, count - 1, 1.0, Side.BUY, extreme_fraction=0.3)
        if channel is None:
            self.fail("valid synthetic H4 data did not produce a regression channel")
        target = channel.lower + 0.25 * (channel.upper - channel.lower)
        narrow = evaluate_macro_channel(h4, count - 1, target, Side.BUY, extreme_fraction=0.2)
        wide = evaluate_macro_channel(h4, count - 1, target, Side.BUY, extreme_fraction=0.3)
        if narrow is None or wide is None:
            self.fail("valid synthetic H4 data did not produce both regression channels")
        self.assertFalse(narrow.is_at_channel_extreme)
        self.assertTrue(wide.is_at_channel_extreme)

        view = self._set_last_candles(
            (1.0, 1.0001, 0.9999, 1.0),
            (0.9998, 1.0001, 0.9980, 1.0),
        )
        shift = target - view.m1.c[-1]
        for frame in (view.m1, view.m15):
            for field in ("o", "h", "l", "c"):
                setattr(frame, field, getattr(frame, field) + shift)
        view.bid += shift
        view.ask += shift
        view.h4 = h4
        view.q = count - 1
        strategy = MicroScalper(
            "micro_scalper", "1.6.0",
            {"candlestick_triggers_enabled": True, "macro_filter_enabled": True,
             "macro_channel_window": 40, "macro_extreme_fraction": 0.3,
             "minimum_relative_volume": 1.0}, {})

        self.assertIsNotNone(strategy.evaluate(view).plan)

    def _set_last_candles(self, previous, current):
        view = make_view()
        frame = view.m1
        frame.o = frame.o.copy()
        frame.o[-2], frame.h[-2], frame.l[-2], frame.c[-2] = previous
        frame.o[-1], frame.h[-1], frame.l[-1], frame.c[-1] = current
        view.m15.o[-1] = 1.0
        view.m15.c[-1] = 1.0001 if current[3] > current[0] else 0.9999
        return view

    def _set_bullish_pin(self, view, scale):
        return self._set_last_candles_on_view(
            view,
            (1.0, 1.0 + 0.0001 * scale, 1.0 - 0.0001 * scale, 1.0),
            (1.0 - 0.0002 * scale, 1.0 + 0.0001 * scale,
             1.0 - 0.0020 * scale, 1.0),
        )

    @staticmethod
    def _set_last_candles_on_view(view, previous, current):
        frame = view.m1
        frame.o = frame.o.copy()
        frame.o[-2], frame.h[-2], frame.l[-2], frame.c[-2] = previous
        frame.o[-1], frame.h[-1], frame.l[-1], frame.c[-1] = current
        view.m15.o[-1] = 1.0
        view.m15.c[-1] = 1.0001
        return view

    def test_bullish_pin_bar_triggers_without_momentum_threshold(self):
        view = self._set_last_candles(
            (1.0, 1.0001, 0.9999, 1.0),
            (0.9998, 1.0001, 0.9980, 1.0),
        )

        result = self.strategy.evaluate(view)

        self.assertEqual(result.plan.direction, Side.BUY)
        self.assertEqual(result.plan.features["trigger_pattern"], "BULLISH_PIN_BAR")

    def test_bearish_pin_bar_triggers_without_momentum_threshold(self):
        view = self._set_last_candles(
            (1.0, 1.0001, 0.9999, 1.0),
            (1.0002, 1.0020, 0.9999, 1.0),
        )

        result = self.strategy.evaluate(view)

        self.assertEqual(result.plan.direction, Side.SELL)
        self.assertEqual(result.plan.features["trigger_pattern"], "BEARISH_PIN_BAR")

    def test_bullish_engulfing_triggers_without_momentum_threshold(self):
        view = self._set_last_candles(
            (1.0002, 1.0003, 0.9997, 0.9998),
            (0.9997, 1.0004, 0.9996, 1.0003),
        )

        result = self.strategy.evaluate(view)

        self.assertEqual(result.plan.direction, Side.BUY)
        self.assertEqual(result.plan.features["trigger_pattern"], "BULLISH_ENGULFING")

    def test_bearish_engulfing_triggers_without_momentum_threshold(self):
        view = self._set_last_candles(
            (0.9998, 1.0003, 0.9997, 1.0002),
            (1.0003, 1.0004, 0.9996, 0.9997),
        )

        result = self.strategy.evaluate(view)

        self.assertEqual(result.plan.direction, Side.SELL)
        self.assertEqual(result.plan.features["trigger_pattern"], "BEARISH_ENGULFING")

    def _run_research_grid(self, mirrored=False):
        count = 100 * 15
        start = 1_700_000_100
        start -= start % 900
        times = start + np.arange(count) * 60
        opens = np.full(count, 100.0)
        highs = np.full(count, 100.01)
        lows = np.full(count, 99.99)
        closes = np.full(count, 100.0)

        signal_index = 60 * 15 + 14
        opens[signal_index - 1], highs[signal_index - 1] = 100.02, 100.03
        lows[signal_index - 1], closes[signal_index - 1] = 99.99, 100.0
        opens[signal_index], highs[signal_index] = 100.0, 100.02
        lows[signal_index], closes[signal_index] = 99.80, 100.015
        lows[61 * 15] = 99.95
        lows[61 * 15 + 1], highs[61 * 15 + 1] = 99.93, 99.94
        lows[61 * 15 + 2], highs[61 * 15 + 2] = 99.93, 100.20
        if mirrored:
            opens, closes = 200.0 - opens, 200.0 - closes
            highs, lows = 200.0 - lows, 200.0 - highs

        m1_volume = np.full(count, 100.0)
        m1_volume[signal_index] = 120.0
        m1 = FeatureFrame(times, opens, highs, lows, closes,
                          tick_volume=m1_volume, timeframe="M1")
        m15_times = start + np.arange(100) * 900
        m15_o = opens[::15]
        m15_h = np.maximum.reduce([highs[k::15] for k in range(15)])
        m15_l = np.minimum.reduce([lows[k::15] for k in range(15)])
        m15_c = closes[14::15]
        m15_o = opens[::15]
        m15 = FeatureFrame(m15_times, m15_o, m15_h, m15_l, m15_c,
                           np.zeros(100), timeframe="M15")
        h1 = resample(m15, 3600, "H1")
        cfg = ConfigManager()
        regimes = [("RANGE", "NORMAL", False, "TEST")] * len(h1)

        result = run_backtest(
            cfg.strategy("micro_scalper"), m15, h1,
            BTSettings(grid_enabled=True, start_equity=10_000, slippage_points=1,
                       commission_per_lot=2.0),
            spec=xau_spec(), regimes=regimes, m1=m1,
            param_overrides={"macro_filter_enabled": False},
        )

        return result

    def test_research_grid_is_candle_gated_and_closes_the_net_basket_target(self):
        result = self._run_research_grid()

        self.assertEqual(len(result.trades), 1)
        basket = result.trades[0]
        self.assertEqual(basket["layers"], 3)
        self.assertEqual(basket["direction"], "BUY")
        self.assertEqual(basket["exit_reason"], "GRID_BASKET_TARGET")
        self.assertAlmostEqual(basket["pnl_money"], 1.0, places=6)
        self.assertLessEqual(basket["risk_money"], 100.0)

    def test_research_grid_handles_sell_basket(self):
        result = self._run_research_grid(mirrored=True)

        self.assertEqual(len(result.trades), 1)
        basket = result.trades[0]
        self.assertEqual(basket["layers"], 3)
        self.assertEqual(basket["direction"], "SELL")
        self.assertEqual(basket["exit_reason"], "GRID_BASKET_TARGET")
        self.assertAlmostEqual(basket["pnl_money"], 1.0, places=6)
        self.assertLessEqual(basket["risk_money"], 100.0)

    def test_research_grid_requires_m1_history(self):
        cfg = ConfigManager()
        empty = FeatureFrame([], [], [], [], [])

        with self.assertRaisesRegex(ValueError, "requires M1 history"):
            run_backtest(cfg.strategy("micro_scalper"), empty, empty,
                         BTSettings(grid_enabled=True), m1=None)


if __name__ == "__main__":
    unittest.main()
