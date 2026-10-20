"""Storage repositories.

`Repository` is a deliberately small table API (insert / update / upsert /
select / flags). Two implementations:
  * PostgresRepo - production (psycopg 3). Casts every parameter to the real
    column type read from information_schema, so uuid / jsonb / timestamptz
    columns never depend on driver guesswork, and unknown columns fail loudly.
  * MemoryRepo   - tests and `--simulate` runs without a database.
Each thread that writes must own its own PostgresRepo (one connection each).
"""
from __future__ import annotations

import copy
import json
import logging
import math
import re
import threading
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, cast

log = logging.getLogger(__name__)
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_ACTIVE_RISK_STATES = ("APPROVED", "ORDER_SENT", "OPEN", "PROTECTION", "PARTIAL", "RUNNER",
                       "USER_MODIFIED", "RECONCILIATION_REQUIRED")
_TERMINAL_RISK_STATES = ("CLOSED", "REJECTED", "CANCELLED", "BROKER_CLOSED", "ERROR")


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"illegal identifier {name!r}")
    return name


def json_default(o: Any):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if hasattr(o, "value"):          # enums
        return o.value
    if hasattr(o, "__dict__"):
        return o.__dict__
    return str(o)


def to_jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=json_default))


def _reservation_block_reason(trades: List[dict], reservations: List[dict], signal_id: str, symbol: str,
                              risk_money: float, max_total_risk: float, max_correlated_risk: float,
                              correlation_threshold: float, correlations: Dict[str, Optional[float]],
                              external_risk_by_symbol: Dict[str, float],
                              portfolio_cfg: Optional[dict] = None,
                              direction: int = 1,
                              directions_by_symbol: Optional[Dict[str, int]] = None) -> Optional[str]:
    """Why this reservation must be refused, or None to admit it.

    `portfolio_cfg` switches how several open positions are added together. Absent or
    mode="gross" keeps the original arithmetic sum exactly. mode="diversified" routes the total
    through app/risk/portfolio.py, which counts correlation and direction, so two genuinely
    independent strategies no longer each consume the full budget."""
    if not math.isfinite(risk_money) or risk_money <= 0:
        return "RESERVATION_RISK_INVALID"
    reserved_by_signal = {str(r["signal_id"]): r for r in reservations if r.get("signal_id")}
    exposure: Dict[str, float] = defaultdict(float)
    for trade in trades:
        trade_signal = str(trade.get("signal_id") or "")
        if trade_signal == signal_id or trade_signal in reserved_by_signal:
            continue
        amount = trade.get("initial_risk_money")
        if amount is None or not math.isfinite(float(amount)) or float(amount) <= 0:
            return "OPEN_TRADE_RISK_UNKNOWN"
        exposure[trade.get("symbol") or "UNKNOWN"] += float(amount)
    for reservation in reservations:
        if str(reservation.get("signal_id")) == signal_id:
            continue
        amount = reservation.get("risk_money")
        if amount is None or not math.isfinite(float(amount)) or float(amount) <= 0:
            return "RESERVED_RISK_INVALID"
        exposure[reservation.get("symbol") or "UNKNOWN"] += float(amount)
    for external_symbol, amount in external_risk_by_symbol.items():
        if not math.isfinite(float(amount)) or float(amount) < 0:
            return "EXTERNAL_RISK_UNAVAILABLE"
        exposure[external_symbol] += float(amount)

    existing_total = sum(exposure.values())
    if not math.isfinite(existing_total):
        return "PORTFOLIO_OPEN_RISK_LIMIT"

    mode = (portfolio_cfg or {}).get("mode", "gross")
    if mode == "diversified":
        return _diversified_block_reason(symbol, risk_money, direction, exposure, correlations,
                                         directions_by_symbol or {}, max_total_risk, portfolio_cfg or {})

    if existing_total + risk_money > max_total_risk:
        return "PORTFOLIO_OPEN_RISK_LIMIT"
    correlated_total = risk_money
    for other_symbol, amount in exposure.items():
        if amount <= 0:
            continue
        if other_symbol == symbol:
            correlation = 1.0
        else:
            correlation = correlations.get(other_symbol)
            if correlation is None or not math.isfinite(float(correlation)):
                return f"CORRELATION_HISTORY_INSUFFICIENT:{other_symbol}"
        # NOTE: abs() here cannot tell "doubling up" from "partly offset" - direction is not consulted
        # at all in gross mode. The diversified mode fixes that; this branch is kept byte-identical so
        # switching modes is the only thing that changes behaviour.
        if abs(float(correlation)) >= correlation_threshold:
            correlated_total += amount
    if correlated_total > max_correlated_risk:
        return "CORRELATED_OPEN_RISK_LIMIT"
    return None


