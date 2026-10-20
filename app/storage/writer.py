"""Two write paths (section 50):

* Critical path  -> `Ledger.critical(...)`: synchronous, on the core thread's own
  connection. If it fails, the event is appended to a local JSONL journal, the
  kill switch is engaged (no new trades) and an alert is raised. Nothing is lost
  silently.
* Telemetry path -> `TelemetryWriter`: in-memory buffer, flushed in batches by a
  background thread on its own connection. Ticks, health, account snapshots,
  logs. Never blocks the trading loop.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.storage.repo import Repository, json_default

log = logging.getLogger(__name__)


class FallbackJournal:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, op: str, table: str, payload: dict, error: str) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "op": op, "table": table, "payload": payload,
               "error": error}
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=json_default) + "\n")


class Ledger:
    """Critical writes with fail-safe behaviour."""

    # Research / analytics tables: a failed write is journaled and logged but must NEVER halt trading.
    # (A missing candidate_evaluations table halted a whole trading day before this exemption existed.)
    NON_CRITICAL_TABLES = frozenset({"candidate_evaluations", "counterfactuals"})

    def __init__(self, repo: Repository, journal: FallbackJournal, on_failure: Callable[[str], None]):
        self.repo = repo
        self.journal = journal
        self.on_failure = on_failure
        self.failures = 0
        self.last_error = ""

    def _guard(self, op: str, table: str, payload: dict, fn: Callable[[], Any]) -> Any:
        try:
            out = fn()
            return out
        except Exception as e:  # noqa: BLE001 - any storage failure is handled the same way
            self.failures += 1
            self.last_error = str(e)
            critical = table not in self.NON_CRITICAL_TABLES
            log.log(logging.ERROR if critical else logging.WARNING,
                    "%sWRITE FAILED %s %s: %s", "CRITICAL " if critical else "non-critical ", op, table, e)
            try:
                self.journal.write(op, table, payload, str(e))
            except Exception as je:  # pragma: no cover
                log.critical("journal write failed too: %s", je)
            if critical:
                self.on_failure(f"{op} {table}: {e}")
            return None

    def insert(self, table: str, row: dict) -> None:
        self._guard("insert", table, row, lambda: self.repo.insert(table, row))

    def update(self, table: str, key: str, key_value: Any, changes: dict) -> None:
        self._guard("update", table, {"key": key, "key_value": key_value, "changes": changes},
                    lambda: self.repo.update(table, key, key_value, changes))

    def upsert(self, table: str, row: dict, conflict: List[str], update_cols: Optional[List[str]] = None) -> None:
        self._guard("upsert", table, row, lambda: self.repo.upsert(table, row, conflict, update_cols))

    def select(self, table: str, **kw) -> List[dict]:
        try:
            return self.repo.select(table, **kw)
        except Exception as e:
            log.error("select %s failed: %s", table, e)
            self.on_failure(f"select {table}: {e}")
            raise


class TelemetryWriter(threading.Thread):
    def __init__(self, repo_factory: Callable[[], Repository], flush_interval: float = 2.0,
                 max_batch: int = 1000, max_buffer: int = 50000):
        super().__init__(name="telemetry-writer", daemon=True)
        self.repo_factory = repo_factory
        self.flush_interval = flush_interval
        self.max_batch = max_batch
        self.max_buffer = max_buffer
        self.q: "queue.Queue[Tuple[str, dict]]" = queue.Queue(maxsize=max_buffer)
        self._stop_evt = threading.Event()
        self.dropped = 0
        self.last_error = ""
        self.healthy = True

    def enqueue(self, table: str, row: dict) -> None:
        try:
            self.q.put_nowait((table, row))
        except queue.Full:
            self.dropped += 1

    def enqueue_many(self, table: str, rows: List[dict]) -> None:
        for r in rows:
            self.enqueue(table, r)

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        repo = self.repo_factory()
        pending: Dict[str, List[dict]] = defaultdict(list)
        last_flush = time.time()
        while not self._stop_evt.is_set() or not self.q.empty():
            try:
                table, row = self.q.get(timeout=0.25)
                pending[table].append(row)
            except queue.Empty:
                pass
            n = sum(len(v) for v in pending.values())
            if n and (n >= self.max_batch or time.time() - last_flush >= self.flush_interval or self._stop_evt.is_set()):
                for table in list(pending):
                    rows = pending[table]
                    try:
                        # group rows with identical column sets for executemany
                        groups: Dict[tuple, List[dict]] = defaultdict(list)
                        for r in rows:
                            groups[tuple(sorted(r))].append(r)
                        for g in groups.values():
                            repo.insert_many(table, g)
                        pending.pop(table)
                        self.healthy = True
                    except Exception as e:  # keep data for a later attempt, bounded
                        self.healthy = False
                        self.last_error = str(e)
                        log.warning("telemetry flush %s failed: %s", table, e)
                        if len(rows) > self.max_buffer // 4:
                            drop = len(rows) - self.max_buffer // 4
                            del rows[:drop]
                            self.dropped += drop
                last_flush = time.time()
        try:
            repo.close()
        except Exception:
            pass

    def flush_now(self, timeout: float = 5.0) -> None:
        """Best-effort: wait until the queue is drained."""
        t0 = time.time()
        while not self.q.empty() and time.time() - t0 < timeout:
            time.sleep(0.05)


class DBLogHandler(logging.Handler):
    """Mirror WARNING+ log records into app_logs via telemetry (Errors tab)."""

    def __init__(self, writer: TelemetryWriter):
        super().__init__(level=logging.WARNING)
        self.writer = writer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.writer.enqueue("app_logs", {"created_at": datetime.now(timezone.utc), "level": record.levelname,
                                             "logger": record.name, "message": record.getMessage()[:2000],
                                             "details": {"module": record.module, "line": record.lineno}})
        except Exception:  # pragma: no cover
            pass
