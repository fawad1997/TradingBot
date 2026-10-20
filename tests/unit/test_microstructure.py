import unittest
from datetime import datetime, timedelta, timezone

from app.microstructure.book import BookTracker
from app.microstructure.flow import FlowAnalyzer
from tests.helpers import started_core

T0 = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)


def feed(f, prices, step=0.2):
    for i, p in enumerate(prices):
        f.on_tick(p - 0.05, p + 0.05, T0 + timedelta(seconds=i * step))


class TestFlow(unittest.TestCase):
    def test_rising_prices_give_positive_flow_and_toxicity(self):
        f = FlowAnalyzer(0.01, bucket_ticks=20, vpin_buckets=10)
        feed(f, [100 + i * 0.01 for i in range(400)])
        s = f.snapshot()
        self.assertGreater(s["cvd"], 300)
        self.assertGreater(s["aggression_buy_pct"], 90)
        self.assertGreater(s["vpin"], 0.8)
        self.assertEqual(s["vpin_label"], "HIGH")
        self.assertEqual(s["open_interest"], "UNAVAILABLE_FOR_CFD")

    def test_balanced_flow_low_vpin_and_profile(self):
        f = FlowAnalyzer(0.01, bucket_ticks=20, vpin_buckets=10, bin_points=10)
        feed(f, [100 + (0.02 if i % 2 else 0.0) for i in range(600)])
        s = f.snapshot()
        self.assertLess(s["vpin"], 0.2)
        self.assertTrue(s["val"] <= s["poc"] <= s["vah"])
        self.assertTrue(abs(s["cvd"]) < 5)

    def test_vwap_deviation_and_day_reset(self):
        f = FlowAnalyzer(0.01)
        feed(f, [100.0] * 100 + [100.5] * 5)
        self.assertGreater(f.snapshot()["vwap_dev_sigma"], 1)
        f.on_tick(1.0, 1.1, T0 + timedelta(days=1))
        self.assertEqual(f.snapshot()["ticks"], 1)

    def test_burst_detected(self):
        f = FlowAnalyzer(0.01, burst_z=2.0)
        feed(f, [100 + (0.02 if i % 2 else 0.0) for i in range(400)])
        feed(f, [100 + i * 0.02 for i in range(1, 25)])
        self.assertEqual(f.snapshot()["burst"], "BUY_BURST")


class TestProfileRobustness(unittest.TestCase):
    def test_gapped_and_wide_profiles_terminate(self):
        import random, time
        f = FlowAnalyzer(0.001, bin_points=20)
        random.seed(1)
        p = 4195.0
        start = time.perf_counter()
        for i in range(20000):
            p += random.choice([-1, 1]) * random.choice([0.02, 0.5])      # jumps leave empty bins
            f.on_tick(p - 0.1, p + 0.1, T0 + timedelta(seconds=i * 0.25))
        for _ in range(20):
            f.snapshot()
        self.assertLess(time.perf_counter() - start, 5.0)
        s = f.snapshot()
        self.assertTrue(s["val"] <= s["poc"] <= s["vah"])

    def test_single_bin_profile(self):
        f = FlowAnalyzer(0.01)
        feed(f, [100.0] * 10)
        s = f.snapshot()
        self.assertEqual(s["val"], s["vah"])


class TestBook(unittest.TestCase):
    def test_unavailable_and_walls(self):
        b = BookTracker()
        self.assertEqual(b.update(None)["status"], "UNAVAILABLE")
        lv = [(99.9 - i * 0.1, 10, "BID") for i in range(5)] + [(100.1 + i * 0.1, 10, "ASK") for i in range(5)]
        lv[1] = (99.8, 100, "BID")
        r = b.update(lv)
        self.assertEqual(r["status"], "OK")
        self.assertEqual(len(r["walls"]), 1)
        lv2 = list(lv)
        lv2[1] = (99.8, 5, "BID")
        self.assertEqual(len(b.update(lv2)["pulled_walls"]), 1)

    def test_iceberg_refill_hint(self):
        b = BookTracker(refill_alert=2)
        base = [(99.9 - i * 0.1, 10, "BID") for i in range(5)] + [(100.1 + i * 0.1, 10, "ASK") for i in range(5)]
        wall = base[:1] + [(99.8, 100, "BID")] + base[2:]
        gone = base[:1] + [(99.8, 5, "BID")] + base[2:]
        for _ in range(3):
            b.update(wall)
            b.update(gone)
        b.update(wall)
        self.assertTrue(b.last["possible_icebergs"])


class TestEngine(unittest.TestCase):
    def test_core_publishes_snapshot_and_stores_with_signal(self):
        core, sim, broker, _ = started_core()
        for _ in range(5):
            core.step()
        snap = core.micro_snapshot()
        self.assertIn(snap["status"], ("OK", "NO_DATA"))
        self.assertEqual(snap["book"]["status"], "UNAVAILABLE")
        core._publish()
        self.assertIn(core.symbol, core.bridge.get_state()["microstructure"])


if __name__ == "__main__":
    unittest.main()
