import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from app.config import ConfigManager
from app.core.types import Bar, SymbolSpec
from app.features.frame import FeatureFrame


def synthetic(n=9000, seed=7):
    rng = np.random.default_rng(seed)
    t0 = 1_760_000_000 - (1_760_000_000 % 900)
    price = 2500 + np.cumsum(rng.normal(0, 0.35, n))
    o = np.r_[price[0], price[:-1]]
    wick = np.abs(rng.normal(0, 0.5, (n, 2)))
    h = np.maximum(o, price) + wick[:, 0]
    l = np.minimum(o, price) - wick[:, 1]
    vol = rng.lognormal(3, 0.6, n)
    spread = np.full(n, 240.0)
    t = t0 + 60 * np.arange(n)
    m1 = FeatureFrame(t, o, h, l, price, spread, vol, "M1")
    g = n // 15
    sl = lambda a, f: np.array([f(a[i * 15:(i + 1) * 15]) for i in range(g)])
    m15 = FeatureFrame(t[: g * 15 : 15], sl(o, lambda x: x[0]), sl(h, np.max), sl(l, np.min), sl(price, lambda x: x[-1]),
                       sl(spread, np.mean), sl(vol, np.sum), "M15")
    return m1, m15


class FakeAdapter:
    def __init__(self):
        self.m1, self.m15 = synthetic()

    def select_symbol(self, s):
        return s != "MISSING"

    def symbol_spec(self, s):
        return SymbolSpec(s, 3, 0.001, 0.001, 1.0, 100.0, 0.01, 1.0, 0.01)

    def rates_range(self, symbol, tf, start, end):
        f = self.m1 if tf == "M1" else self.m15
        return [Bar(int(f.t[i]), f.o[i], f.h[i], f.l[i], f.c[i], int(f.tick_volume[i]), int(f.spread[i]), 0)
                for i in range(len(f))] + [Bar(int(f.t[-1]) + 60, 1, 1, 1, 1, 1, 1, 0)]     # + a forming bar


class TestLab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfgm = ConfigManager()

    def test_signals_use_the_bot_trigger_logic(self):
        from app.research.lab.signals import micro_signals
        m1, m15 = synthetic()
        sig = micro_signals(m1, m15, {"volume_lookback": 20, "minimum_relative_volume": 1.2})
        self.assertGreater(sig["long"].sum() + sig["short"].sum(), 20)
        self.assertFalse((sig["long"] & sig["short"]).any())

    def test_vectorbt_sweep_and_backtrader_check(self):
        from app.research.lab.worker import analyse_symbol
        m1, m15 = synthetic()
        lab = self.cfgm.app.research_lab.model_copy(update={"min_trades": 20, "sl_atr_grid": [1.0, 2.0], "rr_grid": [1.0, 2.0]})
        e = analyse_symbol("XAUUSDm", m1, m15, 0.001, lab, {"volume_lookback": 20, "minimum_relative_volume": 1.2})
        self.assertIn(e["verdict"], ("ROBUST_ON_TEST", "NOT_ROBUST_ON_TEST", "NO_QUALIFYING_PARAMETERS"))
        self.assertEqual(len(e["combos"]), 4)
        if e["best"]:
            self.assertIn("n_trades", e["best"]["train"])
            self.assertIn("n_trades", e["backtrader"])

    def test_too_little_data_is_reported_not_crashed(self):
        from app.research.lab.worker import analyse_symbol
        m1, m15 = synthetic(n=1500)
        e = analyse_symbol("X", m1, m15, 0.001, self.cfgm.app.research_lab, {})
        self.assertEqual(e["verdict"], "INSUFFICIENT_DATA")

    def test_full_cycle_stores_files_db_and_flag(self):
        from app.research.lab import data as labdata, store, worker
        from app.storage.repo import MemoryRepo
        repo = MemoryRepo()
        with tempfile.TemporaryDirectory() as d:
            lab = self.cfgm.app.research_lab.model_copy(update={"min_trades": 20, "sl_atr_grid": [1.0], "rr_grid": [2.0],
                                                                  "symbols": ["XAUUSDm", "MISSING"]})
            cfgm = SimpleNamespace(app=self.cfgm.app.model_copy(update={"research_lab": lab}), strategies=self.cfgm.strategies)
            with mock.patch.object(store, "RESULTS", Path(d) / "res"), mock.patch.object(labdata, "LAB_DATA", Path(d) / "data"):
                res = worker.run_cycle(cfgm, repo, FakeAdapter())
            self.assertEqual(res["symbols"]["MISSING"]["verdict"], "SYMBOL_UNAVAILABLE")
            self.assertTrue((Path(d) / "res" / "latest.json").exists())
            self.assertTrue((Path(d) / "data" / "XAUUSDm_M1.csv").exists())
        self.assertIn("XAUUSDm", repo.get_flag("research_lab_latest")["symbols"])
        self.assertEqual(len(repo.tables["research_lab_runs"]), 2)

    def test_worker_survives_missing_libraries(self):
        from app.research.lab import worker
        with mock.patch.dict("sys.modules", {"vectorbt": None}):
            worker.lab_main(stop_event=None)          # returns instead of raising

    def test_config_and_schema(self):
        self.assertTrue(self.cfgm.app.research_lab.enabled)
        sql = (Path(__file__).parents[2] / "database" / "01_schema.sql").read_text(encoding="utf-8")
        self.assertIn("research_lab_runs", sql)


if __name__ == "__main__":
    unittest.main()
