"""Quantara - command line.

    python -m app.main check                    verify Python, packages, database, MT5
    python -m app.main initdb                   create tables (alternative to pgAdmin)
    python -m app.main feasibility              Phase 0: $100 feasibility report
    python -m app.main collect-spreads --minutes 60 [--symbols XAUUSDm,XAUUSD247m]   (default: all configured symbols)
    python -m app.main export-history --start 2021-01-01
    python -m app.main build-bars --ticks exness_ticks.csv --out research/datasets/XAUUSD_M15.csv
    python -m app.main import-events config/economic_events.csv
    python -m app.main backtest   --strategy pullback_v1
    python -m app.main costs      --strategy pullback_v1
    python -m app.main exits      --strategy pullback_v1
    python -m app.main sessions   --strategy pullback_v1
    python -m app.main walkforward --strategy pullback_v1
    python -m app.main promote --strategy pullback_v1 --experiment <id> --to MICRO_LIVE --reason "..."
    python -m app.main run                      trading core + dashboard (environment from config.json)
    python -m app.main run --simulate           whole pipeline on simulated data (no MT5 needed)
    python -m app.main status
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import __version__
from app.config import ROOT, ConfigError, ConfigManager, load_secrets

log = logging.getLogger("main")
DATASETS = ROOT / "research" / "datasets"


def setup_logging(level: str = "INFO") -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    fh = logging.handlers.RotatingFileHandler(ROOT / "logs" / "quantara.log", maxBytes=20_000_000,
                                              backupCount=10, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.handlers = [fh, ch]
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # Windows: a browser closing a dashboard connection makes asyncio's Proactor loop log a harmless traceback
    # ("_ProactorBasePipeTransport._call_connection_lost"). It is not an error in the bot; keep it out of the log.
    # Likewise "socket.send() raised exception.", asyncio's only sign of a write to a connection whose peer has gone:
    # one line per dropped write, 300 a second on 23 Oct 2026. The dashboard now ends such connections itself
    # (api.WS_IDLE_SEC) and logs that once.
    noise = ("_call_connection_lost", "socket.send() raised exception")
    logging.getLogger("asyncio").addFilter(lambda r: not any(s in r.getMessage() for s in noise))


def make_mt5(secrets, cfg):
    from app.broker.mt5_adapter import MT5Adapter
    return MT5Adapter(secrets.mt5_login, secrets.mt5_password, secrets.mt5_server, secrets.mt5_terminal_path,
                      cfg.broker.connect_timeout_ms)


def get_repo(secrets, required: bool = False):
    from app.storage.repo import MemoryRepo, PostgresRepo
    if not secrets.database_url:
        if required:
            raise SystemExit("DATABASE_URL is not set in .env")
        log.warning("DATABASE_URL not set - results will not be stored in PostgreSQL")
        return None
    return PostgresRepo(secrets.database_url)


def resolve_symbol(broker, cfg) -> str:
    from app.market.symbols import resolve
    for c in cfg.broker.symbol_candidates:
        name = resolve(broker, c)
        if name:
            return name
    raise SystemExit(f"None of {cfg.broker.symbol_candidates} found. Broker symbols: {broker.find_symbols('*XAU*')}")


def _warn_uninstalled_strategies(cfgm) -> None:
    """Say out loud when a strategy class exists in code but has no entry in config/strategies.json.

    Keeping your own `config/` across package upgrades protects tuned settings, but it also means a strategy
    shipped in a later package never reaches the running bot - the code arrives, the config entry does not, and
    the strategy is invisible everywhere rather than obviously absent. Merge with tools/merge_strategies.py.
    """
    try:
        from app.strategies.registry import uninstalled_classes
        missing = uninstalled_classes(cfgm.strategies.strategies)
    except Exception:                                  # a diagnostic must never stop the bot starting
        log.debug("uninstalled-strategy check failed", exc_info=True)
        return
    if not missing:
        return
    log.warning("%d strategy class(es) exist in code but have NO entry in config/strategies.json and will not "
                "run: %s", len(missing), ", ".join(missing))
    log.warning("Add them with:  python tools/merge_strategies.py --mine config/strategies.json "
                "--reference <new package>/config/strategies.json --dry-run")


# ------------------------------------------------------------------ commands
def cmd_check(args, cfgm, secrets) -> int:
    ok = True
    print(f"Quantara {__version__}")
    print(f"Python {sys.version.split()[0]} ({'64' if sys.maxsize > 2**32 else '32'}-bit)")
    if sys.version_info < (3, 10):
        print("  !! Python 3.10+ required (3.12 64-bit recommended)")
        ok = False
    for mod in ("numpy", "pandas", "pydantic", "starlette", "uvicorn", "psycopg", "requests", "dotenv", "MetaTrader5"):
        try:
            __import__(mod)
            print(f"  ok  {mod}")
        except Exception as e:
            print(f"  !!  {mod}: {e}")
            ok = ok and mod not in ("psycopg", "MetaTrader5")
    print(f"Environment in config.json: {cfgm.app.environment}")
    if secrets.database_url:
        try:
            from app.storage.repo import PostgresRepo
            r = PostgresRepo(secrets.database_url)
            n = r.query("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema='public'")[0]["n"]
            print(f"  ok  PostgreSQL reachable, {n} tables")
            if n < 20:
                print("  !!  schema missing - run database/01_schema.sql in pgAdmin or `python -m app.main initdb`")
                ok = False
        except Exception as e:
            print(f"  !!  PostgreSQL: {e}")
            ok = False
    else:
        print("  !!  DATABASE_URL not set in .env")
        ok = False
    try:
        b = make_mt5(secrets, cfgm.app)
        b.connect()
        a = b.account_info()
        print(f"  ok  MT5 connected: account {a.login} on {a.server} ({a.trade_mode}), equity {a.equity} {a.currency}")
        print(f"      terminal: {b.terminal_info()}")
        sym = resolve_symbol(b, cfgm.app)
        print(f"  ok  symbol {sym}")
        b.shutdown()
    except Exception as e:
        print(f"  !!  MT5: {e}")
        ok = False
    print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED - see above")
    return 0 if ok else 1


def ensure_schema(repo) -> list:
    """The code and the database must agree. Missing tables are created from the idempotent schema file
    (CREATE ... IF NOT EXISTS - existing data is never touched). Returns the tables that were missing."""
    import re
    if not hasattr(repo, "_run"):          # in-memory repo (simulation / tests)
        return []
    sql = (ROOT / "database" / "01_schema.sql").read_text(encoding="utf-8")
    expected = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql)
    have = {r["table_name"] for r in repo.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    missing = [t for t in expected if t not in have]
    if not missing:
        return []
    print(f"Database is older than the code - creating missing tables: {', '.join(missing)}")
    try:
        repo._run(sql)
    except Exception as exc:
        raise SystemExit(f"Could not create the missing tables ({exc}). Run scripts\\2_check.bat or "
                         f"'python -m app.main initdb' with a database user that may CREATE tables.")
    return missing


def cmd_initdb(args, cfgm, secrets) -> int:
    repo = get_repo(secrets, required=True)
    sql = (ROOT / "database" / "01_schema.sql").read_text(encoding="utf-8")
    repo._run(sql)
    print("schema applied")
    return 0


def cmd_feasibility(args, cfgm, secrets) -> int:
    from app.research.feasibility import feasibility_report
    b = make_mt5(secrets, cfgm.app)
    b.connect()
    try:
        sym = resolve_symbol(b, cfgm.app)
        rep = feasibility_report(b, sym, cfgm.app, cfgm.strategy("pullback_v1"), args.equity)
    finally:
        b.shutdown()
    out = ROOT / "research" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    (out / f"{stamp}_feasibility_{sym}.md").write_text(rep["markdown"], encoding="utf-8")
    (out / f"{stamp}_feasibility_{sym}.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    print(rep["markdown"])
    repo = get_repo(secrets)
    if repo:
        repo.insert("feasibility_reports", {"id": rep["id"], "account_login": rep["account"]["login"], "symbol": sym,
                                            "equity": rep["account"]["equity_used_for_report"],
                                            "can_trade_safely": rep["verdict"]["can_trade_safely"],
                                            "can_test_meaningfully": rep["verdict"]["can_test_strategy_meaningfully"],
                                            "report": {k: v for k, v in rep.items() if k != "markdown"},
                                            "markdown": rep["markdown"]})
    print(f"\nSaved to {out}")
    return 0


def cmd_collect_spreads(args, cfgm, secrets) -> int:
    """Record live quotes (about 1/s per symbol) for EVERY configured symbol (or --symbols a,b,c)."""
    from app.core.clock import BrokerClock
    from app.market.sessions import SessionClassifier
    from app.market.ticks import SpreadCollector
    repo = get_repo(secrets, required=True)
    b = make_mt5(secrets, cfgm.app)
    b.connect()
    wanted = ([x.strip() for x in args.symbols.split(",") if x.strip()] if args.symbols
              else list(cfgm.app.broker.symbol_candidates))
    always_open = set(cfgm.app.broker.always_open_symbols)
    clock = BrokerClock(cfgm.app.broker.broker_utc_offset_hours)
    feeds = {}
    from app.market.symbols import contains, resolve
    for sym in wanted:
        try:
            sym = resolve(b, sym) or sym          # XAUUSDm on Standard is XAUUSD on Raw Spread / Zero
            if not b.select_symbol(sym):
                print(f"  skip {sym}: not available on this account")
                continue
            spec = b.symbol_spec(sym)
        except Exception as e:
            print(f"  skip {sym}: {e}")
            continue
        feeds[sym] = {"col": SpreadCollector(point=spec.point), "last": None,
                      "sess": SessionClassifier(cfgm.app.sessions, always_open=contains(always_open, sym))}
    if not feeds:
        b.shutdown()
        raise SystemExit("none of the configured symbols could be selected")
    end = time.time() + args.minutes * 60
    n = 0
    print(f"Collecting quotes for {len(feeds)} symbols: {', '.join(feeds)} for {args.minutes} minutes "
          f"(Ctrl+C to stop early)...")

    def flush(f, sym):
        rows = f["col"].drain()
        for r in rows:
            r["time"] = datetime.fromisoformat(r["time"])
        if rows:
            repo.insert_many("ticks", rows)
        return len(rows)

    try:
        while time.time() < end:
            for sym, f in feeds.items():
                t = b.tick(sym)
                if t and t.time_msc != f["last"]:
                    if f["last"] is not None:
                        clock.observe(t.time, True)
                    f["last"] = t.time_msc
                    utc = clock.to_utc(t.time)
                    f["col"].observe(t, str(f["sess"].classify(utc)), utc.isoformat(), {"symbol": sym})
                if len(f["col"].buffer) >= 60:
                    n += flush(f, sym)
                    print(f"  {n} samples total; {sym}: median spread {f['col'].median():.1f} pts, "
                          f"p90 {f['col'].overall.quantile(0.9):.1f}")
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    for sym, f in feeds.items():
        n += flush(f, sym)
    b.shutdown()
    print(f"done - {n} samples. See views v_spread_by_session and v_spread_by_symbol_hour in pgAdmin")
    return 0


def cmd_export_history(args, cfgm, secrets) -> int:
    from app.research.data import save_bars_csv
    from app.core.types import Bar
    b = make_mt5(secrets, cfgm.app)
    b.connect()
    sym = resolve_symbol(b, cfgm.app)
    spec = b.symbol_spec(sym)
    offset = cfgm.app.broker.broker_utc_offset_hours or 0.0
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)
    DATASETS.mkdir(parents=True, exist_ok=True)
    for tf in args.timeframes.split(","):
        bars, cur = [], start
        while cur < end:          # chunk by 180 days - MT5 limits very large requests
            nxt = min(end, cur + timedelta(days=180))
            chunk = b.rates_range(sym, tf, cur, nxt)
            bars.extend(chunk)
            cur = nxt
        # drop the last (possibly forming) bar, convert broker time -> UTC, dedupe
        seen, clean = set(), []
        for x in bars[:-1]:
            t = int(x.time - offset * 3600)
            if t not in seen:
                seen.add(t)
                clean.append(Bar(t, x.open, x.high, x.low, x.close, x.tick_volume, x.spread, x.real_volume))
        path = DATASETS / f"{sym}_{tf}.csv"
        save_bars_csv(path, sorted(clean, key=lambda z: z.time))
        print(f"{tf}: {len(clean)} bars -> {path}")
        if len(clean) < 1000:
            print(f"   !! few bars - open the {sym} {tf} chart in MT5 and scroll back to download more history")
    spec_d = {k: v for k, v in spec.__dict__.items() if k != "raw"}
    (DATASETS / f"{sym}_spec.json").write_text(json.dumps(spec_d, indent=2), encoding="utf-8")
    print(f"spec -> {DATASETS / f'{sym}_spec.json'}")
    b.shutdown()
    return 0


def cmd_build_bars(args, cfgm, secrets) -> int:
    from app.core.types import Bar
    from app.research.data import save_bars_csv, ticks_to_bars
    ff = ticks_to_bars(Path(args.ticks), args.timeframe, args.point)
    bars = [Bar(int(ff.t[i]), float(ff.o[i]), float(ff.h[i]), float(ff.l[i]), float(ff.c[i]), int(ff.tick_volume[i]),
                int(round(ff.spread[i])), 0) for i in range(len(ff))]
    save_bars_csv(Path(args.out), bars)
    print(f"{len(bars)} {args.timeframe} bars with measured mean spread -> {args.out}")
    return 0


def cmd_import_events(args, cfgm, secrets) -> int:
    from app.market.events import load_events_csv
    repo = get_repo(secrets, required=True)
    evs = load_events_csv(Path(args.file))
    for e in evs:
        repo.upsert("economic_events", {"event_time": e.time, "currency": e.currency, "impact": e.impact,
                                        "title": e.title, "source": Path(args.file).name},
                    ["event_time", "currency", "title"], ["impact", "source"])
    print(f"imported {len(evs)} events (restart the bot or it reloads on next start)")
    return 0


def _research_inputs(args, cfgm):
    from app.research.backtester import BTSettings
    from app.research.data import load_bars, load_dataset, load_spec, parse_date
    sym = args.symbol
    m15p = Path(args.m15 or DATASETS / f"{sym}_M15.csv")
    h1p = Path(args.h1) if args.h1 else (DATASETS / f"{sym}_H1.csv")
    if not m15p.exists():
        raise SystemExit(f"{m15p} not found. Run `python -m app.main export-history` first (or pass --m15).")
    m15, h1 = load_dataset(m15p, h1p if h1p.exists() else None, args.utc_offset)
    m1 = load_bars(Path(args.m1), "M1", args.utc_offset) if args.m1 else None
    spec = load_spec(Path(args.spec) if args.spec else DATASETS / f"{sym}_spec.json")
    s = BTSettings(spread_mult=args.spread_mult, slippage_points=args.slippage,
                   commission_per_lot=cfgm.app.cost.commission_per_lot_round_trip if args.commission is None
                   else args.commission, start_equity=args.equity, risk_fraction=args.risk,
                   grid_enabled=getattr(args, "research_grid", False),
                   start_time=parse_date(args.start), end_time=parse_date(args.end))
    print(f"data: {len(m15)} M15 bars, {len(h1)} H1 bars"
          f"{f', {len(m1)} M1 bars' if m1 is not None else ''}, "
          f"spec: {'broker export' if spec else 'default XAUUSD (100 oz)'}")
    return m15, h1, m1, spec, s


def cmd_research(args, cfgm, secrets) -> int:
    from app.research import runner
    m15, h1, m1, spec, s = _research_inputs(args, cfgm)
    repo = None if args.no_db else get_repo(secrets)
    cal = None
    if repo is not None:
        try:
            from app.market.events import EconomicEvent, EventCalendar
            rows = repo.select("economic_events")
            cal = EventCalendar(cfgm.app.events, [EconomicEvent(r["event_time"], r["currency"], r["impact"], r["title"])
                                                  for r in rows]) if rows else None
        except Exception:
            cal = None
    kind = args.command
    if kind == "backtest":
        out = runner.experiment_backtest(cfgm, args.strategy, m15, h1, s, spec, repo, cal, m1=m1)
    elif kind == "costs":
        out = runner.experiment_cost_stress(cfgm, args.strategy, m15, h1, s, spec, repo)
    elif kind == "exits":
        out = runner.experiment_exits(cfgm, args.strategy, m15, h1, s, spec, repo)
    elif kind == "sessions":
        out = runner.experiment_sessions(cfgm, args.strategy, m15, h1, s, spec, repo, cal)
    else:
        out = runner.experiment_walkforward(cfgm, args.strategy, m15, h1, s, spec, repo, args.train_months,
                                            args.test_months, workers=args.workers)
    print(json.dumps({k: v for k, v in out.items() if k not in ("grid", "table", "by_session", "hypotheses")},
                     indent=2, default=str))
    print(f"\nReport: {out['report']}")
    return 0


def cmd_promote(args, cfgm, secrets) -> int:
    target = args.to.upper()
    stats = None
    if target in ("MICRO_LIVE", "VALIDATED"):
        found = None
        for p in sorted((ROOT / "research" / "reports").glob("*_walk_forward_*.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            ps = d.get("promotion_stats") or {}
            if ps.get("experiment_id") == args.experiment:
                found = ps
        if found is None:
            raise SystemExit("walk-forward experiment not found in research/reports (promotion needs a walk-forward "
                             "evidence package)")
        if found.get("evidence_level") not in ("ROBUST", "VALIDATED") and not args.override:
            raise SystemExit(f"evidence level is {found.get('evidence_level')} - ROBUST is required. "
                             f"(--override exists but is recorded in the audit log)")
        stats = found
    rec = cfgm.set_strategy_status(args.strategy, target, stats, os.environ.get("USERNAME", "cli"),
                                   args.reason + (" [OVERRIDE]" if args.override else ""))
    repo = get_repo(secrets)
    if repo:
        repo.insert("config_versions", {k: rec.get(k) for k in ("config_version", "actor", "action", "path",
                                                               "old_value", "new_value", "reason")})
    print(f"{args.strategy} -> {target}. Restart the bot to apply.")
    return 0


def cmd_session(args, cfgm, secrets) -> int:
    """Pre-session readiness, or a post-session review. Reads only - it never changes a parameter."""
    from app.reports_session import readiness, review, review_message

    if args.when == "review":
        repo = get_repo(secrets, required=True)     # only the review reads history
        d = review(repo, datetime.now(timezone.utc), hours=args.hours)
        print(review_message(d))
        if d.get("by_session"):
            print("\n  by session:")
            for k, v in sorted(d["by_session"].items(), key=lambda kv: -kv[1]["mean_r"]):
                print(f"    {k.replace('_', ' ').title():<22}{v['mean_r']:+.3f}R over {v['n']} trades")
        if d.get("stop_moves_we_spaced_out"):
            print(f"\n  {d['stop_moves_we_spaced_out']} stop moves were spaced out by our own cooldown "
                  f"(not the broker; see execution.sl_modification_cooldown_sec)")
        return 0

    # Readiness reads the SAME state the dashboard renders, straight from the running bot, so the check can
    # never disagree with the screen. The bot holds that state in memory rather than in the database, so this
    # asks the local dashboard for it - unauthenticated, because /api/state is a read on 127.0.0.1 only.
    state = {}
    url = f"http://{cfgm.app.dashboard.host}:{cfgm.app.dashboard.port}/api/state"
    try:
        import json as _json
        import urllib.request
        with urllib.request.urlopen(url, timeout=5) as r:
            state = _json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        print(f"Could not reach the running bot at {url} ({exc}).")
        print("A readiness check asks the live system, so the bot and its dashboard need to be running.")
        return 1
    if not state:
        print("The bot answered but published no state yet. It may still be starting up.")
        return 1
    d = readiness(state)
    print(d["summary"])
    print()
    for c in d["checks"]:
        mark = " ok " if c["ok"] else ("!!! " if c["blocking"] else "  ~ ")
        print(f"  {mark}{c['name']:<24}{c['detail']}")
    return 0 if d["ready"] else 1


def cmd_status(args, cfgm, secrets) -> int:
    repo = get_repo(secrets, required=True)
    print("kill switch:", repo.get_flag("kill_switch"))
    print("risk tracker:", repo.get_flag("risk_tracker"))
    for t in repo.query("SELECT id, strategy_id, direction, state, open_volume, entry_price, sl, tp FROM v_open_trades"):
        print("open:", t)
    for s in repo.query("SELECT * FROM v_strategy_performance"):
        print("perf:", s)
    return 0


def cmd_research_lab(args, cfgm, secrets) -> int:
    """Run ONE research-lab cycle in the foreground (VectorBT sweep + Backtrader check)."""
    from app.broker.mt5_adapter import MT5Adapter
    from app.research.lab.worker import run_cycle
    repo = get_repo(secrets) if secrets.database_url else None
    adapter = MT5Adapter(None, None, None, secrets.mt5_terminal_path, cfgm.app.broker.connect_timeout_ms)
    adapter.connect()
    try:
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
        res = run_cycle(cfgm, repo, adapter, syms,
                        all_strategies=True if getattr(args, "all_strategies", False) else None)
    finally:
        adapter.shutdown()
    for sym, e in res["symbols"].items():
        b = (e.get("best") or {})
        print(f"{sym:<11} {e.get('verdict'):<26} signals={e.get('signals')}  best SLx{b.get('sl_atr')} RR{b.get('rr')}  "
              f"test={((b.get('test') or {}).get('expectancy_r'))}R  backtrader={((e.get('backtrader') or {}).get('expectancy_r'))}R")
    return 0


def cmd_why_no_trades(args, cfgm, secrets) -> int:
    """Funnel report: for the last N hours, where did every candidate signal stop? (read-only)"""
    repo = get_repo(secrets, required=True)
    hrs = str(args.hours)
    since = "created_at > now() - (%s || ' hours')::interval"
    print(f"=== Why no trades?  last {hrs} h ===")
    n_tr = repo.query(f"SELECT count(*) AS n FROM trades WHERE {since}", [hrs])[0]["n"]
    print(f"trades created: {n_tr}")
    print("kill switch:", repo.get_flag("kill_switch"))
    rows = repo.query(
        "SELECT symbol, strategy_id, stage, outcome, coalesce(gate,'-') AS gate, "
        "regexp_replace(coalesce(reason,'-'), '[0-9.]+', '#', 'g') AS reason, count(*) AS n "
        f"FROM candidate_evaluations WHERE {since} GROUP BY 1,2,3,4,5,6 ORDER BY n DESC", [hrs])
    if not rows:
        print("no candidate evaluations at all - the bot was not evaluating (stopped, halted, or market closed "
              "before the first bar). Check Logs & Audit and the health banner.")
    else:
        print("\n-- where candidates stopped (top 25) --")
        for r in rows[:25]:
            print(f"{r['n']:>6}  {r['symbol']:<11} {r['strategy_id']:<14} {r['stage']:<10} {r['outcome']:<10} "
                  f"{r['gate']:<14} {r['reason']}")
        print("\n-- per symbol: top blocker --")
        seen = {}
        for r in rows:
            seen.setdefault(r["symbol"], []).append(r)
        for sym, items in seen.items():
            tot = sum(i["n"] for i in items)
            top = next((i for i in items if i["outcome"] not in ("PASSED", "SIGNAL")), items[0])
            print(f"{sym:<11} {tot:>6} evaluations; most common: {top['outcome']} {top['gate']} {top['reason']} x{top['n']}")
    rej = repo.query(f"SELECT coalesce(gate,'-') AS gate, regexp_replace(coalesce(reason,'-'), '[0-9.]+', '#', 'g') AS reason, "
                     f"count(*) AS n FROM rejections WHERE {since} GROUP BY 1,2 ORDER BY n DESC LIMIT 15", [hrs])
    if rej:
        print("\n-- signals rejected after being generated --")
        for r in rej:
            print(f"{r['n']:>6}  {r['gate']:<14} {r['reason']}")
    health = repo.query(f"SELECT created_at, component, status, message FROM system_health WHERE {since} "
                        "AND status NOT IN ('OK','HEALTHY','INFO') ORDER BY created_at DESC LIMIT 12", [hrs])
    if health:
        print("\n-- recent health warnings --")
        for h in health:
            print(f"{h['created_at']}  {h['component']:<14} {h['status']:<9} {h['message']}")
    print("\nNote: MARKET_CLOSED / SKIPPED rows over the weekend (Fri 21:00 - Sun 22:00 UTC) are normal.")
    return 0


def cmd_run(args, cfgm, secrets) -> int:
    from app.core.bridge import Bridge
    from app.core.engine import TradingCore
    from app.core.multi import MultiAssetRunner
    from app.core.symbol_control import SymbolTradingControl
    from app.notifications.base import make_notifier
    from app.storage.repo import MemoryRepo, PostgresRepo
    from app.storage.writer import DBLogHandler, TelemetryWriter

    bridge = Bridge()
    cfg = cfgm.app
    lab = None
    if secrets.database_url and not args.simulate:
        ensure_schema(PostgresRepo(secrets.database_url, application_name="ata_schema"))
    notifier = make_notifier(cfg.alerts.channel, secrets, cfg.alerts.enabled,
                             cfg.alerts.min_seconds_between_same_alert)
    sim = None
    if args.simulate:
        from app.simulation import make_sim_core, sim_config_dir
        print("SIMULATION MODE - synthetic market, paper broker, in-memory database. Nothing here says anything "
              "about real profitability.")
        cfg_dir = sim_config_dir("DEMO", {"alerts.enabled": False})
        core, sim, broker, _ = make_sim_core("DEMO", n_bars=60000, warmup_bars=7000, balance=args.sim_balance,
                                             cfg_dir=cfg_dir, bridge=bridge)
        repo_for_dash = core.repo
        telemetry = None
    else:
        if not secrets.database_url:
            raise SystemExit("DATABASE_URL missing in .env - the bot will not trade without durable storage")
        repo = PostgresRepo(secrets.database_url)
        if not repo.healthy():
            raise SystemExit("PostgreSQL not reachable - check DATABASE_URL / that PostgreSQL service is running")
        telemetry = TelemetryWriter(lambda: PostgresRepo(secrets.database_url, application_name="ata_telemetry"))
        telemetry.start()
        logging.getLogger().addHandler(DBLogHandler(telemetry))
        broker = make_mt5(secrets, cfg)
        symbol_control = SymbolTradingControl(repo)
        symbol_control.load()
        if cfg.research_lab.enabled:
            try:       # separate low-priority process: research can never slow or break the trading loop
                from app.research.lab.worker import LabProcess
                lab = LabProcess()
                lab.start()
            except Exception as exc:
                log.warning("research lab not started: %s", exc)
        core = MultiAssetRunner(
            cfgm, cfg.broker.symbol_candidates,
            lambda symbol: TradingCore(cfgm, broker, repo, secrets, bridge=bridge, notifier=notifier,
                                       telemetry=telemetry, journal_path=ROOT / "logs" / "critical_journal.jsonl",
                                       symbol_override=symbol, shutdown_broker=False,
                                       symbol_control=symbol_control),
            bridge, symbol_control=symbol_control)
        repo_for_dash = PostgresRepo(secrets.database_url, application_name="ata_dashboard")
        if cfg.calendar_sync.enabled:
            from app.market.calendar_sync import CalendarSyncWorker
            CalendarSyncWorker(PostgresRepo(secrets.database_url, application_name="ata_calendar"),
                               lambda: cfgm.app).start()

    def core_thread():
        try:
            core.start()
        except Exception as e:
            log.critical("core failed to start: %s", e)
            return
        if sim is not None:
            while not core.stop_event.is_set():
                if not sim.step():
                    log.info("simulation data exhausted")
                    break
                core.step()
                core.stop_event.wait(args.sim_delay)
            core.shutdown()
        else:
            core.run()

    th = threading.Thread(target=core_thread, name="trading-core", daemon=True)
    th.start()
    if args.no_dashboard:
        try:
            while th.is_alive():
                th.join(1.0)
        except KeyboardInterrupt:
            core.stop_event.set()
            th.join(15)
    else:
        import uvicorn
        from app.dashboard.api import create_app
        app = create_app(bridge, repo_for_dash, core.cfgm, secrets.dashboard_token)
        print(f"\n  Dashboard: http://{cfg.dashboard.host}:{cfg.dashboard.port}\n")
        try:
            # A stop must reach the trading core's shutdown. Without a limit uvicorn waits for every dashboard
            # connection to close, and one whose peer has gone never reports closed: on 23 Oct 2026 Ctrl+C sat
            # there until a second Ctrl+C forced it.
            uvicorn.run(app, host=cfg.dashboard.host, port=cfg.dashboard.port, log_level="warning",
                        timeout_graceful_shutdown=10)
        finally:
            core.stop_event.set()
            th.join(15)
    if lab is not None:
        lab.shutdown()
    if telemetry is not None:
        telemetry.stop()
        telemetry.join(10)
    notifier.stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.main", description="Quantara")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("initdb")
    sp = sub.add_parser("feasibility")
    sp.add_argument("--equity", type=float, default=None, help="evaluate a different equity (e.g. 100)")
    sp = sub.add_parser("research-lab")
    sp.add_argument("--symbols", default=None)
    sp.add_argument("--all-strategies", action="store_true",
                    help="also research every configured strategy at its live parameters, not just "
                         "micro_scalper. Slower (full M15/H1/H4 replay per strategy per symbol), and "
                         "normally left to the weekend idle window - this runs it on demand.")
    sp = sub.add_parser("why-no-trades")
    sp.add_argument("--hours", type=int, default=48)
    sp = sub.add_parser("collect-spreads")
    sp.add_argument("--minutes", type=int, default=60)
    sp.add_argument("--symbols", default=None, help="comma list; default = every symbol in config broker.symbol_candidates")
    sp = sub.add_parser("export-history")
    sp.add_argument("--start", default="2021-01-01")
    sp.add_argument("--end", default=None)
    sp.add_argument("--timeframes", default="M15,H1")
    sp = sub.add_parser("build-bars")
    sp.add_argument("--ticks", required=True)
    sp.add_argument("--timeframe", default="M15")
    sp.add_argument("--point", type=float, default=0.01)
    sp.add_argument("--out", required=True)
    sp = sub.add_parser("import-events")
    sp.add_argument("file")
    for name in ("backtest", "costs", "exits", "sessions", "walkforward"):
        sp = sub.add_parser(name)
        sp.add_argument("--strategy", default="pullback_v1")
        sp.add_argument("--symbol", default="XAUUSD", help="dataset file prefix")
        sp.add_argument("--m15")
        sp.add_argument("--h1")
        sp.add_argument("--m1", help="optional M1 history required for micro_scalper research")
        sp.add_argument("--spec")
        sp.add_argument("--start")
        sp.add_argument("--end")
        sp.add_argument("--utc-offset", type=float, default=0.0)
        sp.add_argument("--spread-mult", type=float, default=1.0)
        sp.add_argument("--slippage", type=float, default=5.0, help="points")
        sp.add_argument("--commission", type=float, default=None, help="per lot round trip")
        sp.add_argument("--equity", type=float, default=100.0)
        sp.add_argument("--risk", type=float, default=0.01, help="risk fraction for the money simulation")
        if name == "backtest":
            sp.add_argument("--research-grid", action="store_true",
                            help="research-only M1 candle-gated grid (1 ATR spacing, 3 layers, 1%% basket risk)")
        sp.add_argument("--no-db", action="store_true")
        sp.add_argument("--train-months", type=int, default=12)
        sp.add_argument("--test-months", type=int, default=3)
        sp.add_argument("--workers", type=int, default=max(1, min(4, (os.cpu_count() or 2) - 1)))
    sp = sub.add_parser("session", help="pre-session readiness check, or a post-session review")
    sp.add_argument("when", choices=["check", "review"], help="check = before trading, review = after")
    sp.add_argument("--hours", type=float, default=8.0, help="how far back a review looks")

    sp = sub.add_parser("promote")
    sp.add_argument("--strategy", required=True)
    sp.add_argument("--experiment", default="")
    sp.add_argument("--to", required=True, choices=["RESEARCH_ONLY", "SHADOW", "DEMO", "MICRO_LIVE", "VALIDATED",
                                                   "QUARANTINE", "DISABLED"])
    sp.add_argument("--reason", required=True)
    sp.add_argument("--override", action="store_true")
    sp = sub.add_parser("run")
    sp.add_argument("--simulate", action="store_true")
    sp.add_argument("--sim-delay", type=float, default=0.05, help="seconds between simulated ticks")
    sp.add_argument("--sim-balance", type=float, default=10000.0)
    sp.add_argument("--no-dashboard", action="store_true")
    sub.add_parser("status")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    try:
        cfgm = ConfigManager()
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}")
        return 2
    _warn_uninstalled_strategies(cfgm)
    secrets = load_secrets()
    handlers = {"check": cmd_check, "initdb": cmd_initdb, "feasibility": cmd_feasibility,
                "collect-spreads": cmd_collect_spreads, "why-no-trades": cmd_why_no_trades, "research-lab": cmd_research_lab, "export-history": cmd_export_history,
                "build-bars": cmd_build_bars, "import-events": cmd_import_events, "backtest": cmd_research,
                "costs": cmd_research, "exits": cmd_research, "sessions": cmd_research, "walkforward": cmd_research,
                "promote": cmd_promote, "run": cmd_run, "status": cmd_status, "session": cmd_session}
    return handlers[args.command](args, cfgm, secrets)


if __name__ == "__main__":
    sys.exit(main())
