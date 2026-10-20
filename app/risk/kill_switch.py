"""Emergency kill switch (section 60).

Engaged  -> no new signal authorization, no new orders.
Still on -> monitoring, reconciliation, broker-side SL/TP protection, exit management.
Closing existing positions is a SEPARATE explicit action.
The state is durable (system_flags) so it survives a restart.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable, Optional


class KillSwitch:
    def __init__(self, load: Callable[[], Optional[dict]], save: Callable[[dict], None]):
        self._load = load
        self._save = save
        self._lock = threading.Lock()
        self.engaged = False
        self.reason = ""
        self.by = ""
        self.at: Optional[str] = None
        self.volatile_reasons: set = set()   # engaged in memory only (e.g. DB down -> can't persist)

    def restore(self) -> None:
        st = None
        try:
            st = self._load()
        except Exception:
            st = None
        if st:
            self.engaged = bool(st.get("engaged"))
            self.reason = st.get("reason", "")
            self.by = st.get("by", "")
            self.at = st.get("at")

    def engage(self, reason: str, by: str) -> dict:
        with self._lock:
            self.engaged, self.reason, self.by = True, reason, by
            self.at = datetime.now(timezone.utc).isoformat()
            rec = self.as_dict()
        try:
            self._save(rec)
        except Exception:
            self.volatile_reasons.add(reason)
        return rec

    def release(self, by: str, reason: str) -> dict:
        with self._lock:
            self.engaged, self.reason, self.by = False, f"released: {reason}", by
            self.at = datetime.now(timezone.utc).isoformat()
            self.volatile_reasons.clear()
            rec = self.as_dict()
        self._save(rec)
        return rec

    def engage_volatile(self, reason: str) -> None:
        """Block trading without touching storage (used when storage itself failed)."""
        self.volatile_reasons.add(reason)

    @property
    def blocking(self) -> bool:
        return self.engaged or bool(self.volatile_reasons)

    def as_dict(self) -> dict:
        return {"engaged": self.engaged, "reason": self.reason, "by": self.by, "at": self.at,
                "volatile": sorted(self.volatile_reasons)}
