"""Phase 0 - Broker Feasibility Engine (sections 4-6, 27).

Answers two DIFFERENT questions from the connected account (never assumed):
  1. Can this capital trade the symbol safely?
  2. Can this capital meaningfully TEST the strategy?
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import numpy as np

from app.broker.base import BrokerAdapter
from app.config import AppConfig, StrategyCfg
from app.core.types import Side, SymbolSpec, new_id, utcnow
from app.features.frame import FeatureFrame
from app.market.sessions import SessionClassifier


def _q(vals: List[float], p: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    return round(s[int(p * (len(s) - 1))], 2)


def spread_distribution(broker: BrokerAdapter, symbol: str, spec: SymbolSpec, cfg: AppConfig, days: int = 5) -> Dict:
    """Spread statistics from MT5 tick history (falls back to M15 bar spreads)."""
    sess = SessionClassifier(cfg.sessions)
    end = utcnow()
    start = end - timedelta(days=days)
    by: Dict[str, List[float]] = {}
    source = "ticks"
    try:
        ticks = broker.ticks_range(symbol, start, end)
    except Exception:
        ticks = []
    if len(ticks) > 1000:
        step = max(1, len(ticks) // 200000)
        for t in ticks[::step]:
            s = str(sess.classify(datetime.fromtimestamp(t.time_msc / 1000, tz=timezone.utc)))
            by.setdefault(s, []).append((t.ask - t.bid) / spec.point)
    else:
        source = "M15 bar spread field (approximation)"
        for b in broker.rates(symbol, "M15", 96 * days):
            s = str(sess.classify(datetime.fromtimestamp(b.time, tz=timezone.utc)))
            by.setdefault(s, []).append(float(b.spread))
    allv = [v for vs in by.values() for v in vs]
    out = {"source": source, "days": days, "samples": len(allv),
           "overall": {"p50": _q(allv, 0.5), "p90": _q(allv, 0.9), "p99": _q(allv, 0.99), "max": max(allv) if allv else None}}
    out["by_session"] = {k: {"n": len(v), "p50": _q(v, 0.5), "p90": _q(v, 0.9), "p99": _q(v, 0.99)} for k, v in by.items()}
    return out


def feasibility_report(broker: BrokerAdapter, symbol: str, cfg: AppConfig, strategy: Optional[StrategyCfg] = None,
                       equity_override: Optional[float] = None) -> Dict:
    acct = broker.account_info()
    spec = broker.symbol_spec(symbol)
    tick = broker.tick(symbol)
    equity = equity_override if equity_override is not None else acct.equity
    price = tick.ask if tick else None
    risk = cfg.risk
    vmin = spec.volume_min

    def loss(volume: float, dist: float) -> float:
        p = broker.calc_profit(symbol, Side.BUY, volume, price, price - dist) if price else None
        return abs(p) if p is not None else spec.loss_per_lot(dist) * volume

    money_per_unit_min = loss(vmin, 1.0)                     # $ per 1.00 price move at min volume
    margin_min = broker.calc_margin(symbol, Side.BUY, vmin, price) if price else None
    m15 = broker.rates(symbol, "M15", 2000)
    ff = FeatureFrame.from_bars(m15[:-1], "M15") if len(m15) > 50 else None
    atr_vals = [float(x) for x in ff.atr(14)[-1500:] if np.isfinite(x)] if ff is not None else []
    atr_med = statistics.median(atr_vals) if atr_vals else None
    stops = {}
    if atr_med:
        for mult in (0.5, 1.0, 1.5, 2.0, 3.0):
            d = atr_med * mult
            r = loss(vmin, d) + cfg.cost.commission_per_lot_round_trip * vmin
            stops[f"{mult} x ATR(M15) = {d:.2f}"] = {"risk_at_min_volume": round(r, 2),
                                                    "pct_of_equity": round(100 * r / equity, 2) if equity else None}
    spreads = spread_distribution(broker, symbol, spec, cfg)
    cap_money = equity * risk.max_risk_fraction_per_trade
    research_money = equity * risk.research_risk_fraction
    max_stop_cap = cap_money / money_per_unit_min if money_per_unit_min else None
    max_stop_research = research_money / money_per_unit_min if money_per_unit_min else None
    typical_stop = (atr_med * 1.5) if atr_med else None
    typical_risk = loss(vmin, typical_stop) if typical_stop else None
    sp50 = spreads["overall"]["p50"] or 0.0
    spread_r = (sp50 * spec.point / typical_stop) if typical_stop else None
    margin_pct = (100 * margin_min / equity) if (margin_min and equity) else None

    reasons_trade, reasons_test = [], []
    safe = "YES"
    if spec.trade_mode != "FULL":
        safe, reasons_trade = "NO", reasons_trade + [f"symbol trade mode {spec.trade_mode}"]
    if typical_risk is not None and typical_risk > cap_money:
        safe = "NO"
        reasons_trade.append(f"a typical stop (1.5 x ATR = {typical_stop:.2f}) at the minimum volume {vmin} risks "
                             f"{typical_risk:.2f} {acct.currency} = {100 * typical_risk / equity:.1f}% of equity, above "
                             f"the {100 * risk.max_risk_fraction_per_trade:.1f}% per-trade ceiling")
    if margin_pct is not None and margin_pct > 100 * risk.max_margin_usage_fraction:
        safe = "NO"
        reasons_trade.append(f"minimum position needs {margin_pct:.1f}% of equity as margin")
    test = "YES"
    if typical_risk is not None and typical_risk > research_money:
        test = "LIMITED"
        reasons_test.append(f"the research risk budget ({100 * risk.research_risk_fraction:.1f}% = {research_money:.2f}) "
                            f"is smaller than the minimum-volume risk of a typical stop ({typical_risk:.2f}); most "
                            f"signals will be rejected as POSITION_SIZE_INFEASIBLE")
    if spread_r is not None and spread_r > 0.15:
        test = "LIMITED" if test == "YES" else test
        reasons_test.append(f"median spread is {spread_r:.2f} R of a typical stop - costs dominate")
    if safe == "NO" and test == "LIMITED":
        test = "NO (use DEMO / SHADOW / backtests, or a cent account)"
    if not reasons_trade:
        reasons_trade.append("minimum position risk and margin fit inside the configured ceilings")
    recs = []
    if safe == "NO":
        recs += ["Run the system in DRY_RUN / SHADOW / DEMO and research with backtests - this is a valid outcome.",
                 "If your Exness account type offers XAUUSDc (Standard Cent), run this report on that account: "
                 "cent lots are 100x smaller in money terms.",
                 "Or increase capital until the minimum position risk fits the ceiling."]
    rep = {
        "id": new_id(), "created_at": utcnow().isoformat(),
        "account": {"login": acct.login, "server": acct.server, "company": acct.company, "currency": acct.currency,
                    "trade_mode": acct.trade_mode, "leverage": acct.leverage, "balance": acct.balance,
                    "equity": acct.equity, "equity_used_for_report": equity, "hedging": acct.hedging},
        "symbol": {"name": spec.name, "description": spec.description, "contract_size": spec.contract_size,
                   "volume_min": spec.volume_min, "volume_step": spec.volume_step, "volume_max": spec.volume_max,
                   "digits": spec.digits, "point": spec.point, "tick_size": spec.tick_size,
                   "tick_value": spec.tick_value, "stops_level_points": spec.stops_level,
                   "freeze_level_points": spec.freeze_level, "execution_mode": spec.execution_mode,
                   "filling_modes": {"FOK": bool(spec.filling_mode_flags & 1), "IOC": bool(spec.filling_mode_flags & 2)},
                   "trade_mode": spec.trade_mode, "swap_long": spec.swap_long, "swap_short": spec.swap_short,
                   "currency_profit": spec.currency_profit},
        "price": price,
        "minimum_position": {"volume": vmin, "money_per_1.00_price_move": round(money_per_unit_min, 4),
                             "margin_required": None if margin_min is None else round(margin_min, 2),
                             "margin_pct_of_equity": None if margin_pct is None else round(margin_pct, 2)},
        "atr_m15_median": None if atr_med is None else round(atr_med, 3),
        "stop_distance_feasibility": stops,
        "risk_ceilings": {"per_trade_max_money": round(cap_money, 2),
                          "max_stop_distance_at_min_volume_for_ceiling": None if max_stop_cap is None else round(max_stop_cap, 3),
                          "research_budget_money": round(research_money, 2),
                          "max_stop_distance_at_min_volume_for_research_budget": None if max_stop_research is None else round(max_stop_research, 3)},
        "spread": spreads,
        "median_spread_as_R_of_typical_stop": None if spread_r is None else round(spread_r, 4),
        "verdict": {"can_trade_safely": safe, "why_trade": reasons_trade,
                    "can_test_strategy_meaningfully": test, "why_test": reasons_test or ["costs and size are workable"]},
        "recommendations": recs,
    }
    rep["markdown"] = render_markdown(rep)
    return rep


def render_markdown(r: Dict) -> str:
    v = r["verdict"]
    lines = [f"# Feasibility report - {r['symbol']['name']} on {r['account']['server']}", "",
             f"Account {r['account']['login']} ({r['account']['trade_mode']}), equity used: "
             f"{r['account']['equity_used_for_report']} {r['account']['currency']}", "",
             f"## Can this capital trade {r['symbol']['name']} safely?  **{v['can_trade_safely']}**", ""]
    lines += [f"- {x}" for x in v["why_trade"]]
    lines += ["", f"## Can this capital meaningfully test the strategy?  **{v['can_test_strategy_meaningfully']}**", ""]
    lines += [f"- {x}" for x in v["why_test"]]
    lines += ["", "## Contract specification (from the broker)", "", "| field | value |", "|---|---|"]
    lines += [f"| {k} | {val} |" for k, val in r["symbol"].items()]
    lines += ["", "## Minimum position", "", "| field | value |", "|---|---|"]
    lines += [f"| {k} | {val} |" for k, val in r["minimum_position"].items()]
    lines += ["", "## Stop distance vs risk at minimum volume", "", "| stop | risk | % equity |", "|---|---|---|"]
    lines += [f"| {k} | {x['risk_at_min_volume']} | {x['pct_of_equity']} |" for k, x in r["stop_distance_feasibility"].items()]
    lines += ["", "## Risk ceilings", "", "| field | value |", "|---|---|"]
    lines += [f"| {k} | {val} |" for k, val in r["risk_ceilings"].items()]
    sp = r["spread"]
    lines += ["", f"## Spread distribution ({sp['source']}, {sp['samples']} samples, points)", "",
              "| session | n | p50 | p90 | p99 |", "|---|---|---|---|---|"]
    lines += [f"| {k} | {x['n']} | {x['p50']} | {x['p90']} | {x['p99']} |" for k, x in sp["by_session"].items()]
    if r["recommendations"]:
        lines += ["", "## Recommendations", ""] + [f"- {x}" for x in r["recommendations"]]
    return "\n".join(lines)
