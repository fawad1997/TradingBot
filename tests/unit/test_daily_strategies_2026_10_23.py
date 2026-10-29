"""The two daily strategies added on 23 Oct 2026: multi-speed trend following (trend_ensemble_v1) and the turn of
the month (turn_of_month_v1).

Both trade the function the lab replays, on the same daily bars as trend_daily, so these tests pin the rules (what
fires, when, and that nothing reads the future), the exits the live classes attach, and that the nightly lab
replays each one with the exit it will actually trade.
"""
import datetime as dt
import unittest
from types import SimpleNamespace

import numpy as np

from app.config import ConfigManager
from app.features.frame import FeatureFrame
from app.research.lab.hypotheses import FAR_TARGET_R, TURN_OF_MONTH_DEFAULTS, trend_ensemble, turn_of_month
from app.research.lab.trend import research_trend
from app.strategies.base import StrategyView
from app.strategies.registry import build
from app.strategies.turn_of_month import bars_per_day, hold_exit

DAY = 86400
UTC = dt.timezone.utc


def epoch(d: dt.date, hour: int = 0) -> int:
    return int(dt.datetime(d.year, d.month, d.day, hour, tzinfo=UTC).timestamp())


def daily_frame(closes, start=dt.date(2020, 1, 1), weekdays_only=True) -> FeatureFrame:
    """One bar per weekday (or per calendar day), each a small range around its close."""
    t, d = [], start
    while len(t) < len(closes):
        if not weekdays_only or d.weekday() < 5:
            t.append(epoch(d))
        d += dt.timedelta(days=1)
    c = np.asarray(closes, float)
    o = np.r_[c[0], c[:-1]]
    return FeatureFrame(np.asarray(t, dtype=np.int64), o, np.maximum(o, c) * 1.002, np.minimum(o, c) * 0.998, c,
                        np.zeros(len(c)), np.ones(len(c)), "D1")


def head(f: FeatureFrame, n: int) -> FeatureFrame:
    return FeatureFrame(f.t[:n], f.o[:n], f.h[:n], f.l[:n], f.c[:n], f.spread[:n], f.tick_volume[:n], f.timeframe)


def on(f: FeatureFrame, mask) -> list:
    return [dt.datetime.fromtimestamp(int(x), UTC).date() for x in np.asarray(f.t)[np.asarray(mask, bool)]]


def view(days: list, closes, m15_days: int = 25, next_day_hour: int = 1) -> StrategyView:
    """What the live engine hands a strategy: H4 bars for `days`, and M15 bars up to `next_day_hour` of the day
    after the last one, so the last of `days` has ended and is visible."""
    t4, c4 = [], []
    for d, c in zip(days, closes):
        for h in range(0, 24, 4):
            t4.append(epoch(d, h))
            c4.append(c)
    c4 = np.asarray(c4, float)
    h4 = FeatureFrame(np.asarray(t4, dtype=np.int64), c4, c4 * 1.001, c4 * 0.999, c4, np.zeros(len(c4)),
                      np.ones(len(c4)), "H4")
    start = days[-1] - dt.timedelta(days=m15_days - 1)
    t15 = np.arange(epoch(start), epoch(days[-1] + dt.timedelta(days=1), next_day_hour), 900, dtype=np.int64)
    c15 = np.full(len(t15), float(closes[-1]))
    m15 = FeatureFrame(t15, c15, c15 * 1.0005, c15 * 0.9995, c15, np.zeros(len(t15)), np.ones(len(t15)), "M15")
    price = float(closes[-1])
    return StrategyView(m15=m15, i=len(m15) - 1, h1=m15, k=len(m15) - 1, h4=h4, q=len(h4) - 1, symbol="TEST",
                        point=0.01, spread_price=0.0, bid=price, ask=price)


class TestTrendEnsembleRule(unittest.TestCase):
    def test_long_once_every_speed_is_up_and_never_before_a_year(self):
        f = daily_frame(100 * 1.001 ** np.arange(400))
        sig = trend_ensemble(f)
        first = int(np.flatnonzero(sig["long"])[0])
        self.assertGreaterEqual((f.t[first] - f.t[0]) / DAY, 365)
        self.assertTrue(sig["long"][-1])
        self.assertFalse(sig["short"].any())

    def test_short_when_every_speed_is_down_and_nothing_when_they_disagree(self):
        down = trend_ensemble(daily_frame(100 * 0.999 ** np.arange(400)))
        self.assertTrue(down["short"][-1])
        self.assertFalse(down["long"].any())
        # a long rise then three months of fall: the 12-month return is up, the 1- and 3-month ones down
        mixed = trend_ensemble(daily_frame(np.r_[100 * 1.002 ** np.arange(300),
                                                 100 * 1.002 ** 300 * 0.998 ** np.arange(1, 70)]))
        self.assertFalse(mixed["long"][-1] or mixed["short"][-1])
        self.assertLess(abs(mixed["score"][-1]), 1.0)

    def test_lookbacks_are_calendar_days_not_bars(self):
        """Crypto prints 7 bars a week and FX 5: the same 365-day lookback must mean a year for both."""
        five = trend_ensemble(daily_frame(100 * 1.001 ** np.arange(300), weekdays_only=True))     # ~420 days
        seven = trend_ensemble(daily_frame(100 * 1.001 ** np.arange(300), weekdays_only=False))   # 300 days
        self.assertTrue(five["long"][-1])
        self.assertFalse(seven["long"].any(), "300 calendar days cannot fill a 365-day lookback")

    def test_nothing_it_decides_changes_when_later_bars_are_removed(self):
        rng = np.random.default_rng(3)
        f = daily_frame(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, 800))))
        full, part = trend_ensemble(f), trend_ensemble(head(f, 650))
        for k in ("long", "short"):
            self.assertTrue(np.array_equal(full[k][:650], part[k]), k)
        self.assertTrue(full["long"].any() or full["short"].any())


