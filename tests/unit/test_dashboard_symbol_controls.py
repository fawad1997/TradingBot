import unittest

from starlette.testclient import TestClient

from app.core.bridge import Bridge
from app.config import ConfigManager
from app.dashboard.api import create_app
from app.storage.repo import MemoryRepo


class TestSymbolControlApi(unittest.TestCase):
    def test_requires_token_and_forwards_command(self):
        bridge = Bridge()
        client = TestClient(create_app(bridge, MemoryRepo(), ConfigManager(), "tok"))
        r = client.post("/api/control/set_symbol_trading", json={"symbol": "XAUUSDm", "enabled": False})
        self.assertEqual(r.status_code, 403)
        import threading

        def core():
            for _ in range(100):
                for name, payload, fut in bridge.drain_commands():
                    fut.set_result({"ok": True, "echo": [name, payload["symbol"], payload["enabled"]]})
                    return
                threading.Event().wait(0.05)
        t = threading.Thread(target=core)
        t.start()
        r = client.post("/api/control/set_symbol_trading", headers={"X-Dashboard-Token": "tok"},
                        json={"symbol": "XAUUSD247m", "enabled": True})
        t.join()
        self.assertEqual(r.json()["echo"], ["set_symbol_trading", "XAUUSD247m", True])

    def test_static_page_has_the_symbol_controls(self):
        """The per-symbol start/stop control must be on the overview page.

        It was a table (#symbolSwitch) until the 16 Oct redesign and is now a grid of clickable tiles
        (#symbolTiles). The control is what matters, not its shape, so this checks the element the page
        actually renders into plus the wording that tells the operator what stopping a symbol does.
        """
        page = TestClient(create_app(Bridge(), MemoryRepo(), ConfigManager(), "tok")).get("/").text
        self.assertIn("symbolTiles", page)
        self.assertIn("start or stop new trades", page)


if __name__ == "__main__":
    unittest.main()


class TestDisplayTimezone(unittest.TestCase):
    def test_state_carries_zone_and_validation(self):
        client = TestClient(create_app(Bridge(), MemoryRepo(), ConfigManager(), "tok"))
        self.assertEqual(client.get("/api/state").json()["display_timezone"], "Asia/Karachi")
        from app.config import DisplayCfg
        with self.assertRaises(Exception):
            DisplayCfg(timezone="Mars/Olympus")
