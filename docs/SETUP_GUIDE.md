# Quantara — Complete Setup and Operating Guide (v0.1)

This guide takes you from an empty Windows PC to a running system, in the safe order the specification asks for:

**Simulation → Feasibility report → Research → DRY_RUN → DEMO.**

Do the steps in order. Each step says what you should see when it worked.

---

## 0. Read this first

* **The strategy is not proven yet.** `pullback_v1` is a hypothesis (spec §9). The software is built to *test* it
  with real Exness costs and to say **NO TRADE** when the numbers are not good enough. A losing or "not enough
  evidence" result is a valid, useful outcome.
* **$100 on standard XAUUSD is probably too small.** On a standard account, 0.01 lot of gold = 1 ounce, so a
  $5 price move = $5 = 5% of $100. The system will not raise the risk just because the account is small; it will
  reject those trades as `POSITION_SIZE_INFEASIBLE`. The feasibility report (Step 8) tells you the exact numbers
  for *your* account. A Standard Cent account (XAUUSDc) is the usual way around this.
* **This release is demo-only for broker orders.** `REAL` is rejected by configuration validation, startup, and
  the execution engine. No confirmation phrase or strategy promotion can enable real-money execution.`
* This is software, not financial advice. You are responsible for every trade it places.

---

## 1. What you need

| Item | Notes |
|---|---|
| Windows 10 or 11, 64-bit | MetaTrader 5's Python library only works on Windows |
| Exness account | Start with a **demo** account. Note the login number, password and server name |
| MetaTrader 5 (Exness version) | Download from your Exness Personal Area |
| Python 3.12, 64-bit | From python.org (3.10–3.13 also work) |
| PostgreSQL 16 or 17 + pgAdmin 4 | From postgresql.org (the installer includes pgAdmin) |
| The package | Unzip to a short path without spaces, for example `C:\Quantara` |
| Internet, PC that stays on | For live running, set Windows power options so the PC never sleeps (or use a Windows VPS) |

---

## 2. Install and prepare MetaTrader 5

1. Install MT5 from Exness and log in to your **demo** account.
2. Turn on algorithmic trading:
   * Click the **Algo Trading** button in the top toolbar (it must be green).
   * **Tools → Options → Expert Advisors** → tick **Allow algorithmic trading**. Click OK.
3. Find your gold symbol: **View → Market Watch**, right-click → **Symbols**, search `XAU`. Depending on the
   account type it is `XAUUSD`, `XAUUSDm` or `XAUUSDc`. Make sure it is shown in Market Watch.
   Before running multi-asset mode, edit `config\config.json` and keep only candidate symbols available on your
   account, using their exact Market Watch names. Unsupported or insufficiently hydrated symbols are skipped at
   startup and reported in the log; the bot refuses to start if none can be initialized.
   The Overview dashboard shows whether each active symbol is open and, during the weekly closure, an estimated
   countdown to reopen. These times use a shared weekend schedule in UTC and may differ from broker-specific CFD
   sessions or holiday hours.
   The strategy registry in `config\strategies.json` configures a 1R pullback target and spread-covering breakeven
   stop activation at 0.5R. The runner allows at most one live or pending position per symbol, regardless of side.
   `execution.hard_profit_target_money` defaults to 1.00 in account currency and requests a market close when
   estimated net floating P/L reaches the target. Live broker P/L already reflects the bid/ask spread; configured
   round-trip commission and adverse slippage are also subtracted from the estimate. The broker-side take-profit and
   protective stop remain active. This is a tick-driven close request, not a guaranteed fill price; market movement,
   latency, and slippage can make realized profit lower than the target.
   Dynamic profit protection tracks peak estimated net P/L and persists it across restarts. After
   `execution.small_win_activation_money` (+0.20), a retrace to `execution.small_win_floor_money` (+0.05) requests a
   market close. At `execution.ambition_activation_money` (+0.90), the floor rises to
   `execution.ambition_floor_money` (+0.50) and then ratchets upward with the peak, maintaining a 0.40 account-unit
   gap; the +1.00 hard target closes first. The bot also tries to move its broker-side stop to the current floor
   after estimated costs. A failed stop change triggers a market-close fallback only when a fresh position check
   confirms estimated net P/L still meets the floor; otherwise the existing protective stop remains. Stop changes
   clear broker stop/freeze levels plus `execution.profit_lock_stop_buffer_points` (3 symbol points by default).
   These thresholds are estimates, not guarantees: gaps, market movement, latency, costs, and slippage can produce a
   lower realized result or a loss.
4. Download history: open **M1**, **M15**, **H1**, and **H4** charts for that symbol and press the **Home** key /
   scroll left for a while. The micro-scalper requires at least 40 closed H4 bars for its causal macro channel;
   the bot validates history availability at startup.
5. Write down: login number, password, server name (e.g. `Exness-MT5Trial7`), and the path of `terminal64.exe`
   (right-click the MT5 shortcut → *Open file location*).

**Worked when:** you see live prices for gold and the Algo Trading button is green.

---

## 3. Install Python

1. Download **Python 3.12 (64-bit) Windows installer** from python.org.
2. On the first installer screen **tick "Add python.exe to PATH"**, then *Install Now*.
3. Open *Command Prompt* and type `python --version`. You should see `Python 3.12.x`.

---

## 4. Install PostgreSQL and pgAdmin

1. Run the PostgreSQL installer. Keep the default components (Server, pgAdmin 4, Command Line Tools).
2. Choose a password for the `postgres` superuser — **write it down**. Keep port **5432**.
3. Finish. You do not need Stack Builder.

**Worked when:** pgAdmin 4 opens and shows *Servers → PostgreSQL* after you enter the password.

---

## 5. Create the database in pgAdmin (the "import" step)

The files are in the `database` folder. Full details: `database/README_DATABASE.md`.

1. Open `database\00a_create_role.sql` in Notepad and **change** `ChangeMe_Strong_Password_123` to your own
   strong password. Save.
2. In pgAdmin: *Servers → PostgreSQL → Databases → postgres* → right-click → **Query Tool**.
3. Click the folder icon (**Open File**) → choose `00a_create_role.sql` → press **F5** (Execute).
   You should see `DO` / *Query returned successfully*.
4. Same Query Tool: open `00b_create_database.sql` → **F5**. You should see `CREATE DATABASE`.
5. Right-click *Databases* → **Refresh**. You now see `quantara`. Right-click **quantara → Query Tool**.
6. Open `01_schema.sql` → **F5**. It finishes with `DO`.
7. Check: paste and run
   ```sql
   SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';
   ```
   The answer must be **28**.

---

## 6. Install the software

1. Unzip the package to `C:\Quantara`.
2. Double-click **`scripts\1_install.bat`**. It creates a private Python environment (`.venv`) and installs the
   packages. At the end it opens `.env` in Notepad.
3. Fill in `.env`:
   * `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` — from Step 2.
   * `MT5_TERMINAL_PATH` — full path to `terminal64.exe`.
   * `DATABASE_URL` — replace the password with the one you put in `00a_create_role.sql`:
     `postgresql://quantara_app:YOUR_PASSWORD@localhost:5432/quantara`
   * `DASHBOARD_TOKEN` — any long random text (you type it once in the browser to use the controls).
   * `QUANTARA_CONFIRM_REAL` is not used by this release; do not rely on it to enable live trading.
