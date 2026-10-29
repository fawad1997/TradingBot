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
* **Strategy selector** (`selector` in config.json, `app/selection/selector.py`, **mode `auto`**): the Holly-AI /
  Cryptohopper idea without the marketing. Every hour each symbol re-scores every strategy from its own record after
  costs: closed account trades (full weight), shadow counterfactuals rejected by quality-independent gates (half weight)
  and fresh per-strategy lab research at live parameters (test half, train quarter weight). The mean is shrunk toward a
  -0.05R prior over 20 pseudo-trades. ACTIVE (lower bound > 0 and >= 20 account trades) trades at 1.0x, PROBATION
  (positive estimate) 0.5x, UNPROVEN (< 30 evidence trades) 0.25x, BENCHED (negative estimate, or upper bound < 0) goes
  to shadow only (gate `SELECTOR`) and must reach +0.05R to return. A strategy that loses in one H1 regime across all
  symbols is benched in that regime only. Multipliers only ever REDUCE the strategy's `risk_multiplier`; entry rules,
  parameters, statuses and risk limits are never changed. State persists in `system_flags` (`selector:<symbol>`),
  changes are logged to `selector_history` and alerted, and every signal stores its selector state in
  `features.selector`. Dashboard -> Analysis -> "How much each rule is allowed to trade". `mode: "shadow"` computes and
  records without changing routing; `"off"` restores the previous behaviour exactly.
* **Demo sizing**: on the DEMO account `selector.unproven_multiplier` and `probation_multiplier` are 1.0, so only
  BENCHED strategies change size (to shadow). At $500 equity and 1% risk the minimum lot does not fit a 25% budget on
  any symbol for the candidate strategies, so graded sizing would have produced no account trades to learn from.
  Restore 0.25 / 0.5 before any real money.
* **Daily records** (`scripts\9_daily_records.bat`, Windows task **"Quantara daily records"**, every day 08:15 local
  = 00:15 UTC, runs late if the PC was off): `tools/daily_review.py` writes yesterday's review to
  `research/reports/daily/<day>.md` (+ `.json`) - account, every closed trade, open positions, why signals were
  refused, paper results of refused signals, selector state and changes, research verdicts, alerts from the log, the
  all-time record per strategy with 95% ranges, and a "Look at" list. `tools/backup_db.py` writes a verified
  `pg_dump` to `backups/` (newest 14 + first of each month kept; every dump holds all records). Run either by hand any
  time (`python tools/daily_review.py --day today`). Remove the task with
  `Unregister-ScheduledTask -TaskName 'Quantara daily records'`. Nothing in the database is ever deleted.
* **Auto-start**: Windows task **"Quantara bot at logon"** runs `scripts\start_bot_hidden.ps1` 1 minute after you log
  in. It starts the bot in the background (no window) unless one is already running, and notes each attempt in
  `logs\autostart.log`. Remove with `Unregister-ScheduledTask -TaskName 'Quantara bot at logon'`.
* **One kill switch for all symbols**: in multi-symbol mode every core now shares one kill switch. Before, a dashboard
  halt or resume was executed by whichever core drained the command, so "Stop all new trades" stopped one symbol.
* **Nightly per-strategy research**: `research_lab.research_all_nightly` (on, after 21:00 UTC) runs the per-strategy
  replay once a day instead of only at weekends. A cycle that does not run it now carries the previous result forward
  with its own `strategies_created_at`; before, the next 6-hourly cycle silently dropped it and every strategy except
  micro_scalper ran the week with no research evidence.
* **Profit lock**: when the stop cannot be tightened (broker minimum distance), the position is now KEPT on its existing
  broker stop and retried instead of being closed at market - set `execution.close_when_profit_lock_unprotectable` to
  restore the old behaviour.

