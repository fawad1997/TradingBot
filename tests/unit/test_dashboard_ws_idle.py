"""A dashboard connection that goes silent is closed and stops being written to.

23 Oct 2026: the broadcaster had been writing to about 75 dead connections for two days. A connection whose peer
has gone can stay open to asyncio indefinitely, and a write to it raises nothing - asyncio drops the data and logs
"socket.send() raised exception." - so the broadcaster's except-and-remove never ran. That was 300 warnings a
second, 47 million rows and 9.7 GB of app_logs, and log rotation every 16 minutes.

The page pings every 20 s, so silence is the one signal that does not depend on asyncio noticing anything.
"""
import asyncio
import time
import unittest
from unittest import mock

from starlette.websockets import WebSocketDisconnect

from app.config import ConfigManager
from app.core.bridge import Bridge
from app.dashboard import api
from app.storage.repo import MemoryRepo


class FakeSocket:
    """A dashboard page: it pings `pings` times, then goes silent like a peer that vanished (or disconnects)."""

    def __init__(self, pings=0, then_disconnect=False):
        self.pings, self.then_disconnect = pings, then_disconnect
        self.sent, self.closed = 0, None

    async def accept(self):
        pass

    async def send_text(self, data):
        self.sent += 1

    async def receive_text(self):
        if self.pings:
            self.pings -= 1
            await asyncio.sleep(0.05)
            return "ping"
        if self.then_disconnect:
            raise WebSocketDisconnect(1001)
        await asyncio.Event().wait()          # nothing ever arrives

    async def close(self, code=1000, reason=None):
        self.closed = code


def app_and_endpoint(bridge):
    app = api.create_app(bridge, MemoryRepo(), ConfigManager(), "tok")
    return app, next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/ws")


class TestIdleConnections(unittest.TestCase):
    def test_a_silent_connection_is_closed_and_no_longer_broadcast_to(self):
        bridge = Bridge()
        app, endpoint = app_and_endpoint(bridge)

        async def scenario():
            async with app.router.lifespan_context(app):
                ws = FakeSocket()
                handler = asyncio.create_task(endpoint(ws))
                await asyncio.sleep(0.1)
                bridge.emit("tick_update", {"bid": 1.0})
                await asyncio.sleep(0.4)                  # the broadcaster sends every 0.25 s
                while_open = ws.sent
                await asyncio.wait_for(handler, 3.0)      # ends by itself: the idle timeout
                after_close = ws.sent
                bridge.emit("tick_update", {"bid": 2.0})
                await asyncio.sleep(0.6)
                return while_open, after_close, ws.sent, ws.closed

        with mock.patch.object(api, "WS_IDLE_SEC", 1.0):
            while_open, after_close, final, closed = asyncio.run(scenario())
        self.assertGreaterEqual(while_open, 2, "the state on connect, then the broadcast")
        self.assertEqual(final, after_close, "a closed connection must not be written to again")
        self.assertEqual(closed, 1001, "closing tells a live but throttled tab to reconnect")

    def test_a_pinging_connection_is_kept(self):
        _, endpoint = app_and_endpoint(Bridge())
        ws = FakeSocket(pings=12, then_disconnect=True)           # 0.6 s of pings, then a normal disconnect
        with mock.patch.object(api, "WS_IDLE_SEC", 0.2):
            t0 = time.perf_counter()
            asyncio.run(asyncio.wait_for(endpoint(ws), 3.0))
        self.assertGreater(time.perf_counter() - t0, 0.55, "pings arriving inside the window keep it open")
        self.assertIsNone(ws.closed, "a client that disconnected itself is not closed again")

    def test_the_wait_is_longer_than_a_throttled_tab_takes_to_ping(self):
        self.assertGreaterEqual(api.WS_IDLE_SEC, 120.0)


if __name__ == "__main__":
    unittest.main()