4. Save and close Notepad.

`.env` contains passwords. Never send it to anyone and never put it in a shared folder.

---

## 7. Check everything

Double-click **`scripts\2_check.bat`**. Keep MT5 open.

**Worked when:** the last line says `ALL CHECKS PASSED`, and you see your account number, server, `DEMO`,
equity, and the gold symbol.

| Message | Fix |
|---|---|
| `MT5 initialize failed` | MT5 not installed at `MT5_TERMINAL_PATH`, or wrong login/password/server in `.env` |
| `Algo Trading is disabled` | Press the **Algo Trading** button in MT5 (Step 2) |
| `PostgreSQL: password authentication failed` | Password in `DATABASE_URL` is different from `00a_create_role.sql` |
| `schema missing` | Run `01_schema.sql` in pgAdmin on the **quantara** database (Step 5.6) |
| `None of [...] found` | Your gold symbol has another name: add it to `broker.symbol_candidates` in `config\config.json` |
| `MetaTrader5: No module named` | You are not on 64-bit Windows Python, or the install step failed — run `1_install.bat` again |

---

## 8. First look: the simulator (no risk)

Double-click **`scripts\6_run_simulation.bat`**. A browser opens at <http://127.0.0.1:8787>.

This runs the *real* trading core against a **simulated** market and broker with $10,000 of pretend money, so
you can learn the dashboard. The simulated profits mean nothing about real gold.