class TestTurnOfMonthRule(unittest.TestCase):
    def test_fires_once_a_month_on_the_second_to_last_weekday(self):
        f = daily_frame(100 + np.arange(420) * 0.01)
        days = on(f, turn_of_month(f)["long"])
        self.assertGreater(len(days), 15)
        for d in days:
            later = [d + dt.timedelta(days=k) for k in range(1, 8)]
            self.assertEqual(sum(1 for x in later if x.month == d.month and x.weekday() < 5), 1, d)
        self.assertEqual(len({(d.year, d.month) for d in days}), len(days))

    def test_long_only_and_never_on_a_weekend_stub(self):
        f = daily_frame(np.linspace(100, 90, 500), weekdays_only=False)        # falling, with weekend bars
        sig = turn_of_month(f)
        self.assertFalse(sig["short"].any())
        self.assertTrue(all(d.weekday() < 5 for d in on(f, sig["long"])))

    def test_the_replay_holds_the_last_day_and_the_first_three(self):
        f = daily_frame(np.linspace(100, 110, 320), weekdays_only=False)
        spec = SimpleNamespace(point=0.01, swap_long=0.0, swap_short=0.0, swap_mode=1, raw={})
        r = research_trend(f, spec, 0.0, 0.0, rule=turn_of_month, defaults=TURN_OF_MONTH_DEFAULTS, min_days=22,
                           weekdays_only=True, exit_model=lambda d: hold_exit(4, FAR_TARGET_R))
        self.assertGreater(len(r["trades"]), 6)
        for t in r["trades"][:-1]:                     # the last may still be open at the end of the data
            self.assertEqual((t["exit_reason"], t["bars_held"]), ("TIME_STOP", 4))
            entry = dt.datetime.fromtimestamp(t["entry_time"], UTC).date()
            nxt = entry + dt.timedelta(days=1)
            while nxt.weekday() > 4:
                nxt += dt.timedelta(days=1)
            self.assertNotEqual(nxt.month, entry.month, f"{entry} is not the month's last weekday")


class TestLiveClasses(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfgm = ConfigManager()

    def test_trend_ensemble_buys_an_all_speed_uptrend_with_the_trend_exit(self):
        days = [dt.date(2024, 1, 1) + dt.timedelta(days=k) for k in range(420)]
        res = build(self.cfgm.strategy("trend_ensemble_v1")).evaluate(view(days, 100 * 1.001 ** np.arange(420)))
        self.assertIsNotNone(res.plan, res.reasons)
        p = res.plan
        self.assertEqual(str(p.direction), "BUY")
        self.assertEqual(p.exit_model["type"], "chandelier")
        self.assertFalse(p.exit_model["profit_lock"]["enabled"])
        self.assertAlmostEqual(p.exit_model["trail"]["distance_price"], p.entry - p.sl)
        self.assertEqual((p.features["rule"], p.features["score"]), ("trend_ensemble", 1.0))

    def test_turn_of_month_buys_the_last_day_and_exits_after_four_days_of_bars(self):
        # 28 Oct 2026 is a Wednesday: Thu 29 and Fri 30 remain, so the 29th is the second-to-last weekday
        days = [dt.date(2026, 10, 29) - dt.timedelta(days=k) for k in range(60)][::-1]
        res = build(self.cfgm.strategy("turn_of_month_v1")).evaluate(view(days, np.linspace(100, 101, 60)))
        self.assertIsNotNone(res.plan, res.reasons)
        p = res.plan
        self.assertEqual(str(p.direction), "BUY")
        self.assertEqual(p.exit_model["type"], "fixed")
        self.assertEqual(p.exit_model["time_stop"], {"enabled": True, "max_bars": 4 * 96, "min_r": 1e9})
        self.assertFalse(p.exit_model["profit_lock"]["enabled"])
        self.assertFalse(p.exit_model["breakeven"]["enabled"])
        # the day before is not a signal day
        res = build(self.cfgm.strategy("turn_of_month_v1")).evaluate(view(days[:-1], np.linspace(100, 101, 59)))
        self.assertIsNone(res.plan)

    def test_bars_per_day_reads_the_symbols_own_session(self):
        t = np.concatenate([np.arange(epoch(dt.date(2026, 10, d), 13), epoch(dt.date(2026, 10, d), 20), 900)
                            for d in range(5, 24) if dt.date(2026, 10, d).weekday() < 5]).astype(np.int64)
        c = np.ones(len(t))
        m15 = FeatureFrame(t, c, c, c, c, c * 0, c, "M15")
        self.assertEqual(bars_per_day(m15, len(t) - 1), 28)               # 13:00-20:00 = 7 h of M15 bars


class TestTheLabReplaysTheTradedRules(unittest.TestCase):
    def test_each_symbol_gets_the_rules_configured_on_it(self):
        from app.research.lab.worker import _other_daily_rules
        cfgm = ConfigManager()
        names = lambda sym: [n for n, _ in _other_daily_rules(cfgm, sym)]
        self.assertEqual(names("XAUUSDm"), ["trend_ensemble"])
        self.assertEqual(names("US500m"), ["turn_of_month"])
        self.assertEqual(names("NVDAm"), [])
        tom = dict(_other_daily_rules(cfgm, "US500m"))["turn_of_month"]
        self.assertTrue(tom["weekdays_only"])
        self.assertEqual(tom["exit_model"](1.0)["time_stop"]["max_bars"], 4, "one replay bar is one day")


if __name__ == "__main__":
    unittest.main()
