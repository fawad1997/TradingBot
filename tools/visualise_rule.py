#!/usr/bin/env python3
"""See where a rule fires, and which of its signals won - on your own broker's candles.

The research lab tells you a rule produced 84 setups a week for -31.8R. It cannot tell you whether those
setups were genuine stretches or every small wobble, and that is usually the thing worth knowing. This writes a
self-contained HTML chart: your Exness bars, every signal marked, each marker coloured by what that signal
actually did - green for a winner, red for a loser.

Charting is TradingView's Lightweight Charts, loaded from a CDN. The DATA is yours: tradingview.com carries
OANDA and Pepperstone feeds, not Exness, so for inspecting what your bot would have traded this chart is the
accurate one and theirs is not.

    python tools/visualise_rule.py --rule vwap_reversion
    python tools/visualise_rule.py --rule breakout_retest --bars 4000 --out breakout.html
    python tools/visualise_rule.py --rule momentum_trend --param min_mom_atr=2.0

Open the resulting .html in any browser. No server, no build step, nothing to install.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import sys
import webbrowser
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import ConfigManager                     # noqa: E402
from app.features.frame import FeatureFrame              # noqa: E402
from app.research.lab.hypotheses import HYPOTHESES       # noqa: E402
from app.research.visualise import build_view            # noqa: E402

# pinned: a floating version would change the chart under you between runs
CHART_LIB = "https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"

RULE_TO_STRATEGY = {"breakout_retest": "breakout_retest_v1", "momentum_trend": "momentum_trend_v1",
                    "session_breakout": "session_breakout_v1", "vwap_reversion": "vwap_reversion_v1",
                    "htf_continuation": "htf_continuation_v1"}


def load_csv(path: Path, timeframe: str) -> FeatureFrame:
    t, o, h, l, c, v, s = [], [], [], [], [], [], []
    with path.open() as f:
        for r in csv.DictReader(f):
            t.append(int(r["time"])); o.append(float(r["open"])); h.append(float(r["high"]))
            l.append(float(r["low"])); c.append(float(r["close"]))
            v.append(float(r.get("tick_volume") or 0)); s.append(float(r.get("spread") or 0))
    return FeatureFrame(np.array(t, np.int64), np.array(o), np.array(h), np.array(l), np.array(c),
                        np.array(s), np.array(v), timeframe)


def tail(f: FeatureFrame, n: int) -> FeatureFrame:
    n = min(n, len(f))
    return FeatureFrame(f.t[-n:], f.o[-n:], f.h[-n:], f.l[-n:], f.c[-n:], f.spread[-n:], f.tick_volume[-n:],
                        f.timeframe)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{--bg:#0e1116;--panel:#161b22;--line:#222a35;--ink:#e6edf3;--dim:#8b949e;
        --win:#2ea043;--loss:#e5534b;--accent:#4f8cc9}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);
    font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
  header{padding:14px 18px;border-bottom:1px solid var(--line)}
  h1{margin:0;font-size:16px;font-weight:600} .sub{color:var(--dim);font-size:12px;margin-top:2px}
  .stats{display:flex;flex-wrap:wrap;gap:10px;padding:12px 18px}
  .s{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 12px;min-width:104px}
  .s b{display:block;font-size:17px;font-weight:600} .s span{color:var(--dim);font-size:11px}
  .win b{color:var(--win)} .loss b{color:var(--loss)}
  #chart{height:62vh;min-height:380px;margin:0 14px;border:1px solid var(--line);border-radius:8px}
  .legend{padding:8px 18px;color:var(--dim);font-size:12px}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin:0 4px 0 12px;vertical-align:middle}
  footer{padding:10px 18px 24px;color:var(--dim);font-size:11px}
  @media(max-width:700px){#chart{height:52vh;margin:0 8px}.stats{padding:10px 8px}}
</style></head><body>
<header><h1>__TITLE__</h1><div class="sub">__SUB__</div></header>
<div class="stats">__STATS__</div>
<div id="chart"></div>
<div class="legend"><span class="dot" style="background:var(--win)"></span>winner
  <span class="dot" style="background:var(--loss)"></span>loser
  &nbsp;&mdash;&nbsp; hover a marker for its R, exit and bars held. Scroll to zoom, drag to pan.</div>
<footer>__FOOT__</footer>
<script src="__LIB__"></script>
<script>
const DATA = __DATA__;
const el = document.getElementById('chart');
const chart = LightweightCharts.createChart(el, {
  layout:{background:{color:'#0e1116'},textColor:'#e6edf3'},
  grid:{vertLines:{color:'#1b222c'},horzLines:{color:'#1b222c'}},
  rightPriceScale:{borderColor:'#222a35'},
  timeScale:{borderColor:'#222a35',timeVisible:true,secondsVisible:false},
  crosshair:{mode:LightweightCharts.CrosshairMode.Normal}
});
const series = chart.addCandlestickSeries({
  upColor:'#2ea043',downColor:'#e5534b',borderUpColor:'#2ea043',borderDownColor:'#e5534b',
  wickUpColor:'#2ea043',wickDownColor:'#e5534b'
});
series.setData(DATA.bars);
series.setMarkers(DATA.markers.map(m => ({
  time:m.time,
  position: m.side === 'long' ? 'belowBar' : 'aboveBar',
  shape:    m.side === 'long' ? 'arrowUp' : 'arrowDown',
  color:    m.won ? '#2ea043' : '#e5534b',
  text:     `${m.side==='long'?'L':'S'} ${m.r>0?'+':''}${m.r}R ${m.exit} ${m.bars_held}b`
})));
chart.timeScale().fitContent();
new ResizeObserver(() => chart.applyOptions({width:el.clientWidth,height:el.clientHeight})).observe(el);
</script></body></html>
"""