## Evidence-first research and daily trend following (20 Oct 2026)
Why: a search of the published evidence (papers, independent replications, popular YouTube systems) found one family
with strong out-of-sample support - daily trend following / time-series momentum - and found that most intraday
edges, where they exist, are about the size of a retail CFD spread (the 2026 replication of the opening-range-
breakout paper: gross edge reproduced, net about zero on five index CFDs). Quantara's own lab could not tell an edge
from luck: of 195 rule x symbol x timeframe cells, 22 of the 46 that reached walk-forward were positive, which is
what noise gives.
* **Evidence grade** (`app/research/lab/stats.py`, `evidence.py`; `research_lab.evidence_enabled`): every lab cell and
  every per-strategy result now carries a `stats` block - the 95% range of R per trade (moving-block bootstrap), the
  per-trade Sharpe deflated by the number of cells the cycle LOOKED AT (Bailey & Lopez de Prado), the probability of
  backtest overfitting over the stop/target grid (CSCV), and a random-direction benchmark (the same entries and the
  same bracket with random sides). `evidence_grade` is **EDGE** only when the range is above zero, DSR >= 0.95 and
  random sides do worse at p < 0.05; otherwise NO_EDGE_SHOWN, NEGATIVE (range below zero) or THIN (< 20 trades). The
  existing `verdict` fields, the research gate and the selector are unchanged. Shown in the daily report ("Research
  evidence") and on Dashboard -> Analysis (candidate-rule and timeframe tables).
* **Longer history**: `days_m15` 730, `days_m5` 365, `days_h4` 3650 fetched straight from MT5; a long request MT5
  refuses ("Invalid params") is retried and then fetched in 90-day chunks. Bar spreads on the long history are scaled
  up by each symbol's median tick/bar spread ratio over the 60-day M1 overlap (`bar_spread_scale`). Exness history on
  20 Oct 2026: H4 from 2016 for gold/EURUSD/GBPUSD/GBPJPY, 2018 for BTC/ETH, 2022 for gas, Aug 2025 for the stock
  CFDs. The funnel and threshold sweep stay on `recent_days_m15` (90) because they replay every strategy bar by bar;
  the nightly per-strategy research now covers the full two years (expect it to take about an hour).
* **One exit rule everywhere**: the profit-lock ladder moved from `TradeManager.on_tick` to
  `app/alpha/exits.py::profit_lock_target` (same logic, proven equal to the old code on 5,000 random cases). The
  engine attaches the account's breakeven + lock levels to every plan's exit model (`with_execution_exits`), and
  `SimulatedTrade` - the backtester and the counterfactual tracker - now applies them, so research and "would have
  been" results judge the exit that actually trades. Before, both left breakeven and the lock out entirely. A strategy
  can opt out with `exit_model.profit_lock.enabled: false`. The lab reports `as_traded` vs `bracket` on identical
  entries; changing the live exit stays a separate decision made on that evidence.
* **`trend_daily_v1`** (`app/strategies/trend_daily.py`, research in `app/research/lab/trend.py`, status DEMO):
  Clenow's rule with his parameters, fixed in advance - a close at a 50-day high with EMA50 > EMA100 (short in
  reverse), stop 3 x ATR(100 days) trailed at the same fixed distance from the best price (chandelier), no
  breakeven, no profit lock, a 20R take-profit only because the broker requires one, one position per symbol
  (`allow_scale_in: false`). Built on UTC daily bars from the H4 store; the live class calls the same function the lab
  replays. Decade replay before going live (13 symbols, 372 trades, after spread, slippage and real swap): +0.084R a
  trade, 95% range -0.013 to +0.204, PSR 0.94, +0.136R better than random sides (p 0.005) - NO_EDGE_SHOWN, not
  NEGATIVE, so DEMO for incubation. Natural gas (-0.40R, swap 0.23R a trade) and XAUUSD247m (duplicate gold) are left
  out of its `symbols`. With the account's profit lock the same entries made gold +0.07R instead of +0.35R.
* **Strategy `symbols` are enforced**: the engine never read this field - every strategy ran on every symbol. A
  strategy now runs only where its list allows (`*` = all, unchanged for every other strategy).
* **Intraday momentum test** (`research_lab.intraday_momentum`, research only): the last 30 minutes before the close
  in the direction of the move since the previous close (Gao et al. 2018; Baltussen et al. 2021), on the five stock
  CFDs, time exit at the close (daylight saving from zoneinfo). **Exness stops its US stock CFDs at 15:45 New York**,
  not 16:00, so the lab reads each symbol's session close from its own bars (`session_close_ny`). First result,
  NVDAm over a year: -0.25R a trade on unseen data (range -0.49 to -0.03), and random sides lose as much - the spread
  is too large next to a 30-minute move. Graded like every other cell; no live class unless it earns EDGE.