def _diversified_block_reason(symbol: str, risk_money: float, direction: int,
                              exposure: Dict[str, float], correlations: Dict[str, Optional[float]],
                              directions_by_symbol: Dict[str, int], max_total_risk: float,
                              pcfg: dict) -> Optional[str]:
    """Portfolio-variance aggregation. See app/risk/portfolio.py for the model and its three ceilings."""
    from app.risk.portfolio import Leg, admits

    open_legs = [Leg(sym, amount, directions_by_symbol.get(sym, 1))
                 for sym, amount in exposure.items() if amount > 0]
    candidate = Leg(symbol, risk_money, direction)

    # The engine supplies correlations as {other_symbol: rho} relative to the candidate. Widen it to the
    # nested form combine() expects; pairs it does not cover fall back to missing_correlation_value.
    nested: Dict[str, Dict[str, Optional[float]]] = {symbol: dict(correlations)}

    ok, reason, _ = admits(
        candidate, open_legs, nested,
        budget=max_total_risk,
        gross_budget=float(pcfg.get("gross_budget", max_total_risk)),
        stress_budget=max_total_risk * float(pcfg.get("stressed_risk_tolerance", 1.25)),
        diversification_floor=float(pcfg.get("diversification_floor", 0.0)),
        stress_correlation=float(pcfg.get("stress_correlation", 0.8)),
        missing_correlation_value=float(pcfg.get("missing_correlation_value", 1.0)))
    return None if ok else reason


class StorageError(Exception):
    pass


