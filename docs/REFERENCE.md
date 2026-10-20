# Quantara — v0.1

Autonomous quantitative trading, research and execution platform for **MetaTrader 5 / Exness**, built from the
*Architecture & Technical Specification v0.1*. Python, PostgreSQL, localhost web control center.

> **Read `docs/SETUP_GUIDE.md` first.** It walks through installation and the safe path
> DRY_RUN → SHADOW → DEMO, step by step. This release prohibits REAL order execution.

**Important:** `pullback_v1` is a *hypothesis*, not a proven edge (spec §9). The software is built to find out
whether it has an edge under real Exness costs, and to say **NO TRADE** when it doesn't. It ships in `DRY_RUN`
and rejects REAL mode in configuration, startup, and the execution engine. Evidence or confirmation phrases cannot
override this release policy.
Nothing in this package is financial advice.

Live mode scans each configured broker symbol with an independent trading core in round-robin cycles. The sample
pool in `config\config.json` includes metals, crypto, energy, FX, and US equities; remove any symbols your broker
does not offer and use its exact Market Watch names. Multi-symbol risk remains constrained by the shared open-risk and margin
ceilings. Two or more consecutive losses halve the next position's risk on that symbol. The configured symbol
loss-cooldown is currently 0 minutes, so the loss streak does not itself block another entry; loss history remains
recorded and continues to reduce sizing. All other risk, health, spread, and portfolio gates remain active.
The default portfolio ceilings are 10 concurrent positions, 10% aggregate open risk, and 1.5% maximum risk per
trade; the ordinary research sizing fraction remains 1%, so these are limits rather than target sizes.
New entries require both a broker-resident stop-loss and take-profit; missing bracket legs are attached after fill
or the unprotected position is closed according to the execution safety setting. The configured exit models tighten
the stop to spread-covering breakeven after a 0.5R favorable move and prevent additional entries on any symbol that
already has an open position or pending signal.
The execution setting `hard_profit_target_money` (1.00 account-currency units by default) triggers a market close
when estimated net floating P/L reaches the target. Current broker P/L already reflects the live bid/ask spread;
the estimate additionally subtracts configured round-trip commission and adverse slippage. The broker-side
take-profit and protective stop remain in place. A close is sent when the tick manager observes the threshold, but
market movement, broker latency, and fill slippage mean realized profit may be below the target.
Dynamic profit protection tracks the peak estimated net P/L and persists it across restarts. After reaching
`small_win_activation_money` (+0.20), a retrace to `small_win_floor_money` (+0.05) requests a market close. At
`ambition_activation_money` (+0.90), the floor rises to `ambition_floor_money` (+0.50) and then ratchets upward with
the peak, maintaining a $0.40 gap; the +$1.00 hard target still closes first. When the profit-lock stop is active,
the bot also tries to move the broker-side stop to the current floor after estimated costs. Failed stop changes only
trigger a market-close fallback when a fresh position check confirms estimated net P/L remains at or above that
floor; otherwise the existing protective stop stays in place. Stop changes clear broker stop/freeze distance plus
`profit_lock_stop_buffer_points` (3 symbol points by default). These are estimated thresholds, not guarantees:
gaps, market movement, latency, costs, and slippage can result in a lower realized profit or a loss.
The DEMO `micro_scalper` requires a closed M1 bullish/bearish pin bar or engulfing candle; there is no
single-candle momentum fallback. Signal-candle tick volume must reach at least the median of the previous 20 closed
M1 candles. An aligned 40-bar H4 regression channel must place a buy in its lower 30% during an uptrend or a sell
in its upper 30% during a downtrend. An opposing M15 direction blocks the candidate; a neutral one does not.
Candidate analysis uses market data only: M1 candlestick and tick-volume confirmation, M15 direction, and the H4
macro channel. Spread, cost, risk, health, portfolio and execution protections all remain in force.
Its stop and target each scale to 1.0 times the latest closed-bar M1 ATR(14), subject to the live spread and
broker minimum stop distance. Quiet conditions therefore produce closer levels and higher volatility widens them.
It does not require a liquidity sweep/FVG retest or a confluence score. Spread remains
a hard gate: each symbol has its own observed spread distribution and session percentile, with the configured
300-point absolute ceiling as a backstop. The cost, risk, margin, portfolio, and broker-side stop-loss/take-profit
checks also remain mandatory. The dynamic profit floors target positive estimated net P/L after configured execution
costs, but cannot guarantee positive realized profit. Losses reduce subsequent position
sizing and repeated losses can pause that symbol, but the strategy does not automatically rewrite or "learn" entry
rules. Tick data and momentum are not evidence of a profitable edge. Validate the hypothesis in DEMO and review
rejected signals and costs before considering any promotion. Historical backtests exercise candlestick,
M15-direction, macro, and volume gates. For a strategy backtest, export M1 bars alongside M15/H1
(`python -m app.main export-history --start 2024-01-01 --timeframes M1,M15,H1`) and pass the M1 file with
`--m1 research\datasets\<symbol>_M1.csv`; without M1 data, the micro-scalper correctly produces no historical
signals.
Automated live grid/DCA order stacking is not implemented. `research_grid` is only for parameter experiments;
the optional grid simulator is strictly a historical research mode.
The opt-in `backtest --research-grid` experiment now models a pin-bar/engulfing-gated M1 basket, adds up to two
more equally sized layers at 1.0 M1 ATR intervals, caps planned basket stop risk at 1% of equity, applies a
conservative 100:1 leverage / 50% equity margin assumption, and closes the basket at $1 net estimated P/L after
commission and slippage. Its shared stop is 1.0 M1 ATR beyond the third layer. This remains a historical
simulation only; it does not enable or imply live/Demo grid execution.