* **Backtester uses each plan's own exit**: it used to trade every signal with the strategy's static `exit_model`,
  so per-trade values (the trend trail distance) were ignored and trend trades got an M15 trail. Live trades always
  carried the plan's model; now the backtest does too.
* **Startup needs less than hydration fetches**: a strategy can ask hydration for history (`lookbacks`) without
  requiring it to start (`min_history`). trend_daily_v1 fetches ~3,900 H4 bars, requires none and reports
  INSUFFICIENT_HISTORY when short, so a thin history cannot stop a symbol. Its daily decision is cached per completed
  day and H4 bar (the two-year backtest went from ~5 minutes a symbol to ~25 s).
* **Lab runtime**: with two years of M15, a year of M5 and the evidence statistics, a cycle takes roughly 5-6 minutes a
  symbol, and the nightly per-strategy research about 10 minutes a symbol (~2 hours for 13), in the low-priority
  research process.
* **Local LLM endpoint**: the owner's freellmapi router (`AI_BASE_URL` / `AI_API_KEY` in `.env`, OpenAI-compatible)
  routes prompts to free cloud models, so data sent there leaves the PC. Nothing uses it yet; if it is ever used it is
  advisory (e.g. a written summary of the daily review), never a trade gate - an LLM's opinion on past bars cannot be
  backtested honestly because it has seen those prices.
* **Demo balance**: the owner moved the Exness demo to $5,000 (Personal Area -> Set Balance) so daily-stop trades fit the
  minimum lot (at $500 a 3 x daily-ATR stop at 0.01 lot risked 3-38% per trade). The daily report shows a balance change
  that no closed trade explains as **Deposit / top-up**, never as profit. Gold still needs ~$19k for 1% risk at the
  minimum lot, so its trend signals are tracked as "would have been" results.

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

## Full-history replay report and per-symbol news calendar (20 Oct 2026)

- **Replay report** (`app/research/lab/replay.py`): every configured strategy over all the M15 history the broker
  gives (730 days asked; Exness has about 2 years for FX, metals, crypto and gas, 14 months for stock CFDs and 10
  for XAUUSD247), pooled across symbols. Per rule and per symbol it gives trades, win rate, average R with a
  block-bootstrap 95% range, total R, profit factor, max drawdown in R, worst losing run, best and worst month,
  R by quarter, the account curve, and the unseen last 30% kept separate. trend_daily_v1 comes from the lab's
  daily replay of all H4 history (pre-registered, so it has no held-out stretch).
- Produced by `python tools/backtest_report.py [--symbols ...] [--workers 3]` (MT5 data only, low priority,
  about 8 min per symbol per worker; it writes nothing to the DB) and refreshed by the nightly per-strategy lab
  run. Output: `research/results/backtest/<stamp>.json` and `latest.json`. `research_one(..., keep_trades=True)`
  carries the trades; the nightly run pops them before the lab stores its results.
- Shown on Analysis → "Every rule replayed over the last two years" (`/api/backtest/latest`, `?trades=<rule>` for
  one rule's trades) and in the daily review's "Full-history replay" section. The money column is an
  illustration at `risk.research_risk_fraction` of equity; R is the measure.
- **News calendar per symbol**: `events.currencies` now USD, EUR, GBP, JPY. Each core's `EventCalendar` is
  scoped to its symbol's currencies from the MT5 spec (`symbol_currencies`: EURUSD → EUR+USD, GBPJPY → GBP+JPY,
  NVDA/gas → USD, gold → XAU+USD), so a UK release stops GBP pairs only. Coverage (stale feed) is judged on the
  whole watched feed. The news straddle trades only `news_straddle.currencies` (default USD), so widening the
  blocking calendar did not widen the straddle.
- The bot reads no news headlines and uses no AI or LocalPilot: the calendar (FairEconomy weekly JSON, hourly)
  is its only news input.