**The four pages**

* **Overview** — five-second health check: status pills (SYSTEM, MT5, BROKER, DATA, DATABASE, EXECUTION,
  RECON, STRATEGY, RISK), account cards, equity curve, risk state, strategies, health bars, open positions,
  and today's counterfactual summary (which safety gate saved money and which one cost money).
* **Real-Time** — replaces watching MT5: balance, equity, floating and realized P/L, spread, ping, live
  positions, the market panel (bid/ask, spread percentile, ATR, session, regime, event state), **"Why is (or
  isn't) the system trading?"**, the live execution feed, and today's completed trades.
* **Logs & Audit** — Execution, Errors, Signals, Rejected signals (with the hypothetical result), Counterfactuals,
  Risk, Reconciliation, System health, Research, Trades, Config history.
* **Configuration** — environment (read-only), strategies (enable/disable), runtime parameters (validated,
  versioned, every change needs a reason), kill switch, close-all, reconcile, reload event calendar.

**The red button (top right) = EMERGENCY · HALT NEW TRADES.** It stops new signals and new orders immediately.
Existing positions keep their stop loss at the broker. Closing positions is a separate button on the
Configuration page. To resume you must give a reason.

Close the window (or press Ctrl+C) to stop.

---

## 9. Phase 0 — the feasibility report

Keep MT5 open (demo is fine — it reads the same contract specification). Double-click
**`scripts\3_feasibility_report.bat`**. It prints two reports — one for $100, one for your actual equity — and
saves them in `research\reports` and the `feasibility_reports` table.

It answers two separate questions:

1. **Can this capital trade gold safely?** — minimum lot, money per $1 move, margin, risk of a typical stop at the
   minimum lot compared with the per-trade ceiling (`risk.max_risk_fraction_per_trade`, default 1.5%).
2. **Can this capital meaningfully test the strategy?** — is the minimum-lot risk inside the research budget
   (1%), and how big is the spread compared to a typical stop?

If the answer is **NO**: that is normal for $100 on standard gold. Your options are (a) keep researching with
backtests, SHADOW and DRY_RUN; (b) open an Exness Standard Cent account and run the report there (XAUUSDc);
(c) add capital. The system will never quietly increase risk to make a small account "work".

---

## 10. Collect your own spread data (recommended)

Double-click **`scripts\4_collect_spreads.bat`** and let it run for several hours across different sessions
(Asia, London, New York). It stores about one quote per second in the `ticks` table. In pgAdmin,
`SELECT * FROM v_spread_by_session;` shows the spread distribution per session. The live spread gate uses
these percentiles.

---

## 11. Phase 2 — research: does the strategy have an edge?

Double-click **`scripts\5_research.bat`** (MT5 open). It:

1. exports M15 and H1 history from MT5 to `research\datasets` (and the contract spec);
2. imports the economic calendar;
3. runs **backtest** → **costs** → **exits** → **sessions** → **walkforward** for `pullback_v1`.

Reports appear in `research\reports` as `.md` (read these), `.json` and `_trades.csv`. They are also stored in the
`experiments` table and listed on the dashboard (*Logs & Audit → Research*).

**How the backtest stays honest:** the strategy code is exactly the live code; signals are formed on a closed
bar and filled at the *next* bar's open on the correct side (ask for buys, bid for sells) with that bar's real
spread plus slippage; stops trigger on the correct side; if a stop and a target are hit in the same bar the stop
is assumed first; H1 data is only visible after the H1 bar has closed; commission and swap are charged.

**How to read the results**

