"""Test-only stand-in for psycopg that executes SQL through the `psql` CLI.

Lets the test-suite verify PostgresRepo's generated SQL (casts, column names,
upserts, views) against a REAL PostgreSQL server on machines where psycopg is
not installable. On your PC the real psycopg driver is used instead.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import date, datetime
from typing import Any, List, Optional


class ShimJson:
    def __init__(self, obj):
        self.obj = obj


def _lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v != v:
            return "'NaN'"
        return repr(float(v)) if isinstance(v, float) else str(v)
    if isinstance(v, ShimJson):
        v = json.dumps(v.obj)
    elif isinstance(v, (datetime, date)):
        v = v.isoformat()
    s = str(v).replace("'", "''")
    return f"'{s}'"


def interpolate(sql: str, params: Optional[List[Any]]) -> str:
    if not params:
        return sql
    parts = sql.split("%s")
    if len(parts) - 1 != len(params):
        raise ValueError(f"placeholder mismatch {len(parts) - 1} vs {len(params)}")
    out = parts[0]
    for p, rest in zip(params, parts[1:]):
        out += _lit(p) + rest
    return out


class ShimCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows: List[dict] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        q = interpolate(sql, list(params) if params else None).strip().rstrip(";")
        if re.match(r"^\s*SELECT", q, re.I) and ";" not in q:
            out = self.conn.run(f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({q}) t", tuples=True)
            self._rows = json.loads(out.strip() or "[]")
            self.rowcount = len(self._rows)
        else:
            out = self.conn.run(q, tuples=False)
            m = re.findall(r"^(?:UPDATE|DELETE|INSERT \d+) (\d+)", out, re.M)
            self.rowcount = int(m[-1]) if m else 0
            self._rows = []

    def executemany(self, sql, seq):
        for p in seq:
            self.execute(sql, p)

    def fetchall(self):
        return self._rows


class ShimConnection:
    closed = False

    def __init__(self, dsn: dict):
        self.dsn = dsn

    def cursor(self):
        return ShimCursor(self)

    def close(self):
        self.closed = True

    def run(self, q: str, tuples: bool) -> str:
        cmd = ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-h", self.dsn["host"], "-p", str(self.dsn["port"]),
               "-U", self.dsn["user"], "-d", self.dsn["db"]]
        cmd += ["-A", "-t", "-q"] if tuples else []
        r = subprocess.run(cmd + ["-c", q], capture_output=True, text=True, env={**os.environ, "PGOPTIONS": ""})
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[:500])
        return r.stdout


def shim_repo(dsn: dict):
    from app.storage.repo import PostgresRepo
    return PostgresRepo("shim", connect=lambda: ShimConnection(dsn), json_wrapper=ShimJson)
