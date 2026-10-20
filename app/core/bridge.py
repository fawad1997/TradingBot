"""Thread-safe bridge between the trading core thread and the dashboard.

Core -> dashboard : state snapshot (polled) + event stream (queue).
Dashboard -> core : command queue; the core validates and executes each
command on its own thread and answers through a Future. The dashboard never
touches the broker, the strategies or the risk engine directly.
"""
from __future__ import annotations

import copy
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Dict, List, Optional, Tuple


class Bridge:
    def __init__(self, max_events: int = 5000):
        self._state: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self.events: "queue.Queue[dict]" = queue.Queue(maxsize=max_events)
        self.commands: "queue.Queue[Tuple[str, dict, Future]]" = queue.Queue()
        self.recent: List[dict] = []
        self.started_at = time.time()

    # ---- state
    def update_state(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    def get_state(self) -> Dict[str, Any]:
        with self._lock:
            s = copy.deepcopy(self._state)
        s["uptime_sec"] = round(time.time() - self.started_at)
        return s

    # ---- events
    def emit(self, event: str, payload: dict) -> None:
        msg = {"event": event, "ts": time.time(), **payload}
        with self._lock:
            self.recent.append(msg)
            if len(self.recent) > 300:
                del self.recent[:100]
        try:
            self.events.put_nowait(msg)
        except queue.Full:
            try:
                self.events.get_nowait()
                self.events.put_nowait(msg)
            except queue.Empty:
                pass

    def recent_events(self, n: int = 100) -> List[dict]:
        with self._lock:
            return list(self.recent[-n:])

    # ---- commands
    def submit(self, name: str, payload: dict, timeout: float = 10.0) -> dict:
        fut: Future = Future()
        self.commands.put((name, payload, fut))
        try:
            return fut.result(timeout=timeout)
        except Exception as e:  # timeout or command error
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def drain_commands(self, limit: int = 20) -> List[Tuple[str, dict, Future]]:
        out = []
        for _ in range(limit):
            try:
                out.append(self.commands.get_nowait())
            except queue.Empty:
                break
        return out