| Report | Key question | Good sign |
|---|---|---|
| Backtest | Positive expectancy after costs? | `expectancy_r` > 0 with **hundreds** of trades, profit factor > 1.1 |
| Cost stress | Does the edge survive worse spreads and slippage? | Still positive at spread ×1.5, slippage +5 |
| Exit research | Which exit (A–G) keeps more of the move? | Compare `expectancy_r`, `mfe_captured_pct`, giveback; p-value < 0.05 |
| Session research | When does it work? (H1–H5) | Big, stable differences only — otherwise keep all sessions |
| Walk-forward | Does it work on data it was **not** tuned on? | OOS expectancy > 0 over ≥ 100 trades, neighbours stable, DSR ≥ 0.9 |

**Evidence levels:** INSUFFICIENT (<30 trades) → PRELIMINARY → PROMISING → ROBUST. VALIDATED is only given
by you after demo and micro-live. INVALIDATED means ≥ 100 trades with zero or negative expectancy: **stop and
rethink the hypothesis — do not tweak parameters until it looks good.** That is curve-fitting (spec §29).

**Better spread history (optional):** Exness offers downloadable historical tick data. Convert a tick file to M15
bars with measured spreads:
```
.venv\Scripts\python -m app.main build-bars --ticks C:\path\to\exness_ticks.csv --out research\datasets\XAUUSD_M15.csv
```
then run the research commands again.

---

## 12. DRY_RUN — live data, paper trades

`config\config.json` already says `"environment": "DRY_RUN"`.

Double-click **`scripts\7_run_bot.bat`**. The startup sequence (watch the console) is:
`CONNECTING → MT5_CONNECTED → ACCOUNT_VALIDATION → SYMBOL_VALIDATION → DATA_HYDRATION →
INDICATOR_INITIALIZATION → STRUCTURE_INITIALIZATION → BROKER_STATE_RECONCILIATION → STRATEGY_VALIDATION →
RISK_VALIDATION → READY → ACTIVE`. No trading happens before ACTIVE.

In DRY_RUN the bot uses live Exness prices but fills orders in a **paper broker** (starting balance
`paper.starting_balance`, default 100). Nothing is sent to MT5. Everything else is real: signals, gates,
counterfactuals, reconciliation, the database, the dashboard.

Run it for at least **two weeks**. Check the *Rejected signals* and *Counterfactuals* tabs: they show which gates
protect you and which cost you opportunities.

**SHADOW** (`"environment": "SHADOW"`) is even stricter: no fills at all, every approved signal is only tracked as a
counterfactual. Use it to compare strategies live (Mean Reversion v1 always runs in shadow).

---

## 13. DEMO — real orders on the demo account

1. Stop the bot (Ctrl+C in its window).
2. Edit `config\config.json`: `"environment": "DEMO"`. Make sure MT5 is logged into the **demo** account.
3. Start `7_run_bot.bat`.

The bot refuses to start in DEMO if the MT5 account is a real account. Orders carry magic number `20260101` and a
comment like `ATA3F9C...` — you will see them in MT5. Every order is checked with `order_check`, the intent is
saved to the database **before** sending, and the fill is confirmed from the broker's positions.

If you close or change a bot position by hand in MT5, the bot notices within about 30 seconds and records it
(`USER_MODIFIED` / closed by `CLIENT`). Manual trades with other magic numbers are counted for risk but never
managed.

Run DEMO for **at least a month / 50+ trades** and compare the live results (Overview → Strategies, or
`SELECT * FROM v_strategy_performance;`) with the walk-forward numbers.

---

## 14. Strategy promotion and release policy

Promotion is a human decision about an **evidence package** (spec §30). The current release supports this ladder:
`RESEARCH_ONLY → SHADOW → DEMO → MICRO_LIVE → VALIDATED`.

`MICRO_LIVE` and `VALIDATED` are strategy evidence/status labels only in this release; they do not authorize REAL
orders. REAL execution is prohibited regardless of strategy status, environment edits, or the former
`QUANTARA_CONFIRM_REAL` phrase. Configurations requesting `"environment": "REAL"` fail validation before the broker
connects, and the execution engine also rejects REAL as a defense-in-depth check.

1. For DEMO promotion, read the walk-forward report. At the bottom it prints the exact promote command, for example:
   ```
   .venv\Scripts\python -m app.main promote --strategy pullback_v1 --experiment <id> --to MICRO_LIVE --reason "OOS +0.18R over 240 trades, demo matched"
   ```
   This writes the evidence statistics into `config\strategies.json` and the audit log. (`--override` exists, is
   recorded as an override, and is not recommended.)