## Audit fixes, one gold symbol, two new daily strategies (23 Oct 2026)
Why: the account-value chart showed about a day whatever range was picked, the database had grown to 10 GB in three
days, and an external audit listed further defects; each was checked against the code before it was changed.
* **Account-value chart** (`/api/equity`): the newest snapshot of each time bucket over the whole window, chosen in
  SQL. It read the newest 20,000 rows and filtered afterwards; 13 cores wrote the same account every minute, so that
  was 26 hours. Only the first portfolio core now records the account (one row a minute, not 13).
* **Dead dashboard connections**: a connection silent for `api.WS_IDLE_SEC` (150 s; the page pings every 20 s) is
  closed. asyncio drops writes to a connection whose peer has gone without raising, so the broadcaster kept writing
  to ~75 of them: 300 "socket.send() raised exception." warnings a second, 47 million app_logs rows (9.7 GB), and
  quantara.log rotating every 16 minutes. The warning is filtered; the page clears its ping timer on reconnect.
* **Correlation check** (gross mode): a pair whose correlation cannot be measured counts as fully correlated
  (`risk.missing_correlation_value`, as the diversified mode does) instead of refusing the trade. Stock CFDs give
  ~200 hourly returns in 30 days against the 400 required, so one open stock position cancelled 34 approved orders.
* **Waiting signals**: a signal queued for its spread re-checks risk state, the loss cooldown, health blockers,
  session and the news window on every pass (`TradingCore._entry_blocker`, shared with new signals).
* **Research gate**: verdicts that mean the lab could not judge (TOO_FEW_SIGNALS, NO_QUALIFYING_PARAMETERS,
  INSUFFICIENT_DATA, ERROR, ...) are evidence THIN / INCOMPLETE, so `gate_require_evidence` blocks them.
* **Research costs**: the per-strategy backtester and `tools/backtest_report.py` receive commission in money per
  lot (`_commission_money`); they were handed the fast simulator's price distance. Dormant while commission is 0.
* Strategy health reads the newest 1,000 trades; `_open_directions_by_symbol` reads `Position.side`; idle research
  rows use the table's columns; `limit=0` is zero rows; dashboard windows are filtered in SQL and query parameters
  are clamped; the selector's `r_sd` is filled (`trade_metrics` returns `sd_r`); the ML filter deploys the model and
  threshold that were judged on the unseen 30%; `strategies.json` keeps `disabled_reason` and `_comment` through
  dashboard rewrites; per-symbol commission in the cost model and on DRY_RUN closing deals; a bar that closed against
  a trade is ordered peak-first when a newly armed profit lock is tested (MT5's tester order); the session review's
  slippage takes the symbol from the trade and the point from its spec; positions with no stop show "no stop".
* **Look-ahead check** (`tools/lookahead_check.py`): it truncated by handing `frame_slice` a bar INDEX where it
  expects a TIMESTAMP, so every truncated run was empty and the check passed anything. It now cuts by index and
  fails when nothing was compared; `--symbol XAUUSDm` checks every traded rule on the lab's cached bars (all pass).
* `tools/bump_dashboard_version.py --help` prints help; it had written `?v=--help` into every page.
* **One gold symbol**: XAUUSD247m removed (same gold as XAUUSDm, 10 months of history; the two biggest losses of
  20-22 Oct were the same signal taken on both). The intraday strategies' `"*"` became the explicit list they
  traded, so adding a symbol no longer adds it to them.
* **trend_ensemble_v1** (DEMO, x0.5, the six symbols with 8-10 years of history): long while the 1-, 3-, 6- and
  12-month returns are all positive, short while all are negative (calendar-day lookbacks); trend_daily's exit.
  Replayed by the lab as `trend_ensemble`. Pre-registered replay: 537 trades, +0.069R (95% -0.013..+0.159),
  NO_EDGE_SHOWN - weaker than trend_daily on the same symbols (324 trades, +0.140R, 95% +0.029..+0.265, EDGE).
* **turn_of_month_v1** (DEMO, x0.5, US500m): long from the last trading day of the month through the third of the
  next (McConnell & Xu 2008), protective 3 x ATR(20) stop, time exit after 4 trading days of M15 bars. Replayed as
  `turn_of_month`; this account holds ~1 year of US500m, so the replay is 12 trades - a check of the calendar only.
