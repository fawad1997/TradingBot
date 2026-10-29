import unittest
from types import SimpleNamespace

from app.core.bridge import Bridge
from app.core.engine import StartupError
from app.core.multi import MultiAssetRunner
from app.risk.kill_switch import KillSwitch


class FakeCore:
    def __init__(self, symbol, runner, fail_start=False, flags=None):
        self.symbol = symbol
        self.runner = runner
        self.fail_start = fail_start
        flags = {} if flags is None else flags
        self.kill = KillSwitch(lambda: flags.get("kill_switch"), lambda st: flags.update(kill_switch=st))
        self.kill.restore()
        self.data = SimpleNamespace(is_connected=lambda: True, shutdown=lambda: None)
        self.pending = []
        self._consecutive_errors = 0
        self.steps = 0
        self.stopped = False

    def start(self):
        if self.fail_start:
            raise StartupError("symbol history unavailable")

    def step(self):
        self.steps += 1
        if self.symbol == "GBPUSDm":
            self.runner.stop_event.set()

    def market_state(self):
        return {
            "is_open": True,
            "schedule_type": "estimated_weekend_hours",
            "basis": "test",
            "quote_age_sec": 0.0,
        }

    def shutdown(self):
        self.stopped = True


class TestMultiAssetRunner(unittest.TestCase):
    def test_scans_each_available_symbol_and_skips_unavailable_symbols(self):
        bridge = Bridge()
        runner = None
        created = []

        def factory(symbol):
            core = FakeCore(symbol, runner, fail_start=symbol == "BTCUSDm")
            created.append(core)
            return core

        runner = MultiAssetRunner(None, ["XAUUSDm", "BTCUSDm", "GBPUSDm"], factory, bridge)
        runner.start()
        self.assertEqual([core.symbol for core in runner.cores], ["XAUUSDm", "GBPUSDm"])
        self.assertTrue(all(core.portfolio_cores is runner.cores for core in runner.cores))
        market_status = bridge.get_state()["symbol_market_status"]
        self.assertEqual(set(market_status), {"XAUUSDm", "GBPUSDm"})
        self.assertTrue(all(status["schedule_type"] == "estimated_weekend_hours"
                            for status in market_status.values()))
        runner.run()
        self.assertEqual([core.steps for core in runner.cores], [1, 1])
        self.assertTrue(all(core.stopped for core in runner.cores))

    def test_one_kill_switch_halts_and_resumes_every_symbol(self):
        flags = {"kill_switch": {"engaged": True, "reason": "EMERGENCY risk state at startup", "by": "risk"}}
        runner = MultiAssetRunner(None, ["XAUUSDm", "EURUSDm", "GBPUSDm"],
                                  lambda s: FakeCore(s, None, flags=flags), Bridge())
        runner.start()
        self.assertTrue(all(core.kill.blocking for core in runner.cores))
        runner.cores[1].kill.release("dashboard", "resume")       # whichever core drained the command
        self.assertFalse(any(core.kill.blocking for core in runner.cores), "resume must reach every symbol")
        runner.cores[2].kill.engage("manual halt", "dashboard")
        self.assertTrue(all(core.kill.blocking for core in runner.cores), "a halt must stop every symbol")


if __name__ == "__main__":
    unittest.main()