2. Before expanding DEMO exposure, confirm all of this:
   * [ ] Feasibility report says the account **can trade safely**.
   * [ ] Walk-forward evidence is ROBUST; cost stress still positive.
   * [ ] DEMO results are in line with the research (not much worse).
   * [ ] `scripts\run_tests.bat` passes on your PC.
   * [ ] Discord alerts work (Section 16).
   * [ ] You have tested the kill switch and close-all on DEMO.
   * [ ] Risk ceilings in `config.json` are what you want (they are maximums, not targets).
3. Keep MT5 logged into a **demo** account and retain `"environment": "DEMO"` (or `DRY_RUN`) in `config.json`.
4. Start `7_run_bot.bat`; startup verifies the broker account is DEMO before orders are allowed.

---

## 15. Daily operation

* **Start:** open MT5 (Algo Trading green) → run `7_run_bot.bat` → dashboard opens.
* **Stop:** press Ctrl+C in the bot window. Broker stop losses stay in place. On the next start the bot
  reconciles with the broker before doing anything.
* **Five-second check:** Overview — every pill green, risk NORMAL, equity curve sensible.
* **If something looks wrong:** press **HALT NEW TRADES**. Then look at *Logs & Audit → Errors / Reconciliation*.
* **Restarts are safe:** a crash or power cut in the middle of an order is reconciled from the broker on restart;
  the same signal is never sent twice (this is tested).
* **Keep the PC awake:** Windows *Settings → System → Power* → Sleep: *Never*. Pause Windows Update restarts
  during trading hours, or run on a Windows VPS near the Exness servers.
* **Back up the database weekly:** pgAdmin → right-click `quantara` → *Backup…*.

**Automatic protections you will see**

| Event | What the system does |
|---|---|
| Daily loss limit reached (default 5%) | Risk state `NO_NEW_TRADES` until the next UTC day |
| Drawdown 10% / 15% / 20% / 30% from peak | Risk reduced ×0.5 / ×0.25 / no new trades / EMERGENCY + kill switch |
| MT5 disconnected, stale prices, spread shock | No new trades until healthy |
| Cannot read broker positions | Portfolio frozen, alert sent |
| Database write fails | Writes go to `logs\critical_journal.jsonl`, new trades halted; you resume manually |
| Order result unknown (timeout) | Never resent; reconciled from the broker |
| Order is awaiting reconciliation | Reserve risk using its requested entry, stop, and size; unknown risk blocks new entries |
| Broker did not attach the stop loss | Bot attaches it; if impossible it closes the position |
| Strategy live results statistically negative | Strategy health `RESTRICTED`, alert |

Strategy health is evaluated independently for each strategy version using that version's closed trades.
Publishing a new version starts it with an empty live sample; this is not evidence of profitability and
does not clear or alter the previous version's health record. If the closed-trade history cannot be read,
the version is restricted rather than treated as a clean sample.

---

## 16. Discord alerts (optional, recommended for live)

Telegram was removed: it is blocked in Pakistan and never delivered. Discord is the default channel; `ntfy`,
`email` and a generic `webhook` are also supported (see `app/notifications/channels.py`).

1. In Discord, open the channel you want the alerts in → **Edit Channel** → **Integrations** → **Webhooks** →
   **New Webhook** → **Copy Webhook URL**.
2. Put that URL in `.env` as `NOTIFY_URL`. **Treat it as a secret** — anyone holding it can post into your channel.
3. Set `"alerts": {"enabled": true, "channel": "discord"}` in `config.json`. Restart the bot.
4. Verify before you rely on it: `python tools\test_alert.py`. It uses the same config, secrets and notifier the
   bot uses, so a pass here means the bot's alerts will work too.

You get "wake me up" alerts (broker disconnected, emergency risk state, kill switch, unexpected position,
reconciliation problems, strategy restricted, database failure) plus one daily report at
`alerts.daily_summary_hour_utc`. Per-trade OPEN/CLOSE messages are a separate switch:
`alerts.trade_alerts.enabled`.

---