## Per-symbol Start / Stop, XAUUSD247m, news straddle (added)
* **Dashboard -> Overview -> "Symbols - Start / Stop trading"**: one row per running symbol with a Stop/Start button
  (needs the dashboard token). STOP = no new entries for that symbol: strategy evaluation is skipped,
  waiting signals and resting straddle orders are cancelled. Open positions keep their broker-side SL/TP and normal
  management. The state is stored in `system_flags` (`symbol_trading`) and survives restarts; untouched symbols run.
* **XAUUSD247m** is in `broker.symbol_candidates` and in `broker.always_open_symbols`: the estimated Fri 21:00 -> Sun 22:00 UTC
  weekend closure is not applied to it (weekend quotes use their own `WEEKEND` spread bucket). Other symbols keep the
  weekend schedule. It is ~100% correlated with XAUUSDm, so the shared correlated-risk cap applies to the pair.
* **News straddle** (`news_straddle` in config.json, **ON since 16 Oct across all 13 symbols**, DEMO/DRY_RUN only): ~5 min before a high-impact USD
  event it places BUY STOP + SELL STOP (ATR-scaled distances, broker SL/TP, risk split 50/50 between legs), runs OCO
  (opposite order cancelled the moment one fills), stands down on spread shock before the release / kill switch / symbol
  STOP / risk emergency / low margin level, and cancels at expiry and on shutdown. Event times come from
  `config/economic_events.csv` (import-events) or `calendar_sync` (free weekly JSON feed, on).
  Resting legs are capped by `news_straddle.max_armed_symbols` (13) and are deliberately NOT counted against
  `risk.max_concurrent_positions`: OCO guarantees at most one leg per symbol can become a position, and charging both
  meant the first five symbols consumed the whole allowance while every later symbol was silently refused. Money at
  risk stays bounded by the portfolio reservation; `leg_risk_share` is 0.25, so 26 resting legs reserve 9.75% of the
  10% cap and real exposure once every straddle triggers is about 4.9%.
* **Hourly adaptive parameters** (`adaptive` in config.json, **ON since 16 Oct**): once per `interval_minutes` each symbol refreshes
  (a) `vol_scale` = recent 4h ATR / 24h median ATR, clamped to 0.75-1.5 and moved at most 0.15 per cycle - it scales the
  news-straddle distances - and (b) a per-symbol spread ceiling from recent bar spreads that can only TIGHTEN the global
  limit. Lot size, risk fractions and loss limits are never adapted. It runs on the trading thread (MT5 is not thread
  safe), values hot-reload on the next evaluation, persist in `system_flags` (`adaptive:<symbol>`) and every refresh is
  logged to `adaptive_param_history`. It deliberately does not grid-search parameters on a few hours of bars (curve fitting).
  Note that with this on, the adaptive cap is frequently the BINDING spread ceiling, so expect more SPREAD rejections
  than before; `spread_limit_points()` reports `"hourly adaptive cap"` as its basis whenever that is the case.
* **`scripts\4_collect_spreads.bat`** now records every symbol in `broker.symbol_candidates` (or `--symbols a,b`) into `ticks`.
  Existing databases: run `database/02_migration_v0_2.sql` once.