class Repository(ABC):
    @abstractmethod
    def insert(self, table: str, row: Dict[str, Any]) -> None: ...

    @abstractmethod
    def insert_many(self, table: str, rows: List[Dict[str, Any]]) -> None: ...

    @abstractmethod
    def update(self, table: str, key: str, key_value: Any, changes: Dict[str, Any]) -> int: ...

    @abstractmethod
    def upsert(self, table: str, row: Dict[str, Any], conflict: Sequence[str],
               update_cols: Optional[Sequence[str]] = None) -> None: ...

    @abstractmethod
    def select(self, table: str, where: Optional[Dict[str, Any]] = None, order_by: Optional[str] = None,
               desc: bool = False, limit: Optional[int] = None) -> List[Dict[str, Any]]: ...

    def query(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError("raw SQL requires PostgresRepo")

    def reserve_portfolio_risk(self, account_key: str, signal_id: str, trade_id: str, symbol: str,
                               risk_money: float, max_total_risk: float, max_correlated_risk: float,
                               correlation_threshold: float, correlations: Dict[str, Optional[float]],
                               external_risk_by_symbol: Dict[str, float], expires_at: datetime,
                               portfolio_cfg: Optional[dict] = None,
                               direction: int = 1,
                               directions_by_symbol: Optional[Dict[str, int]] = None) -> Optional[str]:
        raise NotImplementedError("portfolio risk reservations require a repository implementation")

    def release_portfolio_risk(self, account_key: str, signal_id: str) -> int:
        raise NotImplementedError("portfolio risk reservations require a repository implementation")

    def recover_portfolio_risk_reservations(self, account_key: str, now: datetime,
                                            lease: timedelta) -> int:
        raise NotImplementedError("portfolio risk reservations require a repository implementation")

    def healthy(self) -> bool:
        return True

    # ---- flags (durable switches)
    def get_flag(self, key: str, default: Any = None) -> Any:
        rows = self.select("system_flags", {"key": key})
        return rows[0]["value"] if rows else default

    def set_flag(self, key: str, value: Any, by: str = "system") -> None:
        self.upsert("system_flags", {"key": key, "value": value, "updated_at": datetime.now(timezone.utc),
                                     "updated_by": by}, ["key"])

    def close(self) -> None:
        pass


# ============================================================== memory
class MemoryRepo(Repository):
    def __init__(self):
        self.tables: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._lock = threading.RLock()
        self._seq: Dict[str, int] = defaultdict(int)
        self.fail_writes = False     # fault injection: simulate database outage

    def _check(self):
        if self.fail_writes:
            raise StorageError("simulated database failure")

    def insert(self, table, row):
        with self._lock:
            self._check()
            r = {k: (copy.deepcopy(v) if isinstance(v, (dict, list)) else v) for k, v in row.items()}
            if "id" not in r:
                self._seq[table] += 1
                r["id"] = self._seq[table]
            r.setdefault("created_at", datetime.now(timezone.utc))
            self.tables[table].append(r)

    def insert_many(self, table, rows):
        for r in rows:
            self.insert(table, r)

    def update(self, table, key, key_value, changes):
        with self._lock:
            self._check()
            n = 0
            for r in self.tables[table]:
                if r.get(key) == key_value:
                    r.update(copy.deepcopy(changes))
                    n += 1
            return n

    def upsert(self, table, row, conflict, update_cols=None):
        with self._lock:
            self._check()
            for r in self.tables[table]:
                if all(r.get(c) == row.get(c) for c in conflict):
                    cols = update_cols if update_cols is not None else [k for k in row if k not in conflict]
                    for c in cols:
                        r[c] = copy.deepcopy(row[c])
                    return
            self.insert(table, row)

    def select(self, table, where=None, order_by=None, desc=False, limit=None):
        with self._lock:
            rows = []
            for r in self.tables.get(table, []):
                ok = True
                for k, v in (where or {}).items():
                    if isinstance(v, (list, tuple, set)):
                        if r.get(k) not in v:
                            ok = False
                    elif r.get(k) != v:
                        ok = False
                if ok:
                    rows.append({k: (copy.deepcopy(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
            if order_by:
                rows.sort(key=lambda r: (r.get(order_by) is None, r.get(order_by) if r.get(order_by) is not None else 0),
                          reverse=desc)
            return rows[:limit] if limit else rows

    def reserve_portfolio_risk(self, account_key: str, signal_id: str, trade_id: str, symbol: str,
                               risk_money: float, max_total_risk: float, max_correlated_risk: float,
                               correlation_threshold: float, correlations: Dict[str, Optional[float]],
                               external_risk_by_symbol: Dict[str, float],
                               expires_at: datetime,
                               portfolio_cfg: Optional[dict] = None,
                               direction: int = 1,
                               directions_by_symbol: Optional[Dict[str, int]] = None) -> Optional[str]:
        with self._lock:
            self._check()
            now = datetime.now(timezone.utc)
            active_trades = [r for r in self.tables.get("trades", [])
                             if r.get("state") in _ACTIVE_RISK_STATES]
            by_trade_id = {str(r.get("id")): r for r in active_trades}
            all_trades_by_id = {str(r.get("id")): r for r in self.tables.get("trades", [])}
            rows = [r for r in self.tables.get("risk_reservations", [])
                    if r.get("account_key") == account_key and r.get("state") == "RESERVED"]
            active_reservations = []
            for row in rows:
                trade_id = str(row.get("trade_id"))
                trade = by_trade_id.get(trade_id)
                if trade is not None:
                    row["expires_at"] = now + timedelta(days=1)
                    row["updated_at"] = now
                    active_reservations.append(row)
                elif all_trades_by_id.get(trade_id, {}).get("state") in _TERMINAL_RISK_STATES:
                    row["state"] = "RELEASED"
                    row["updated_at"] = now
                elif row.get("expires_at") and row["expires_at"] > now:
                    active_reservations.append(row)
                else:
                    row["state"] = "EXPIRED"
                    row["updated_at"] = now
            reason = _reservation_block_reason(
                active_trades, active_reservations, signal_id, symbol, risk_money, max_total_risk,
                max_correlated_risk, correlation_threshold, correlations, external_risk_by_symbol,
                portfolio_cfg, direction, directions_by_symbol)
            if reason:
                return reason
            existing = next((r for r in self.tables.get("risk_reservations", [])
                             if r.get("account_key") == account_key and str(r.get("signal_id")) == signal_id), None)
            values = {"account_key": account_key, "signal_id": signal_id, "trade_id": trade_id,
                      "symbol": symbol, "risk_money": risk_money, "state": "RESERVED",
                      "expires_at": expires_at, "updated_at": now}
            if existing is None:
                values["id"] = str(uuid.uuid4())
                values["created_at"] = now
                self.insert("risk_reservations", values)
            else:
                existing.update(values)
            return None

    def release_portfolio_risk(self, account_key: str, signal_id: str) -> int:
        with self._lock:
            self._check()
            changed = 0
            for row in self.tables.get("risk_reservations", []):
                if row.get("account_key") == account_key and str(row.get("signal_id")) == signal_id \
                        and row.get("state") == "RESERVED":
                    row["state"] = "RELEASED"
                    row["updated_at"] = datetime.now(timezone.utc)
                    changed += 1
            return changed

    def recover_portfolio_risk_reservations(self, account_key: str, now: datetime, lease: timedelta) -> int:
        with self._lock:
            self._check()
            active_trades = {str(r.get("id")): r for r in self.tables.get("trades", [])
                             if r.get("state") in _ACTIVE_RISK_STATES}
            changed = 0
            for row in self.tables.get("risk_reservations", []):
                if row.get("account_key") != account_key or row.get("state") != "RESERVED":
                    continue
                trade = active_trades.get(str(row.get("trade_id")))
                if trade is not None:
                    row["expires_at"] = now + lease
                    row["updated_at"] = now
                    changed += 1
                elif any(str(t.get("id")) == str(row.get("trade_id"))
                         and t.get("state") in _TERMINAL_RISK_STATES for t in self.tables.get("trades", [])):
                    row["state"] = "RELEASED"
                    row["updated_at"] = now
                    changed += 1
                elif row.get("expires_at") and row["expires_at"] <= now:
                    row["state"] = "EXPIRED"
                    row["updated_at"] = now
                    changed += 1
            return changed


# ============================================================== postgres
class PostgresRepo(Repository):
    def __init__(self, dsn: str, connect: Optional[Callable] = None, json_wrapper: Optional[Callable] = None,
                 application_name: str = "quantara"):
        self.dsn = dsn
        self.application_name = application_name
        self._connect = connect
        self._json = json_wrapper
        self._conn = None
        self._lock = threading.RLock()
        self._columns: Dict[str, Dict[str, str]] = {}
        if self._connect is None:
            try:
                import psycopg
                from psycopg.rows import dict_row
                from psycopg.types.json import Jsonb
            except ImportError as e:  # pragma: no cover
                raise StorageError("psycopg is not installed: pip install \"psycopg[binary]\"") from e
            self._psycopg = psycopg
            self._connect = lambda: psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=5,
                                                    application_name=application_name)
            self._json = Jsonb
            self._op_errors = (psycopg.OperationalError, psycopg.InterfaceError)
        else:
            self._op_errors = (ConnectionError,)

    # ---- connection
    def _connection(self):
        if self._conn is None or getattr(self._conn, "closed", False):
            self._conn = self._connect()
        return self._conn

    def _run(self, sql: str, params: Optional[Sequence[Any]] = None, fetch: bool = False,
             many: Optional[List[Sequence[Any]]] = None):
        with self._lock:
            last_err = None
            for attempt in (1, 2):
                try:
                    conn = self._connection()
                    with conn.cursor() as cur:
                        if many is not None:
                            cur.executemany(sql, many)
                        else:
                            cur.execute(sql, params)
                        if fetch:
                            return cur.fetchall()
                        return getattr(cur, "rowcount", 0)
                except self._op_errors as e:   # connection problem: reconnect once
                    last_err = e
                    self._conn = None
                    continue
                except Exception as e:
                    raise StorageError(f"{e} | SQL: {sql[:300]}") from e
            raise StorageError(f"database unavailable: {last_err}")

    def healthy(self) -> bool:
        try:
            self._run("SELECT 1", fetch=True)
            return True
        except Exception:
            return False

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ---- column type discovery
    def columns(self, table: str) -> Dict[str, str]:
        if table not in self._columns:
            rows = self._run("SELECT column_name, data_type, udt_name FROM information_schema.columns "
                             "WHERE table_schema = 'public' AND table_name = %s", [table], fetch=True)
            if not rows:
                raise StorageError(f"table '{table}' does not exist - run database/01_schema.sql")
            self._columns[table] = {r["column_name"]: r["udt_name"] for r in rows}
        return self._columns[table]

    def _cast(self, table: str, col: str) -> str:
        cols = self.columns(table)
        if col not in cols:
            raise StorageError(f"column '{col}' does not exist in table '{table}'")
        udt = cols[col]
        return {"uuid": "::uuid", "jsonb": "::jsonb", "json": "::json", "timestamptz": "::timestamptz",
                "date": "::date", "int8": "::bigint", "int4": "::integer", "float8": "::double precision",
                "bool": "::boolean", "text": "::text"}.get(udt, "")

    def _value(self, table: str, col: str, v: Any) -> Any:
        udt = self.columns(table).get(col)
        if v is None:
            return None
        if udt in ("jsonb", "json"):
            return self._json(to_jsonable(v))
        if hasattr(v, "value") and not isinstance(v, (int, float, str)):
            return v.value
        if isinstance(v, str) and hasattr(v, "value"):
            return str(v.value)
        return v

    def _placeholders(self, table: str, cols: Iterable[str]) -> str:
        return ", ".join("%s" + self._cast(table, c) for c in cols)

    # ---- API
    def insert(self, table, row):
        t = _ident(table)
        cols = [_ident(c) for c in row]
        sql = f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({self._placeholders(t, cols)})"
        self._run(sql, [self._value(t, c, row[c]) for c in cols])

    def insert_many(self, table, rows):
        if not rows:
            return
        t = _ident(table)
        cols = sorted({c for r in rows for c in r})
        for c in cols:
            _ident(c)
        sql = f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({self._placeholders(t, cols)})"
        self._run(sql, many=[[self._value(t, c, r.get(c)) for c in cols] for r in rows])

    def update(self, table, key, key_value, changes):
        if not changes:
            return 0
        t = _ident(table)
        cols = [_ident(c) for c in changes]
        sets = ", ".join(f"{c} = %s{self._cast(t, c)}" for c in cols)
        sql = f"UPDATE {t} SET {sets} WHERE {_ident(key)} = %s{self._cast(t, key)}"
        return self._run(sql, [self._value(t, c, changes[c]) for c in cols] + [self._value(t, key, key_value)]) or 0

    def upsert(self, table, row, conflict, update_cols=None):
        t = _ident(table)
        cols = [_ident(c) for c in row]
        upd = list(update_cols) if update_cols is not None else [c for c in cols if c not in conflict]
        conflict_sql = ", ".join(_ident(c) for c in conflict)
        if upd:
            action = "DO UPDATE SET " + ", ".join(f"{_ident(c)} = EXCLUDED.{_ident(c)}" for c in upd)
        else:
            action = "DO NOTHING"
        sql = (f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({self._placeholders(t, cols)}) "
               f"ON CONFLICT ({conflict_sql}) {action}")
        self._run(sql, [self._value(t, c, row[c]) for c in cols])

    def select(self, table, where=None, order_by=None, desc=False, limit=None):
        t = _ident(table)
        clauses, params = [], []
        for k, v in (where or {}).items():
            k = _ident(k)
            if isinstance(v, (list, tuple, set)):
                v = list(v)
                if not v:
                    return []
                clauses.append(f"{k} IN ({', '.join(['%s' + self._cast(t, k)] * len(v))})")
                params.extend(self._value(t, k, x) for x in v)
            elif v is None:
                clauses.append(f"{k} IS NULL")
            else:
                clauses.append(f"{k} = %s{self._cast(t, k)}")
                params.append(self._value(t, k, v))
        sql = f"SELECT * FROM {t}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if order_by:
            sql += f" ORDER BY {_ident(order_by)} {'DESC' if desc else 'ASC'}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = self._run(sql, params, fetch=True) or []
        return [self._normalize(r) for r in rows]

    def query(self, sql, params=None):
        rows = self._run(sql, params or [], fetch=True) or []
        return [self._normalize(r) for r in rows]

    def reserve_portfolio_risk(self, account_key: str, signal_id: str, trade_id: str, symbol: str,
                               risk_money: float, max_total_risk: float, max_correlated_risk: float,
                               correlation_threshold: float, correlations: Dict[str, Optional[float]],
                               external_risk_by_symbol: Dict[str, float],
                               expires_at: datetime,
                               portfolio_cfg: Optional[dict] = None,
                               direction: int = 1,
                               directions_by_symbol: Optional[Dict[str, int]] = None) -> Optional[str]:
        now = datetime.now(timezone.utc)
        with self._lock:
            conn = self._connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("BEGIN")
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (account_key,))
                    cur.execute("SELECT id, signal_id, symbol, initial_risk_money, state FROM trades "
                                "WHERE state = ANY(%s)",
                                (list(_ACTIVE_RISK_STATES + _TERMINAL_RISK_STATES),))
                    all_trades = cast(List[Dict[str, Any]], cur.fetchall())
                    trades = [row for row in all_trades if row["state"] in _ACTIVE_RISK_STATES]
                    active_trade_ids = {str(row["id"]) for row in trades}
                    terminal_trade_ids = {str(row["id"]) for row in all_trades
                                          if row["state"] in _TERMINAL_RISK_STATES}
                    cur.execute("SELECT id, signal_id, trade_id, symbol, risk_money, expires_at "
                                "FROM risk_reservations WHERE account_key = %s AND state = 'RESERVED'",
                                (account_key,))
                    reservations = cast(List[Dict[str, Any]], cur.fetchall())
                    valid_reservations: List[Dict[str, Any]] = []
                    for row in reservations:
                        if str(row["trade_id"]) in active_trade_ids:
                            row["expires_at"] = now + timedelta(days=1)
                            cur.execute(
                                "UPDATE risk_reservations SET expires_at = %s, updated_at = %s WHERE id = %s",
                                (row["expires_at"], now, row["id"]),
                            )
                            valid_reservations.append(row)
                        elif str(row["trade_id"]) in terminal_trade_ids:
                            cur.execute("UPDATE risk_reservations SET state = 'RELEASED', updated_at = %s "
                                        "WHERE id = %s", (now, row["id"]))
                        elif row.get("expires_at") and row["expires_at"] > now:
                            valid_reservations.append(row)
                        else:
                            cur.execute("UPDATE risk_reservations SET state = 'EXPIRED', updated_at = %s "
                                        "WHERE id = %s", (now, row["id"]))
                    reason = _reservation_block_reason(
                        trades, valid_reservations, signal_id, symbol, risk_money, max_total_risk,
                        max_correlated_risk, correlation_threshold, correlations,
                        external_risk_by_symbol, portfolio_cfg, direction, directions_by_symbol)
                    if reason:
                        cur.execute("COMMIT")
                        return reason
                    cur.execute(
                        "INSERT INTO risk_reservations "
                        "(id, account_key, signal_id, trade_id, symbol, risk_money, state, expires_at, created_at, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, 'RESERVED', %s, %s, %s) "
                        "ON CONFLICT (account_key, signal_id) DO UPDATE SET "
                        "trade_id = EXCLUDED.trade_id, symbol = EXCLUDED.symbol, risk_money = EXCLUDED.risk_money, "
                        "state = 'RESERVED', expires_at = EXCLUDED.expires_at, updated_at = EXCLUDED.updated_at",
                        (uuid.uuid4(), account_key, signal_id, trade_id, symbol, risk_money, expires_at, now, now),
                    )
                    cur.execute("COMMIT")
                    return None
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise StorageError(f"atomic portfolio risk reservation failed: {exc}") from exc

    def release_portfolio_risk(self, account_key: str, signal_id: str) -> int:
        result = self._run(
            "UPDATE risk_reservations SET state = 'RELEASED', updated_at = %s "
            "WHERE account_key = %s AND signal_id = %s AND state = 'RESERVED'",
            [datetime.now(timezone.utc), account_key, signal_id],
        )
        return cast(int, result or 0)

    def recover_portfolio_risk_reservations(self, account_key: str, now: datetime,
                                            lease: timedelta) -> int:
        changed = 0
        with self._lock:
            conn = self._connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("BEGIN")
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (account_key,))
                    cur.execute("SELECT id, signal_id, trade_id, expires_at FROM risk_reservations "
                                "WHERE account_key = %s AND state = 'RESERVED'", (account_key,))
                    reservations = cast(List[Dict[str, Any]], cur.fetchall())
                    cur.execute("SELECT id, state FROM trades WHERE state = ANY(%s)",
                                (list(_ACTIVE_RISK_STATES + _TERMINAL_RISK_STATES),))
                    trades = {str(row["id"]): row["state"]
                              for row in cast(List[Dict[str, Any]], cur.fetchall())}
                    for row in reservations:
                        trade_state = trades.get(str(row["trade_id"]))
                        if trade_state in _ACTIVE_RISK_STATES:
                            cur.execute("UPDATE risk_reservations SET expires_at = %s, updated_at = %s WHERE id = %s",
                                        (now + lease, now, row["id"]))
                            changed += 1
                        elif trade_state in _TERMINAL_RISK_STATES:
                            cur.execute("UPDATE risk_reservations SET state = 'RELEASED', updated_at = %s WHERE id = %s",
                                        (now, row["id"]))
                            changed += 1
                        elif row.get("expires_at") and row["expires_at"] <= now:
                            cur.execute("UPDATE risk_reservations SET state = 'EXPIRED', updated_at = %s WHERE id = %s",
                                        (now, row["id"]))
                            changed += 1
                    cur.execute("COMMIT")
                    return changed
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise StorageError(f"risk reservation recovery failed: {exc}") from exc

    @staticmethod
    def _normalize(r: Dict[str, Any]) -> Dict[str, Any]:
        out = {}
        for k, v in dict(r).items():
            if v.__class__.__name__ == "UUID":
                v = str(v)
            out[k] = v
        return out


def make_repo(database_url: Optional[str]) -> Repository:
    if not database_url:
        log.warning("DATABASE_URL not set - using in-memory storage (nothing persists!)")
        return MemoryRepo()
    return PostgresRepo(database_url)
