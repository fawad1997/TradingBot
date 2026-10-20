# Database setup (PostgreSQL + pgAdmin)

Three files, run in this order in **pgAdmin 4 → Query Tool**:

| Step | File | Connect the Query Tool to | What it does |
|---|---|---|---|
| 1 | `00a_create_role.sql` | database **postgres** | Creates the login `quantara_app`. **Change the password inside the file first** and use the same password in `.env`. |
| 2 | `00b_create_database.sql` | database **postgres** | Creates the database `quantara` (one statement on purpose: PostgreSQL refuses `CREATE DATABASE` inside a bigger script). |
| 3 | `01_schema.sql` | database **quantara** | Creates 30 tables, 9 views, indexes, seed rows and permissions. Runs as one transaction: all or nothing. Safe to run again. |

How to run a file in pgAdmin: right-click the database → **Query Tool** → folder icon (**Open File**) → choose the file → **Execute** (F5 or the ▶ button).

Alternative for steps 1–2: right-click **Login/Group Roles → Create**, and **Databases → Create → Database** (name `quantara`, owner `quantara_app`). Step 3 can also be done with `python -m app.main initdb` once `.env` is filled in.
After upgrading the package, re-run `01_schema.sql` (or `python -m app.main initdb`) so additive tables and indexes are created.

**Upgrading an existing database to v0.2:** run `02_migration_v0_2.sql` (adds `adaptive_param_history` and the view `v_spread_by_symbol_hour`; safe to re-run). Without it the bot still trades - adaptive history rows are best-effort and are simply not stored.

## Check it worked
Run in the Query Tool on `quantara`:
```sql
SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';  -- 30
SELECT count(*) FROM information_schema.views  WHERE table_schema='public';                              -- 9
SELECT * FROM system_flags;                                                                             -- kill_switch row
```

## Main tables
| Table | Contents |
|---|---|
| `accounts`, `symbols` | Broker accounts seen and contract-spec snapshots (queried, never assumed) |
| `feasibility_reports` | Phase 0 reports |
| `bars`, `ticks` | Closed bars; sampled live quotes (the empirical spread dataset) |
| `signals` | Every strategy signal with plan, edge, cost, regime, session, event state |
| `rejections`, `counterfactuals` | Every rejected signal and what would have happened |
| `candidate_evaluations` | Per-strategy candidate funnel, including no-setup and precondition outcomes |
| `trades`, `trade_events` | Trade state machine and every transition |
| `risk_reservations` | Atomic per-account risk held across broker submission and ambiguous outcomes |
| `execution_events` | Forensic ledger of every broker interaction (retcodes, slippage, latency) |
| `positions` | Broker position snapshots |
| `risk_states`, `account_snapshots` | Risk state machine history; equity curve |
| `system_health`, `app_logs` | Component health; warnings and errors |
| `config_versions`, `system_flags` | Audit trail of every change; durable kill switch and state |
| `research_lab_runs` | VectorBT / Backtrader research results per symbol (advisory) |
| `adaptive_param_history` | Every hourly adaptive-parameter refresh per symbol (volatility scale, spread cap, metrics) |
| `strategy_versions`, `parameters` | Strategy versions with evidence; parameter registry |
| `experiments`, `backtest_trades` | Research runs and their trades |
| `economic_events`, `daily_reports` | Event calendar; daily summaries |

## Useful views
`v_open_trades`, `v_closed_trades`, `v_strategy_performance`, `v_session_performance`, `v_gate_contribution` (which safety gate costs or saves money), `v_counterfactual_today`, `v_execution_quality`, `v_spread_by_session`.

## Backups
pgAdmin: right-click `quantara` → **Backup…** → Format *Custom* → choose a file. Restore with right-click → **Restore…**. Back up at least weekly, and always before upgrading the software.