* **Order flow / microstructure (research-only)**: `app/microstructure/` - quote-rule proxies for CVD, delta bursts, buy/sell
  aggression, session VWAP + deviation, tick-volume profile (POC / value area / POC shift), VPIN, plus order-book
  imbalance / walls / pulled walls / refill hints when the broker publishes depth. MT5 CFDs have no trade tape, open
  interest or liquidation data, so those are reported UNAVAILABLE and nothing here gates a trade; the compact snapshot
  is stored in `signals.features` for later testing.
* **Startup safety**: the bot now compares the database with `01_schema.sql` and creates missing tables itself;
  failed writes to analytics tables (`candidate_evaluations`, `counterfactuals`) no longer halt trading; REDUCED/RESTRICTED
  risk tiers use a recovery band (`risk.dd_recovery_band`) so equity hovering at a threshold does not flap.
* **Several positions**: a protected winner (stop at/beyond breakeven) no longer blocks every other entry - open risk of such a
  position is zero. `scaling` allows adding to a same-direction winner on the same symbol (max 3, only when every open position
  is protected and >= 0.5R in profit, each add at half risk).
* **Exits**: `hard_profit_target_money` is 0 (no fixed $1 ceiling) and `execution.profit_giveback_fraction` 0.3 trails the peak
  profit, giving back at most 30% of it; the broker-side SL stays in place.
* **Dashboard -> Analysis**: losing trades (amount, strategy, setup, checks) and blocked signals (system / risk / cost) with a "positive calculated edge" filter.
* **Research lab (VectorBT + Backtrader)**: started with the bot as a separate low-priority process (`research_lab` in config.json).
  Every 6 hours it reads recent M1/M15 history from MT5 (history only, no trading calls), replays the bot's real
  micro_scalper entries, sweeps stop-loss (xATR) and reward:risk with VectorBT (first 70% = tune, last 30% = unseen test),
  cross-checks the best set bar-by-bar with Backtrader, and stores everything in `research/results/lab/<time>/`, table
  `research_lab_runs` and the dashboard Analysis page. Run one cycle by hand: `python -m app.main research-lab`. Advisory
  only - it never changes live parameters; the latest verdict is stored with each new signal for later analysis.
* **Spread gate fixes**: the spread percentile is now mid-rank (a spread equal to the usual value of a fixed-spread symbol was
  ranked 100% and blocked all day) and `spread_gate.symbol_max_spread_points` sets per-symbol ceilings from your collected data.
* **Why micro_scalper lost money - and the fix**: with the stop at 1x M1 ATR and a wide broker spread, the spread was a
  large fraction of the stop. A driftless random walk pays about **-(spread / stop) R per trade**, whatever the
  reward:risk, so the strategy was negative by construction. Three changes: `cost.max_spread_fraction_of_stop` (0.10)
  rejects or WAITs on any signal whose stop is less than 10x the spread; micro_scalper floors its stop at
  `min_stop_spread_multiple` (10) x the live spread; and `min_reward_risk` sets the target from the final stop.
* **Profit lock**: when the stop cannot be tightened (broker minimum distance), the position is now KEPT on its existing
  broker stop and retried instead of being closed at market - set `execution.close_when_profit_lock_unprotectable` to
  restore the old behaviour.

## Quick start (Windows)
```
scripts\1_install.bat          create .venv, install packages, create .env
scripts\2_check.bat            Python, packages, PostgreSQL, MT5 all reachable?
scripts\6_run_simulation.bat   see the whole system + dashboard on a simulated market
scripts\3_feasibility_report.bat   Phase 0: can $100 trade / test XAUUSD on your account?
scripts\5_research.bat         Phase 2: history export, backtest, costs, exits, sessions, walk-forward
scripts\7_run_bot.bat          trading core + dashboard (environment from config\config.json)
```
Dashboard: <http://127.0.0.1:8787> — Overview, Real-Time, Logs & Audit, Configuration.

