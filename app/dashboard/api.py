"""Localhost Web Control Center (sections 51-63).

Starlette (the ASGI core FastAPI is built on) + WebSocket + plain HTML/CSS/JS.
* Binds to 127.0.0.1 only.
* Read endpoints are open on localhost; every control action requires the
  X-Dashboard-Token header (protects against other web pages posting to localhost).
* The dashboard never trades: it submits commands to the core, which validates them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets as pysecrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.config import ConfigManager
from app.core.bridge import Bridge
from app.execution.book import as_epoch
from app.storage.repo import Repository, json_default

log = logging.getLogger("dashboard")
STATIC = Path(__file__).parent / "static"

RECON_EVENTS = ["RECONCILED_FILL", "CONFIRMED_NOT_FILLED", "BROKER_STATE_DIFFERS", "UNEXPECTED_POSITION_ADOPTED",
                "POSITION_CLOSED", "NOT_SENT", "AMBIGUOUS_ORDER_RESULT", "FILL_NOT_VISIBLE",
                "ALREADY_CLOSED_UNCONFIRMED", "AMBIGUOUS_CLOSE", "CLOSE_DEAL_NOT_VISIBLE"]


class JSON(Response):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return json.dumps(content, default=json_default, separators=(",", ":")).encode("utf-8")


def _safe(fn, default):
    try:
        return fn()
    except Exception as e:  # dashboard must keep working if the database hiccups
        log.warning("dashboard query failed: %s", e)
        return default


def summarize_candidate_evaluations(rows: List[dict], since: float) -> dict:
    groups: Dict[tuple, Dict[str, Any]] = {}
    total = 0
    for row in rows:
        if as_epoch(row.get("created_at")) < since:
            continue
        total += 1
        key = (row.get("symbol") or "?", row.get("strategy_id") or "?",
               row.get("stage") or "?", row.get("gate") or "?", row.get("reason") or "?")
        group = groups.setdefault(key, {
            "symbol": key[0], "strategy_id": key[1], "stage": key[2], "gate": key[3],
            "reason": key[4], "count": 0, "outcomes": {},
        })
        group["count"] += 1
        outcome = row.get("outcome") or "?"
        group["outcomes"][outcome] = group["outcomes"].get(outcome, 0) + 1
    ordered = sorted(groups.values(), key=lambda group: (-group["count"], group["symbol"],
                                                         group["strategy_id"], group["stage"]))
    return {"evaluations": total, "groups": ordered}


def create_app(bridge: Bridge, repo: Repository, cfgm: ConfigManager, token: Optional[str]) -> Starlette:
    if not token:
        token = pysecrets.token_urlsafe(16)
        msg = (f"DASHBOARD_TOKEN not set in .env - temporary control token for this session: {token}")
        log.warning(msg)
        print("\n  " + msg + "\n")
    clients: set = set()

    async def broadcaster():
        while True:
            msgs = []
            while True:
                try:
                    msgs.append(bridge.events.get_nowait())
                except Exception:
                    break
                if len(msgs) > 200:
                    break
            if msgs and clients:
                data = json.dumps(msgs, default=json_default)
                dead = []
                for ws in list(clients):
                    try:
                        await ws.send_text(data)
                    except Exception:
                        dead.append(ws)
                for d in dead:
                    clients.discard(d)
            await asyncio.sleep(0.25)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(broadcaster())
        yield
        task.cancel()

    # ------------------------------------------------------------------ pages
    def page(name: str):
        async def handler(request: Request):
            return FileResponse(STATIC / f"{name}.html")
        return handler

    async def service_worker(request: Request):
        """Served from the root, not from /static/.

        A service worker may only control pages at or below its own URL. Served from
        /static/sw.js it would control nothing but /static/, the browser would not
        consider the dashboard installable, and no "Install app" option would appear.
        no-cache keeps a stale worker from pinning an old dashboard in place.
        """
        return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ reads
    async def api_state(request: Request):
        state = dict(bridge.get_state())
        state["display_timezone"] = cfgm.app.display.timezone
        return JSON(state)

    async def api_recent(request: Request):
        return JSON(bridge.recent_events(int(request.query_params.get("n", 100))))

    async def api_equity(request: Request):
        return await run_in_threadpool(_api_equity_sync, request)

    def _api_equity_sync(request: Request):
        hours = float(request.query_params.get("hours", 168))
        since = time.time() - hours * 3600
        rows = _safe(lambda: repo.select("account_snapshots", order_by="id", desc=True, limit=20000), [])
        pts = [{"t": as_epoch(r["created_at"]), "equity": r["equity"], "balance": r["balance"]}
               for r in rows if as_epoch(r["created_at"]) >= since]
        pts.sort(key=lambda p: p["t"])
        if len(pts) > 1500:
            step = len(pts) // 1500 + 1
            pts = pts[::step] + [pts[-1]]
        return JSON(pts)

    async def api_analysis(request: Request):
        return await run_in_threadpool(_api_analysis_sync, request)

    def _api_analysis_sync(request: Request):
        from app.dashboard import analysis
        kind = request.path_params["kind"]
        hours = float(request.query_params.get("hours", 168 if kind == "losses" else 48))
        since = time.time() - hours * 3600
        limit = min(int(request.query_params.get("limit", 100)), 500)
        if kind == "losses":
            trades = _safe(lambda: repo.select("trades", {"state": ["CLOSED", "BROKER_CLOSED"]}, order_by="closed_at",
                                               desc=True, limit=1500), [])
            trades = [t for t in trades if as_epoch(t.get("closed_at")) >= since]
            ids = list({t["signal_id"] for t in trades if t.get("signal_id")})
            sigs = {s["id"]: s for s in (_safe(lambda: repo.select("signals", {"id": ids}), []) if ids else [])}
            rows = analysis.build_loss_rows(trades, sigs)
            return JSON({"summary": analysis.loss_summary(rows), "rows": rows[:limit]})
        if kind == "blocked":
            rej = _safe(lambda: repo.select("rejections", order_by="id", desc=True, limit=2000), [])
            rej = [r for r in rej if as_epoch(r.get("created_at")) >= since]
            ids = list({r["signal_id"] for r in rej if r.get("signal_id")})
            sigs = {s["id"]: s for s in (_safe(lambda: repo.select("signals", {"id": ids}), []) if ids else [])}
            rows = analysis.build_blocked_rows(rej, sigs)
            only = request.query_params.get("only")
            if only == "positive":
                rows = [r for r in rows if r["calc_positive"]]
            return JSON({"summary": analysis.blocked_summary(rows), "rows": rows[:limit]})
        if kind == "sessions":
            trades = _safe(lambda: repo.select("trades", {"state": ["CLOSED", "BROKER_CLOSED"]},
                                               order_by="closed_at", desc=True, limit=5000), [])
            trades = [t for t in trades if as_epoch(t.get("closed_at")) >= since]
            # average fill slippage per trade, so a session can be judged on its costs as well as its result
            fills = _safe(lambda: repo.select("execution_events", {"event_type": "FILL"},
                                              order_by="id", desc=True, limit=20000), [])
            slip = {}
            for e in fills:
                v, tid = e.get("slippage_points"), e.get("trade_id")
                if v is not None and tid:
                    slip.setdefault(str(tid), []).append(float(v))
            slip = {k: sum(v) / len(v) for k, v in slip.items()}
            sym = request.query_params.get("symbol") or None
            # ?exclude=micro_scalper,other — a session table is a weighted average of whatever was
            # trading, so excluding a dominant strategy is what makes the HOURS visible rather than
            # that strategy's habits.
            excl = [x.strip() for x in (request.query_params.get("exclude") or "").split(",") if x.strip()]
            disabled = [s.id for s in cfgm.strategies.strategies if not getattr(s, "enabled", True)]
            return JSON({**analysis.session_breakdown(trades, slip, sym, excl),
                         "hours": analysis.hour_breakdown(trades, sym, excl),
                         "symbol": sym, "excluded": excl,
                         # offered to the page so it can suggest excluding what no longer trades
                         "disabled_strategies": disabled})
        if kind == "portfolio":
            # Judge the book, not the strategies one at a time. Same code as tools/portfolio_report.py,
            # so the dashboard and the CLI can never disagree about what the portfolio did.
            from tools.portfolio_report import build as build_portfolio
            trades = _safe(lambda: repo.select("trades", {"state": ["CLOSED", "BROKER_CLOSED"]},
                                               order_by="closed_at", desc=True, limit=5000), [])
            trades = [t for t in trades if as_epoch(t.get("closed_at")) >= since]
            bucket = "W" if request.query_params.get("bucket") == "W" else "D"
            return JSON({**build_portfolio(trades, bucket), "hours": hours})
        if kind == "scoreboard":
            trades = _safe(lambda: repo.select("trades", {"state": ["CLOSED", "BROKER_CLOSED"]},
                                               order_by="closed_at", desc=True, limit=3000), [])
            trades = [t for t in trades if as_epoch(t.get("closed_at")) >= since]
            cfs = _safe(lambda: repo.select("counterfactuals", order_by="id", desc=True, limit=5000), [])
            cfs = [c for c in cfs if as_epoch(c.get("created_at")) >= since]
            statuses = {c.id: c.status for c in cfgm.strategies.strategies}
            return JSON({"rows": analysis.build_scoreboard(trades, cfs, statuses), "hours": hours})
        return JSON({"error": "unknown analysis"}, status_code=404)

    def _research_lab_sync():
        from app.research.lab.gate import evaluate
        latest = _safe(lambda: repo.get_flag("research_lab_latest"), None)
        gates = {}
        if isinstance(latest, dict):
            for sym, entry in (latest.get("symbols") or {}).items():
                gates[sym] = _safe(lambda: evaluate(entry, latest.get("created_at"), cfgm.app.research_lab), None)
        # Per-strategy research: {strategy_id: {symbol: entry}} evaluated through the same gate, each
        # against ITS OWN thresholds. Only the strategies in gate_strategies can actually block; the
        # rest are shown so the evidence is visible before anyone decides to let it block.
        per_strategy = {}
        if isinstance(latest, dict):
            for sid, per_sym in (latest.get("strategies") or {}).items():
                rows = {}
                for sym, entry in (per_sym or {}).items():
                    rows[sym] = _safe(lambda e=entry, s=sid: evaluate(
                        e, latest.get("created_at"), cfgm.app.research_lab, strategy_id=s), None)
                per_strategy[sid] = rows
        return JSON({"latest": latest, "live_gate": gates,
                     "gate_strategies": list(cfgm.app.research_lab.gate_strategies),
                     "per_strategy": per_strategy,
                     "verdict_counts": (latest or {}).get("strategy_verdict_counts") or {},
                     "history": _safe(lambda: repo.select("research_lab_runs", order_by="id", desc=True, limit=60), [])})

    async def api_research_lab(request: Request):
        return await run_in_threadpool(_research_lab_sync)

    async def api_trades_today(request: Request):
        return await run_in_threadpool(_api_trades_today_sync, request)

    def _api_trades_today_sync(request: Request):
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        rows = _safe(lambda: repo.select("trades", {"state": ["CLOSED", "BROKER_CLOSED"]}, order_by="closed_at",
                                         desc=True, limit=300), [])
        return JSON([r for r in rows if as_epoch(r.get("closed_at")) >= start])

    async def api_logs(request: Request):
        return await run_in_threadpool(_api_logs_sync, request)

    def _api_logs_sync(request: Request):
        tab = request.path_params["tab"]
        limit = min(int(request.query_params.get("limit", 200)), 2000)
        if tab == "execution":
            rows = _safe(lambda: repo.select("execution_events", order_by="id", desc=True, limit=limit), [])
        elif tab == "errors":
            rows = _safe(lambda: repo.select("app_logs", order_by="id", desc=True, limit=limit), [])
        elif tab == "signals":
            rows = _safe(lambda: repo.select("signals", order_by="created_at", desc=True, limit=limit), [])
        elif tab == "candidate_evaluations":
            try:
                rows = repo.select("candidate_evaluations", order_by="created_at", desc=True, limit=limit)
            except Exception:
                log.exception("candidate evaluations query failed")
                return JSON({"error": "candidate evaluation history unavailable; apply database/01_schema.sql"},
                            status_code=503)
        elif tab == "rejected":
            rows = _safe(lambda: repo.select("rejections", order_by="created_at", desc=True, limit=limit), [])
            cf = _safe(lambda: repo.select("counterfactuals", {"rejection_id": [r["id"] for r in rows]}) if rows else [], [])
            by = {str(c.get("rejection_id")): c for c in cf}
            for r in rows:
                c = by.get(str(r["id"]))
                r["hypothetical_r"] = c.get("hypothetical_r") if c else None
                r["cf_status"] = c.get("status") if c else None
        elif tab == "counterfactuals":
            rows = _safe(lambda: repo.select("counterfactuals", order_by="created_at", desc=True, limit=limit), [])
        elif tab == "risk":
            rows = _safe(lambda: repo.select("risk_states", order_by="id", desc=True, limit=limit), [])
        elif tab == "reconciliation":
            rows = _safe(lambda: repo.select("trade_events", {"event_type": RECON_EVENTS}, order_by="id", desc=True,
                                             limit=limit), [])
        elif tab == "health":
            rows = _safe(lambda: repo.select("system_health", order_by="id", desc=True, limit=limit), [])
        elif tab == "research":
            rows = _safe(lambda: repo.select("experiments", order_by="created_at", desc=True, limit=limit), [])
            for r in rows:
                m = r.get("metrics") or {}
                r["expectancy_r"] = m.get("expectancy_r") or (m.get("oos") or {}).get("expectancy_r")
                r["n_trades"] = m.get("n_trades") or (m.get("oos") or {}).get("n_trades")
                r.pop("metrics", None)
                r.pop("settings", None)
        elif tab == "trades":
            rows = _safe(lambda: repo.select("trades", order_by="created_at", desc=True, limit=limit), [])
        elif tab == "config":
            rows = _safe(lambda: repo.select("config_versions", order_by="id", desc=True, limit=limit), [])
            for r in rows:
                r.pop("full_config", None)
        else:
            return JSON({"error": "unknown tab"}, status_code=404)
        return JSON(rows)

    async def api_cf_summary(request: Request):
        return await run_in_threadpool(_api_cf_summary_sync, request)

    def _api_cf_summary_sync(request: Request):
        days = float(request.query_params.get("days", 1))
        since = time.time() - days * 86400
        rows = _safe(lambda: repo.select("counterfactuals", order_by="created_at", desc=True, limit=5000), [])
        rows = [r for r in rows if as_epoch(r.get("created_at")) >= since]
        by: Dict[str, Dict] = {}
        for r in rows:
            g = by.setdefault(r.get("gate") or "?", {"rejected": 0, "won": 0, "lost": 0, "open": 0, "sum_r": 0.0})
            g["rejected"] += 1
            if r.get("status") == "OPEN":
                g["open"] += 1
            elif (r.get("hypothetical_r") or 0) > 0:
                g["won"] += 1
                g["sum_r"] += float(r["hypothetical_r"])
            else:
                g["lost"] += 1
                g["sum_r"] += float(r.get("hypothetical_r") or 0)
        resolved = {k: v for k, v in by.items() if v["won"] + v["lost"] > 0}
        expensive = max(resolved, key=lambda k: resolved[k]["sum_r"], default=None)
        valuable = min(resolved, key=lambda k: resolved[k]["sum_r"], default=None)
        return JSON({"days": days, "rejected": len(rows),
                     "would_have_won": sum(v["won"] for v in by.values()),
                     "would_have_lost": sum(v["lost"] for v in by.values()),
                     "still_open": sum(v["open"] for v in by.values()),
                     "counterfactual_r": round(sum(v["sum_r"] for v in by.values()), 3),
                     "most_expensive_gate": expensive if expensive and resolved[expensive]["sum_r"] > 0 else None,
                     "most_valuable_gate": valuable if valuable and resolved[valuable]["sum_r"] < 0 else None,
                     "by_gate": by})

    async def api_candidate_summary(request: Request):
        try:
            days = max(0.1, min(float(request.query_params.get("days", 7)), 365.0))
        except ValueError:
            return JSON({"error": "days must be a number between 0.1 and 365"}, status_code=400)
        since = time.time() - days * 86400
        try:
            rows = repo.select("candidate_evaluations", order_by="created_at", desc=True, limit=50000)
        except Exception:
            log.exception("candidate summary query failed")
            return JSON({"error": "candidate analytics unavailable"}, status_code=503)
        summary = summarize_candidate_evaluations(rows, since)
        return JSON({"days": days, **summary})

    async def api_config(request: Request):
        snap = cfgm.snapshot()
        return JSON(snap)

    async def api_integrity(request: Request):
        return await run_in_threadpool(_integrity_sync)

    def _integrity_sync():
        """Settings that decide how strictly the bot judges itself, and which of them are not ready
        for real money.

        Every item here was invisible until something went looking for it: a research gate that failed
        open while the manual said it failed closed, a straddle ceiling equal to the number of symbols,
        a shared evidence bar across eight strategies that do not resemble each other, and a commission
        of zero that is right on one Exness account type and wrong on another. A setting that silently
        does nothing is worse than no setting, because it reads as a control. They belong on screen."""
        from app.research.lab.gate import Thresholds
        lab, straddle, cost, risk = (cfgm.app.research_lab, cfgm.app.news_straddle,
                                     cfgm.app.cost, cfgm.app.risk)
        all_ids = [s.id for s in cfgm.strategies.strategies]
        gated = [s for s in all_ids if s in set(lab.gate_strategies)]

        checks = []
        try:
            from tools.live_readiness import _config_checks
            checks = _config_checks(cfgm)
        except Exception as exc:                                       # noqa: BLE001
            checks = [{"name": "Configuration checks", "pass": False,
                       "got": f"unavailable: {type(exc).__name__}", "needs": "loadable", "why": str(exc)}]

        return JSON({
            "research": {
                "gate_live": lab.gate_live,
                "require_evidence": lab.gate_require_evidence,
                "researched": len(all_ids) if lab.research_all_strategies else 1,
                "gated": gated,
                "record_only": [s for s in all_ids if s not in set(lab.gate_strategies)],
                "thresholds": {sid: Thresholds(lab, sid).to_dict() for sid in all_ids},
                "all_on_global_bar": all(Thresholds(lab, sid).source == "global default" for sid in all_ids),
            },
            "portfolio_risk": {
                "mode": risk.portfolio_risk_mode,
                "max_total_open_risk_fraction": risk.max_total_open_risk_fraction,
                "max_gross_open_risk_fraction": risk.max_gross_open_risk_fraction,
                "stress_correlation": risk.stress_correlation,
                "stressed_risk_tolerance": risk.stressed_risk_tolerance,
                "diversification_floor": risk.diversification_floor,
                "missing_correlation_value": risk.missing_correlation_value,
                "max_concurrent_positions": risk.max_concurrent_positions,
            },
            "news_straddle": {
                "enabled": straddle.enabled,
                "symbols": len(straddle.symbols),
                "max_armed_symbols": straddle.max_armed_symbols,
                "ceiling_binds": straddle.ceiling_binds(),
            },
            "cost": {
                "commission_per_lot_round_trip": cost.commission_per_lot_round_trip,
                "per_symbol_overrides": len(cost.symbol_commission_per_lot_round_trip),
            },
            "adaptive": {
                "enabled": cfgm.app.adaptive.enabled,
                "max_spread_cap_age_minutes": cfgm.app.adaptive.max_spread_cap_age_minutes,
            },
            "real_money_checks": checks,
        })

    # ------------------------------------------------------------------ controls
    async def api_control(request: Request):
        if request.headers.get("x-dashboard-token") != token:
            return JSON({"ok": False, "error": "invalid or missing dashboard token"}, status_code=403)
        action = request.path_params["action"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        body["actor"] = "dashboard"
        allowed = {"halt", "resume", "force_reset_resume", "enable_strategy", "disable_strategy",
                   "set_config", "close_all", "set_symbol_trading",
                   "reconcile_now", "reload_events"}
        if action not in allowed:
            return JSON({"ok": False, "error": "unknown action"}, status_code=404)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, bridge.submit, action, body, 15.0)
        return JSON(result, status_code=200 if result.get("ok") else 400)

    async def api_verify_token(request: Request):
        return JSON({"ok": request.headers.get("x-dashboard-token") == token})

    # ------------------------------------------------------------------ websocket
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        clients.add(ws)
        try:
            await ws.send_text(json.dumps([{"event": "state", **bridge.get_state()}], default=json_default))
            while True:
                await ws.receive_text()     # client pings; we ignore content
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            clients.discard(ws)

    routes = [
        Route("/", page("index")), Route("/realtime", page("realtime")), Route("/logs", page("logs")),
        Route("/config", page("config")), Route("/analysis", page("analysis")),
        Route("/api/analysis/{kind}", api_analysis), Route("/api/research/lab", api_research_lab),
        Route("/api/state", api_state), Route("/api/events", api_recent), Route("/api/equity", api_equity),
        Route("/api/trades/today", api_trades_today), Route("/api/logs/{tab}", api_logs),
        Route("/api/counterfactuals/summary", api_cf_summary),
        Route("/api/candidates/summary", api_candidate_summary), Route("/api/config", api_config),
        Route("/api/integrity", api_integrity),
        Route("/api/control/{action}", api_control, methods=["POST"]),
        Route("/api/verify-token", api_verify_token, methods=["POST"]),
        Route("/sw.js", service_worker),
        WebSocketRoute("/ws", ws_endpoint),
        Mount("/static", app=StaticFiles(directory=str(STATIC)), name="static"),
    ]
    return Starlette(routes=routes, lifespan=lifespan)