## 17. Economic calendar

`config\economic_events.csv` contains the FOMC dates from the Federal Reserve website. **Add** the other
high-impact USD releases you care about (CPI, PCE, NFP, unemployment, GDP, PMI) with their UTC times from the
official calendars (bls.gov, bea.gov) or your broker's calendar. Then:
```
.venv\Scripts\python -m app.main import-events config\economic_events.csv
```
and press *Reload event calendar* on the Configuration page. With `events.mode = "block"`, new trades are not
opened from 30 minutes before to 15 minutes after an event (configurable). If the calendar is missing or older
than 10 days the state is `UNKNOWN`: allowed in DRY_RUN/DEMO. REAL order execution is unavailable in this release.

---

## 18. Configuration reference (`config\config.json`)

| Setting | Default | Meaning |
|---|---|---|
| `environment` | `DRY_RUN` | DRY_RUN / SHADOW / DEMO (REAL is prohibited in this release) |
| `broker.symbol_candidates` | Symbols listed in `config\config.json` | Each available symbol is scanned; remove unavailable symbols or use their exact Market Watch names |
| `broker.magic_number` | 20260101 | Identifies the bot's orders in MT5 |
| `broker.broker_utc_offset_hours` | null (auto) | Exness servers run on UTC; leave null |
| `spread_gate.max_spread_points` | 300 | Absolute spread ceiling in each symbol's broker points |
| `spread_gate.max_spread_percentile` | 0.95 | Reject / wait if spread is above this percentile in that symbol's session history |
| `execution.max_signal_age_sec` | 120 | Older signals are never executed |
| `execution.max_entry_drift_frac_of_stop` | 0.25 | Cancel if price ran away more than 25% of the stop |
| `execution.protection_safety_buffer_points` | 5 | Extra broker-point buffer beyond stop/freeze levels and live spread |
| `execution.sl_modification_cooldown_sec` | 5 | Per-position SL modification cooldown (5–10 seconds) |
| `cost.commission_per_lot_round_trip` | 0 | **Set this for Raw Spread / Zero accounts** |
| `cost.max_cost_fraction_of_risk` | 0.25 | Costs may not eat more than 25% of 1R |
| `risk.max_risk_fraction_per_trade` | 0.015 | Hard ceiling per trade (not a target) |
| `risk.research_risk_fraction` | 0.01 | Risk used for unvalidated strategies in DRY_RUN / DEMO |
| `risk.kelly_fraction` | 0.25 | Fraction of (shrunk) Kelly for validated strategies |
| `risk.max_total_open_risk_fraction` | 0.10 | Portfolio-wide open plus reserved risk ceiling as a fraction of equity |
| `risk.correlation_lookback_days` | 30 | Rolling H1 return window used to identify correlated symbols |
| `risk.correlation_min_samples` | 400 | Minimum synchronized H1 returns; insufficient history blocks affected exposure |
| `risk.correlation_threshold` | 0.70 | Absolute Pearson correlation threshold for a shared risk cluster |
| `risk.correlation_group_fraction` | 0.50 | Correlated cluster cap as a share of the total portfolio risk cap (5% equity by default) |
| `risk.max_concurrent_positions` | 10 | Includes manual positions on the account |
| `risk.daily_loss_limit_fraction` | 0.05 | Stop new trades for the day |
| `risk.dd_*` | 0.10/0.15/0.20/0.30 | Drawdown thresholds for the risk states |
| `events.mode` | block | `block` or `observe` around high-impact events |
| `paper.starting_balance` | 100 | DRY_RUN paper account |
| `dashboard.port` | 8787 | Always bound to 127.0.0.1 |

Strategy parameters live in `config\strategies.json`. Every parameter has a minimum, maximum, step and economic
hypothesis; the bot refuses to start if a value is out of range. Change parameters only through research
(edit `research_grid`, run walk-forward), never by guessing.

### Micro-scalper candidate rules

The micro-scalper first requires a causal 40-closed-bar H4 linear-regression channel: buys need an uptrend and price
in its lower 30%, sells a downtrend and price in its upper 30%. Channel boundaries are the 10th and 90th percentiles
of regression residuals. Its closed M1 trigger must be a bullish/bearish pin bar or engulfing candle; there is no M1
momentum-only fallback. The latest closed M15 candle may be neutral but must not point against the candidate.
Trigger-candle tick volume must reach the median of the previous 20 closed M1 bars.