## What is built
| Spec area | Where | Status |
|---|---|---|
| Phase 0 broker feasibility (§6, §27) | `app/research/feasibility.py` | ✅ |
| MT5 adapter behind a broker interface (§34) | `app/broker/` | ✅ native MetaTrader5 API; paper + simulator adapters |
| Temporal integrity, hydration from the feature graph (§38, §40) | `app/market/bars.py` | ✅ |
| Startup state machine (§39) | `app/core/engine.py` | ✅ no trading before ACTIVE |
| Pullback v1 (§9) and Range Mean Reversion v1 (shadow) | `app/strategies/` | ✅ identical code live and in backtests |
| Regime engine + strategy router (§22–23) | `app/regime/` | ✅ deterministic, with hysteresis |
| Cost model, edge gate, shrinkage, fractional Kelly (§19, §26) | `app/edge/`, `app/risk/sizing.py` | ✅ never rounds size up |
| Risk state machine, daily loss, drawdown, kill switch (§60) | `app/risk/` | ✅ durable across restarts |
| Atomic portfolio risk reservations and synchronized H1 correlation caps | `app/storage/repo.py`, `app/risk/correlation.py` | ✅ 10% total risk cap; 5% correlated-group cap; unknown correlation blocks new exposure |
| Candidate funnel and rejection analytics | `app/core/engine.py`, `app/dashboard/` | ✅ strategy evaluation outcomes are traceable in Logs & Audit |
| Execution: revalidation, order_check, durable intent, partial fills (§41–43) | `app/execution/engine.py` | ✅ MARKET + WAIT routing; LIMIT reserved |
| Reconciliation, scoped, backoff, adoption of unknown positions (§44, §68) | `app/execution/reconciliation.py` | ✅ |
| Exit models A–G (§15) | `app/alpha/exits.py` | ✅ shared by live, backtest, counterfactuals |
| Counterfactual engine + ledger (§13–14) | `app/alpha/counterfactuals.py` | ✅ survives restarts |
| Two-speed governance, strategy health (§24–25) | `app/governance/health.py` | ✅ |
| Custom backtester, cost stress, exit/session research, H1–H8 (§10, §16, §35) | `app/research/` | ✅ |
| Walk-forward, parameter stability, DSR, Monte Carlo, evidence levels (§28–29) | `app/research/` | ✅ multiprocess |
| Promotion pipeline with human approval (§30, §72) | `python -m app.main promote` | ✅ |
| PostgreSQL storage, critical vs batched telemetry (§49–50) | `app/storage/`, `database/` | ✅ fail-safe journal |
| Dashboard: 4 pages, WebSocket, validated + audited config (§51–63) | `app/dashboard/` | ✅ localhost + token |
| "Wake me up" alerts + daily report (Discord / ntfy / email / webhook) | `app/notifications/` | ✅ optional |
| Failure-injection and chaos tests (§66–67) | `tests/chaos/` | ✅ 22 scenarios |
| AI analysts, ML, volume profile, microstructure (§20–21, §32–33) | — | ⏸ Phase 8 by design: `research.ai_enabled=false` |

## Command line
```
python -m app.main check | initdb | feasibility [--equity 100] | collect-spreads --minutes 60
python -m app.main export-history --start 2020-01-01
python -m app.main build-bars --ticks <exness_ticks.csv> --out research/datasets/XAUUSD_M15.csv
python -m app.main import-events config/economic_events.csv
python -m app.main backtest --strategy micro_scalper --m1 research/datasets/XAUUSD_M1.csv --research-grid
python -m app.main backtest|costs|exits|sessions|walkforward --strategy pullback_v1 [--equity 100 --risk 0.01]
python -m app.main promote --strategy pullback_v1 --experiment <id> --to MICRO_LIVE --reason "..."
python -m app.main run [--simulate] [--no-dashboard]
python -m app.main status
```
(`--log-level DEBUG` goes before the command: `python -m app.main --log-level DEBUG run`.)

## Tests
`scripts\run_tests.bat` or `python -m unittest discover -s tests -t . -v` — unit and integration coverage for indicators and causality,
temporal integrity, sizing, gates, exits, config safety, 22 chaos scenarios (process killed mid-order, ambiguous
results, rejects, partial fills, dropped stop loss, manual closes, unexpected positions, MT5 disconnect,
database outage, corrupt/duplicate/delayed ticks, …), full-pipeline simulations, backtest look-ahead test, and
causal market-structure filters; PostgreSQL integration (set `TEST_DATABASE_URL` to a throw-away database).

## Layout
```
app/        core, broker, market, features, regime, strategies, edge, risk, execution, alpha,
            research, storage, governance, dashboard, notifications
config/     config.json (runtime), strategies.json (strategy + parameter registry), economic_events.csv
database/   00a_create_role.sql, 00b_create_database.sql, 01_schema.sql, README_DATABASE.md
docs/       SETUP_GUIDE.md
research/   datasets/ (history), reports/ (evidence packages)
scripts/    Windows launchers
tests/      unit/, chaos/, integration/
logs/       quantara.log, critical_journal.jsonl (only if the database ever fails)
```