def render(view: dict, title: str, sub: str, foot: str) -> str:
    s = view["stats"]
    def card(label, value, cls=""):
        return f'<div class="s {cls}"><b>{html.escape(str(value))}</b><span>{html.escape(label)}</span></div>'
    cards = "".join([
        card("signals", s["signals"]),
        card("per week", s["per_week"]),
        card("total R", f'{s["total_r"]:+.1f}', "win" if s["total_r"] > 0 else "loss"),
        card("avg R / trade", f'{s["avg_r"]:+.3f}', "win" if s["avg_r"] > 0 else "loss"),
        card("win rate", f'{100 * s["win_rate"]:.0f}%'),
        card("avg win", f'{s["avg_win_r"]:+.2f}R', "win"),
        card("avg loss", f'{s["avg_loss_r"]:+.2f}R', "loss"),
    ])
    return (PAGE.replace("__TITLE__", html.escape(title)).replace("__SUB__", html.escape(sub))
                .replace("__STATS__", cards).replace("__FOOT__", html.escape(foot))
                .replace("__LIB__", CHART_LIB)
                .replace("__DATA__", json.dumps({"bars": view["bars"], "markers": view["markers"]})))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rule", required=True, choices=sorted(HYPOTHESES))
    ap.add_argument("--symbol", default="XAUUSD", help="dataset name under research/datasets")
    ap.add_argument("--bars", type=int, default=3000, help="most recent M15 bars to chart")
    ap.add_argument("--spread", type=float, default=None, help="price units; default = dataset median")
    ap.add_argument("--slippage", type=float, default=None, help="price units; default = config cost setting")
    ap.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                    help="override a rule parameter, repeatable")
    ap.add_argument("--out", default=None)
    ap.add_argument("--open", action="store_true", help="open it in the browser when done")
    a = ap.parse_args()

    base = ROOT / "research" / "datasets"
    m15_path, h1_path = base / f"{a.symbol}_M15.csv", base / f"{a.symbol}_H1.csv"
    if not m15_path.exists():
        have = sorted({p.name.split("_")[0] for p in base.glob("*_M15.csv")}) if base.exists() else []
        print(f"  no dataset for {a.symbol}. Available: {have or '(none - export history first)'}")
        return 1

    m15 = tail(load_csv(m15_path, "M15"), a.bars)
    h1 = load_csv(h1_path, "H1") if h1_path.exists() else None

    cfgm = ConfigManager()
    sid = RULE_TO_STRATEGY.get(a.rule)
    params = dict(cfgm.strategy(sid).param_values()) if sid else {}
    for kv in a.param:
        if "=" not in kv:
            print(f"  --param needs NAME=VALUE, got {kv!r}")
            return 1
        k, v = kv.split("=", 1)
        try:
            params[k] = float(v) if "." in v else int(v)
        except ValueError:
            params[k] = v.strip().lower() in ("1", "true", "yes") if v.strip().lower() in (
                "1", "true", "yes", "0", "false", "no") else v
    point = 0.01 if "XAU" in a.symbol.upper() else 0.00001
    spread = a.spread if a.spread is not None else float(np.median(m15.spread)) * point
    slippage = a.slippage if a.slippage is not None else cfgm.app.cost.default_slippage_points * point

    view = build_view(a.rule, m15, h1, params,
                      spread=spread, slippage=slippage,
                      stop_atr=float(params.get("stop_atr", 2.0)),
                      reward_risk=float(params.get("reward_risk", 1.5)),
                      min_stop_spread_multiple=float(params.get("min_stop_spread_multiple", 10.0)))
    s = view["stats"]

    out = Path(a.out) if a.out else ROOT / "research" / "reports" / f"{a.rule}_{a.symbol}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    overrides = " ".join(a.param) or "config defaults"
    out.write_text(render(
        view,
        f"{a.rule} on {a.symbol} M15",
        f'{s["signals"]} signals over {s["bars"]} bars · {s["per_week"]}/week · '
        f'stop {s["stop_atr"]}xATR · target {s["reward_risk"]}R · spread {spread:g} · slippage {slippage:g}',
        f"Parameters: {overrides}. Outcomes simulated to each signal's own stop or target, pessimistic when a "
        f"bar touches both, costs charged on exit. Candles are your own broker data.",
    ), encoding="utf-8")

    print(f"  {a.rule} on {a.symbol}: {s['signals']} signals, {s['per_week']}/week, "
          f"{s['total_r']:+.1f}R total, {100 * s['win_rate']:.0f}% won")
    print(f"  avg win {s['avg_win_r']:+.2f}R   avg loss {s['avg_loss_r']:+.2f}R   avg {s['avg_r']:+.3f}R/trade")
    print(f"  wrote {out}")
    if a.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