A candidate that passes these rules is then revalidated by the normal risk, cost, spread, health, portfolio and
execution gates plus expiry checks before any order is sent. Historical backtests run the candle, M15-direction,
H4-channel and volume gates exactly as the live path does.

Approved signals pass through an execution dispatcher keyed by venue category and strategy ID. The only
registered live execution category is `MT5`; its generic route currently delegates every configured strategy to
the existing risk-checked MT5 execution engine. Unknown categories are rejected. Binance, Alpaca, and other
venues are not implemented or enabled.

To run the opt-in **research-only** grid simulation, export M1 history and run
`python -m app.main backtest --strategy micro_scalper --m1 research\datasets\XAUUSD_M1.csv --research-grid`.
The model requires a pin-bar or engulfing trigger, adds up to two equal-size layers one M1 ATR apart, places a
shared stop one M1 ATR beyond the third layer, and limits planned basket stop risk to 1% of equity. It estimates
margin using 100:1 leverage and a 50% equity margin budget, and closes at $1 estimated net basket profit after
commission and slippage. These are research assumptions, not a live execution feature, guarantee, or validated
trading edge; check the broker's actual margin, spread, and costs separately.

### Broker stop and modification safeguards

Before a market entry or protective SL/TP update, the bot refreshes the symbol's broker stop/freeze metadata and
checks a live quote. Protective levels must clear `stops_level + freeze_level + current spread +
protection_safety_buffer_points` (5 points by default). Levels that are too close are rejected locally and
recorded in execution events instead of being sent to the broker. The same checks apply when restoring a missing
broker-side bracket after a fill.

SL changes are limited to one attempt per position every `sl_modification_cooldown_sec` (5 seconds by default,
configurable from 5 to 10 seconds). The cooldown also follows failed broker modifications, so repeated ticks do
not resend a rejected request. After a close returns `FROZEN` (retcode 10029), a retry for that position is
deferred for the same cooldown; the bot retains the existing broker-side protection and records the deferral.
Retcode 10011 is classified as a generic broker `ERROR`; invalid stops are specifically retcode 10016, so use the
returned text/comment rather than treating every 10011 response as a stop-distance rejection.

---

## 19. Troubleshooting

| Symptom | What to do |
|---|---|
| Startup stops at `DATA_HYDRATION` "only N closed bars" | Open the gold M15 and H1 charts in MT5 and scroll back to download history (H1 needs about 1,800 bars) |
| Startup fails: "environment DEMO but the MT5 account is REAL" | MT5 is logged into a real account — log into demo or change environment |
| System status `NOT_TRADING`, blocker `MARKET_CLOSED` | Weekend or daily break — normal |
| Every signal rejected `POSITION_SIZE_INFEASIBLE` | Account too small for the stop sizes — see Step 9 (this is the system protecting you) |
| Every signal rejected `SPREAD_PERCENTILE` | Spreads are wide right now; or collect more spread data (Step 10) |
| Configuration rejected with `REAL execution is disabled` | This release cannot place real-money orders; use DEMO or DRY_RUN |
| Dashboard asks for a token repeatedly | Use exactly the `DASHBOARD_TOKEN` from `.env` |
| `logs\critical_journal.jsonl` exists | The database failed at some point. Check PostgreSQL is running, then resume from the dashboard |
| Positions in MT5 not shown as managed | They have another magic number (manual trades) — counted for risk, not managed |

Logs: `logs\quantara.log` (rotating). Errors also appear on *Logs & Audit → Errors*.

---

## 20. What v0.1 deliberately does not include

Following the spec's build order (§71), these are switched off until the core has proven an edge:
AI analysts and the bull/bear debate (`research.ai_enabled`), machine-learning models, volume profile / liquidity
maps / order-flow features (tick volume on CFDs is not exchange volume), LIMIT-order routing
(`execution.allow_limit_orders`), and multi-symbol portfolios. The architecture has the places for them
(broker interface, strategy registry, router, promotion pipeline), so they can be added without rewriting the
core.
