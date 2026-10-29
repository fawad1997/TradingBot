-- =====================================================================
--  Quantara  -  PostgreSQL schema v0.1
--  Run this file in pgAdmin's Query Tool while connected to the
--  database "quantara" (see database/README_DATABASE.md).
--  The whole script runs as one transaction: it either fully succeeds
--  or changes nothing. Safe to re-run (IF NOT EXISTS everywhere).
-- =====================================================================

CREATE TABLE IF NOT EXISTS schema_migrations (
    version      text PRIMARY KEY,
    description  text,
    applied_at   timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- broker / account
CREATE TABLE IF NOT EXISTS accounts (
    id           bigserial PRIMARY KEY,
    login        bigint NOT NULL,
    server       text NOT NULL,
    company      text,
    currency     text,
    leverage     integer,
    trade_mode   text,                     -- DEMO / REAL / CONTEST / PAPER
    hedging      boolean,
    environment  text,                     -- DRY_RUN / SHADOW / DEMO / REAL
    first_seen   timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz NOT NULL DEFAULT now(),
    raw          jsonb,
    UNIQUE (login, server)
);
COMMENT ON TABLE accounts IS 'Broker accounts the system has connected to';

CREATE TABLE IF NOT EXISTS symbols (
    id              bigserial PRIMARY KEY,
    captured_at     timestamptz NOT NULL DEFAULT now(),
    account_login   bigint,
    symbol          text NOT NULL,
    digits          integer,
    point           double precision,
    tick_size       double precision,
    tick_value      double precision,
    contract_size   double precision,
    volume_min      double precision,
    volume_step     double precision,
    volume_max      double precision,
    stops_level     integer,
    freeze_level    integer,
    filling_modes   integer,
    execution_mode  text,
    trade_mode      text,
    swap_long       double precision,
    swap_short      double precision,
    currency_profit text,
    spec            jsonb
);
COMMENT ON TABLE symbols IS 'Snapshots of broker contract specifications (queried, never assumed)';
CREATE INDEX IF NOT EXISTS symbols_symbol_idx ON symbols (symbol, captured_at DESC);

CREATE TABLE IF NOT EXISTS feasibility_reports (
    id                     uuid PRIMARY KEY,
    created_at             timestamptz NOT NULL DEFAULT now(),
    account_login          bigint,
    symbol                 text,
    equity                 double precision,
    can_trade_safely       text,
    can_test_meaningfully  text,
    report                 jsonb,
    markdown               text
);
COMMENT ON TABLE feasibility_reports IS 'Phase 0: can this capital trade / test this symbol?';

-- ---------------------------------------------------------------- market data
CREATE TABLE IF NOT EXISTS bars (
    symbol        text NOT NULL,
    timeframe     text NOT NULL,
    time          timestamptz NOT NULL,      -- bar OPEN time, UTC
    open          double precision NOT NULL,
    high          double precision NOT NULL,
    low           double precision NOT NULL,
    close         double precision NOT NULL,
    tick_volume   bigint,                    -- CFD tick volume, NOT exchange volume
    spread_points integer,
    real_volume   bigint,
    source        text NOT NULL DEFAULT 'MT5',
    PRIMARY KEY (symbol, timeframe, time)
);

CREATE TABLE IF NOT EXISTS ticks (
    id               bigserial PRIMARY KEY,
    symbol           text NOT NULL,
    time             timestamptz NOT NULL,
    time_msc         bigint,
    bid              double precision,
    ask              double precision,
    spread_points    double precision,
    spread_pctile    double precision,
    session          text,
    tick_rate        double precision,
    volatility_state text,
    event_state      text
);
COMMENT ON TABLE ticks IS 'Sampled live quotes (about 1/s): the empirical spread and tick-rate dataset';
CREATE INDEX IF NOT EXISTS ticks_symbol_time_idx ON ticks (symbol, time);
CREATE INDEX IF NOT EXISTS ticks_time_brin ON ticks USING brin (time);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id          uuid PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    symbol      text,
    data        jsonb
);

CREATE TABLE IF NOT EXISTS economic_events (
    id          bigserial PRIMARY KEY,
    event_time  timestamptz NOT NULL,
    currency    text NOT NULL,
    impact      text NOT NULL,
    title       text NOT NULL,
    source      text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_time, currency, title)
);

-- ---------------------------------------------------------------- strategies / governance
CREATE TABLE IF NOT EXISTS strategy_versions (
    id               bigserial PRIMARY KEY,
    strategy_id      text NOT NULL,
    version          text NOT NULL,
    params_hash      text NOT NULL,
    params           jsonb,
    exit_model       jsonb,
    status           text,
    evidence_level   text,
    stats            jsonb,
    experiment_id    uuid,
    approved_by      text,
    approval_reason  text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (strategy_id, version, params_hash)
);

CREATE TABLE IF NOT EXISTS parameters (
    id                   bigserial PRIMARY KEY,
    strategy_id          text NOT NULL,
    name                 text NOT NULL,
    value                jsonb,
    minimum              double precision,
    maximum              double precision,
    step                 double precision,
    economic_hypothesis  text,
    experiment_id        uuid,
    training_period      text,
    validation_period    text,
    oos_period           text,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (strategy_id, name)
);
COMMENT ON TABLE parameters IS 'Parameter registry: every parameter has bounds, a step and an economic hypothesis';

CREATE TABLE IF NOT EXISTS config_versions (
    id              bigserial PRIMARY KEY,
    created_at      timestamptz NOT NULL DEFAULT now(),
    config_version  integer,
    actor           text,
    action          text,
    path            text,
    old_value       jsonb,
    new_value       jsonb,
    reason          text,
    full_config     jsonb
);

CREATE TABLE IF NOT EXISTS system_flags (
    key         text PRIMARY KEY,
    value       jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text
);
COMMENT ON TABLE system_flags IS 'Durable switches (kill switch, risk tracker, paper broker state)';

-- ---------------------------------------------------------------- signals / decisions
CREATE TABLE IF NOT EXISTS signals (
    id                  uuid PRIMARY KEY,
    created_at          timestamptz NOT NULL DEFAULT now(),
    strategy_id         text NOT NULL,
    strategy_version    text,
    symbol              text,
    environment         text,
    direction           text,
    entry               double precision,
    sl                  double precision,
    tp                  double precision,
    exit_plan           jsonb,
    quality             double precision,
    confidence          double precision,
    edge_r              double precision,
    cost_r              double precision,
    regime              text,
    session             text,
    event_state         text,
    valid_until         timestamptz,
    market_snapshot_id  uuid,
    setup_key           text,
    status              text,       -- CANDIDATE / APPROVED / REJECTED / SHADOW / EXECUTED / EXPIRED / WAITING
    shadow              boolean NOT NULL DEFAULT false,
    features            jsonb
);
CREATE INDEX IF NOT EXISTS signals_created_idx ON signals (created_at DESC);
CREATE INDEX IF NOT EXISTS signals_setup_idx ON signals (strategy_id, setup_key);

CREATE TABLE IF NOT EXISTS rejections (
    id              uuid PRIMARY KEY,
    created_at      timestamptz NOT NULL DEFAULT now(),
    signal_id       uuid REFERENCES signals(id),
    strategy_id     text,
    symbol          text,
    direction       text,
    gate            text,
    reason          text,
    proposed_entry  double precision,
    proposed_sl     double precision,
    proposed_tp     double precision,
    proposed_size   double precision,
    spread_points   double precision,
    volatility      text,
    regime          text,
    session         text,
    news_state      text,
    details         jsonb
);
CREATE INDEX IF NOT EXISTS rejections_created_idx ON rejections (created_at DESC);
CREATE INDEX IF NOT EXISTS rejections_gate_idx ON rejections (gate);

CREATE TABLE IF NOT EXISTS candidate_evaluations (
    id              uuid PRIMARY KEY,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    strategy_id     text NOT NULL,
    symbol          text NOT NULL,
    environment     text NOT NULL,
    bar_time        bigint,
    stage           text NOT NULL,
    outcome         text NOT NULL,
    gate            text,
    reason          text,
    signal_id       uuid REFERENCES signals(id),
    details         jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS candidate_evaluations_created_idx ON candidate_evaluations (created_at DESC);
CREATE INDEX IF NOT EXISTS candidate_evaluations_group_idx
    ON candidate_evaluations (symbol, strategy_id, stage, outcome, created_at DESC);

CREATE TABLE IF NOT EXISTS counterfactuals (
    id                  uuid PRIMARY KEY,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    signal_id           uuid REFERENCES signals(id),
    rejection_id        uuid,
    strategy_id         text,
    symbol              text,
    direction           text,
    gate                text,
    reason              text,
    status              text NOT NULL DEFAULT 'OPEN',   -- OPEN / RESOLVED
    hypothetical_fill   double precision,
    sl                  double precision,
    tp                  double precision,
    exit_plan           jsonb,
    proposed_size       double precision,
    market_snapshot_id  uuid,
    regime              text,
    session             text,
    news_state          text,
    spread_points       double precision,
    mfe_r               double precision,
    mae_r               double precision,
    hypothetical_exit   double precision,
    exit_reason         text,
    hypothetical_pnl    double precision,
    hypothetical_r      double precision,
    bars_held           integer,
    resolved_at         timestamptz
);
CREATE INDEX IF NOT EXISTS counterfactuals_status_idx ON counterfactuals (status);
CREATE INDEX IF NOT EXISTS counterfactuals_created_idx ON counterfactuals (created_at DESC);

-- ---------------------------------------------------------------- trades / execution
CREATE TABLE IF NOT EXISTS trades (
    id                  uuid PRIMARY KEY,
    signal_id           uuid REFERENCES signals(id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    strategy_id         text,
    strategy_version    text,
    symbol              text,
    direction           text,
    environment         text,
    state               text NOT NULL,
    client_order_id     text UNIQUE,      -- also written to the MT5 order comment
    order_ticket        bigint,
    position_id         bigint,
    requested_volume    double precision,
    filled_volume       double precision,
    open_volume         double precision,
    cancelled_volume    double precision,
    entry_requested     double precision,
    entry_price         double precision,
    initial_sl          double precision,
    sl                  double precision,
    tp                  double precision,
    initial_risk_money  double precision,
    risk_fraction       double precision,
    opened_at           timestamptz,
    closed_at           timestamptz,
    exit_price          double precision,
    exit_reason         text,
    gross_pnl           double precision,
    commission          double precision,
    swap                double precision,
    net_pnl             double precision,
    r_multiple          double precision,
    mfe_r               double precision,
    mae_r               double precision,
    giveback_r          double precision,
    bars_held           integer,
    session             text,
    regime              text,
    exit_model          jsonb,
    partial_done        boolean NOT NULL DEFAULT false,
    protected           boolean NOT NULL DEFAULT false,
    best_price          double precision,
    worst_price         double precision,
    extra               jsonb
);

CREATE TABLE IF NOT EXISTS risk_reservations (
    id              uuid PRIMARY KEY,
    account_key     text NOT NULL,
    signal_id       uuid NOT NULL,
    trade_id        uuid NOT NULL REFERENCES trades(id),
    symbol          text NOT NULL,
    risk_money      double precision NOT NULL CHECK (risk_money > 0),
    state           text NOT NULL DEFAULT 'RESERVED',
    expires_at      timestamptz NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_key, signal_id)
);
CREATE INDEX IF NOT EXISTS risk_reservations_active_idx
    ON risk_reservations (account_key, state, expires_at);
CREATE INDEX IF NOT EXISTS risk_reservations_trade_idx ON risk_reservations (trade_id);

CREATE INDEX IF NOT EXISTS trades_state_idx ON trades (state);
CREATE INDEX IF NOT EXISTS trades_closed_idx ON trades (closed_at DESC);
CREATE INDEX IF NOT EXISTS trades_position_idx ON trades (position_id);

CREATE TABLE IF NOT EXISTS trade_events (
    id          bigserial PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    trade_id    uuid,
    event_type  text NOT NULL,
    from_state  text,
    to_state    text,
    details     jsonb
);
CREATE INDEX IF NOT EXISTS trade_events_trade_idx ON trade_events (trade_id, created_at);

CREATE TABLE IF NOT EXISTS positions (
    id             bigserial PRIMARY KEY,
    captured_at    timestamptz NOT NULL DEFAULT now(),
    ticket         bigint,
    symbol         text,
    side           text,
    volume         double precision,
    price_open     double precision,
    sl             double precision,
    tp             double precision,
    price_current  double precision,
    profit         double precision,
    swap           double precision,
    magic          bigint,
    comment        text,
    trade_id       uuid
);
COMMENT ON TABLE positions IS 'Broker position snapshots taken on change (broker state is authoritative)';

CREATE TABLE IF NOT EXISTS execution_events (
    id                bigserial PRIMARY KEY,
    event_id          uuid UNIQUE,
    created_at        timestamptz NOT NULL DEFAULT now(),
    trade_id          uuid,
    signal_id         uuid,
    event_type        text NOT NULL,
    order_type        text,
    requested_state   jsonb,
    observed_state    jsonb,
    retcode           integer,
    retcode_text      text,
    order_ticket      bigint,
    deal_ticket       bigint,
    position_id       bigint,
    requested_volume  double precision,
    filled_volume     double precision,
    requested_price   double precision,
    price             double precision,
    slippage_points   double precision,
    latency_ms        double precision,
    error             text
);
COMMENT ON TABLE execution_events IS 'Forensic ledger of every broker interaction';
CREATE INDEX IF NOT EXISTS execution_events_created_idx ON execution_events (created_at DESC);
CREATE INDEX IF NOT EXISTS execution_events_trade_idx ON execution_events (trade_id);

-- ---------------------------------------------------------------- risk / health / audit
CREATE TABLE IF NOT EXISTS risk_states (
    id                    bigserial PRIMARY KEY,
    created_at            timestamptz NOT NULL DEFAULT now(),
    state                 text NOT NULL,
    risk_multiplier       double precision,
    equity                double precision,
    balance               double precision,
    peak_equity           double precision,
    drawdown              double precision,
    day                   text,
    day_start_equity      double precision,
    daily_pnl             double precision,
    daily_loss_remaining  double precision,
    reason                text,
    details               jsonb
);
CREATE INDEX IF NOT EXISTS risk_states_created_idx ON risk_states (created_at DESC);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id            bigserial PRIMARY KEY,
    created_at    timestamptz NOT NULL DEFAULT now(),
    environment   text,
    balance       double precision,
    equity        double precision,
    margin        double precision,
    free_margin   double precision,
    margin_level  double precision,
    profit        double precision
);
CREATE INDEX IF NOT EXISTS account_snapshots_created_idx ON account_snapshots (created_at);

CREATE TABLE IF NOT EXISTS system_health (
    id             bigserial PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    component      text NOT NULL,
    status         text NOT NULL,
    startup_state  text,
    message        text,
    details        jsonb
);
CREATE INDEX IF NOT EXISTS system_health_created_idx ON system_health (created_at DESC);

CREATE TABLE IF NOT EXISTS app_logs (
    id          bigserial PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    level       text,
    logger      text,
    message     text,
    details     jsonb
);
CREATE INDEX IF NOT EXISTS app_logs_created_idx ON app_logs (created_at DESC);

CREATE TABLE IF NOT EXISTS daily_reports (
    day         date PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    report      jsonb,
    sent        boolean NOT NULL DEFAULT false
);

-- ---------------------------------------------------------------- research
CREATE TABLE IF NOT EXISTS experiments (
    id              uuid PRIMARY KEY,
    created_at      timestamptz NOT NULL DEFAULT now(),
    kind            text NOT NULL,   -- BACKTEST / COST_STRESS / WALK_FORWARD / EXIT_RESEARCH / SESSION_RESEARCH / MONTE_CARLO
    strategy_id     text,
    hypothesis_id   text,
    title           text,
    params          jsonb,
    exit_model      jsonb,
    data_range      jsonb,
    settings        jsonb,
    metrics         jsonb,
    evidence_level  text,
    report_path     text,
    notes           text
);
CREATE INDEX IF NOT EXISTS experiments_strategy_idx ON experiments (strategy_id, created_at DESC);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id             bigserial PRIMARY KEY,
    experiment_id  uuid REFERENCES experiments(id) ON DELETE CASCADE,
    strategy_id    text,
    direction      text,
    signal_time    timestamptz,
    entry_time     timestamptz,
    exit_time      timestamptz,
    entry          double precision,
    exit           double precision,
    sl             double precision,
    tp             double precision,
    r_multiple     double precision,
    mfe_r          double precision,
    mae_r          double precision,
    giveback_r     double precision,
    bars_held      integer,
    exit_reason    text,
    session        text,
    regime         text,
    volatility     text,
    spread_points  double precision,
    cost_r         double precision,
    pnl_money      double precision,
    volume         double precision,
    features       jsonb
);
CREATE INDEX IF NOT EXISTS backtest_trades_exp_idx ON backtest_trades (experiment_id);

-- =====================================================================
--  VIEWS (used by the dashboard and handy in pgAdmin)
-- =====================================================================
CREATE OR REPLACE VIEW v_open_trades AS
SELECT * FROM trades
WHERE state IN ('APPROVED','ORDER_SENT','OPEN','PROTECTION','PARTIAL','RUNNER','USER_MODIFIED','RECONCILIATION_REQUIRED');

CREATE OR REPLACE VIEW v_closed_trades AS
SELECT * FROM trades WHERE state IN ('CLOSED','BROKER_CLOSED') AND closed_at IS NOT NULL;

CREATE OR REPLACE VIEW v_strategy_performance AS
SELECT strategy_id,
       environment,
       count(*)                                                       AS trades,
       avg(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END)               AS win_rate,
       avg(r_multiple)                                                AS expectancy_r,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r_multiple)         AS median_r,
       sum(net_pnl)                                                   AS net_pnl,
       CASE WHEN sum(CASE WHEN net_pnl < 0 THEN -net_pnl ELSE 0 END) > 0
            THEN sum(CASE WHEN net_pnl > 0 THEN net_pnl ELSE 0 END)
               / sum(CASE WHEN net_pnl < 0 THEN -net_pnl ELSE 0 END)
       END                                                            AS profit_factor,
       avg(mfe_r)                                                     AS avg_mfe_r,
       avg(giveback_r)                                                AS avg_giveback_r,
       avg(bars_held)                                                 AS avg_bars_held
FROM v_closed_trades
GROUP BY strategy_id, environment;

CREATE OR REPLACE VIEW v_session_performance AS
SELECT strategy_id, session, count(*) AS trades, avg(r_multiple) AS expectancy_r,
       avg(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate, sum(net_pnl) AS net_pnl
FROM v_closed_trades GROUP BY strategy_id, session;

-- Gate contribution: positive sum_r = the gate rejected trades that would have WON
-- (opportunity cost, "expensive"); negative sum_r = it avoided losers ("valuable").
CREATE OR REPLACE VIEW v_gate_contribution AS
SELECT gate,
       count(*)                                               AS rejected,
       count(*) FILTER (WHERE status = 'RESOLVED')            AS resolved,
       count(*) FILTER (WHERE hypothetical_r > 0)             AS would_have_won,
       count(*) FILTER (WHERE hypothetical_r <= 0)            AS would_have_lost,
       count(*) FILTER (WHERE status = 'OPEN')                AS still_open,
       coalesce(sum(hypothetical_r), 0)                       AS sum_r,
       avg(hypothetical_r)                                    AS avg_r
FROM counterfactuals
GROUP BY gate;

CREATE OR REPLACE VIEW v_counterfactual_today AS
SELECT * FROM counterfactuals WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';

CREATE OR REPLACE VIEW v_execution_quality AS
SELECT date_trunc('day', created_at) AS day,
       count(*) FILTER (WHERE event_type = 'ORDER_RESULT')                         AS orders,
       count(*) FILTER (WHERE event_type = 'ORDER_RESULT' AND retcode NOT IN (10008,10009,10010)) AS rejected_orders,
       avg(slippage_points) FILTER (WHERE event_type = 'FILL')                      AS avg_slippage_points,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY latency_ms)                      AS p90_latency_ms,
       count(*) FILTER (WHERE event_type = 'FILL' AND filled_volume < requested_volume) AS partial_fills
FROM execution_events GROUP BY 1;

CREATE OR REPLACE VIEW v_spread_by_session AS
SELECT symbol, session, count(*) AS samples,
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY spread_points) AS p50,
       percentile_cont(0.9)  WITHIN GROUP (ORDER BY spread_points) AS p90,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY spread_points) AS p99
FROM ticks GROUP BY symbol, session;

-- =====================================================================
--  SEED DATA
-- ---------------------------------------------------------------- adaptive parameters (v0.2)
CREATE TABLE IF NOT EXISTS adaptive_param_history (
    id                          bigserial PRIMARY KEY,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    symbol                      text NOT NULL,
    updated                     boolean NOT NULL,
    reason                      text,
    vol_scale                   double precision,
    max_spread_points           double precision,
    previous_vol_scale          double precision,
    previous_max_spread_points  double precision,
    metrics                     jsonb
);
COMMENT ON TABLE adaptive_param_history IS 'Hourly per-symbol adaptive parameter refreshes (audit trail of every change)';
CREATE INDEX IF NOT EXISTS adaptive_param_history_symbol_idx ON adaptive_param_history (symbol, created_at DESC);

CREATE TABLE IF NOT EXISTS selector_history (
    id              bigserial PRIMARY KEY,
    created_at      timestamptz NOT NULL DEFAULT now(),
    symbol          text NOT NULL,
    strategy_id     text NOT NULL,
    previous_state  text,
    state           text NOT NULL,
    multiplier      double precision NOT NULL,
    reason          text,
    applied         boolean NOT NULL,
    metrics         jsonb
);
COMMENT ON TABLE selector_history IS 'Strategy selector state changes per strategy and symbol (audit trail of every change)';
CREATE INDEX IF NOT EXISTS selector_history_symbol_idx ON selector_history (symbol, strategy_id, created_at DESC);

-- hourly spread profile per symbol (fed by `collect-spreads`, which records every configured symbol)
CREATE OR REPLACE VIEW v_spread_by_symbol_hour AS
SELECT symbol, EXTRACT(HOUR FROM time AT TIME ZONE 'UTC')::int AS hour_utc, count(*) AS samples,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY spread_points) AS median_points,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY spread_points) AS p90_points,
       max(spread_points) AS max_points
FROM ticks GROUP BY symbol, EXTRACT(HOUR FROM time AT TIME ZONE 'UTC');

-- ---------------------------------------------------------------- research lab (VectorBT + Backtrader)
CREATE TABLE IF NOT EXISTS research_lab_runs (
    id           bigserial PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    symbol       text NOT NULL,
    bars         integer,
    signals      integer,
    verdict      text,
    best         jsonb,
    train        jsonb,
    test         jsonb,
    backtrader   jsonb,
    report_path  text,
    note         text
);
COMMENT ON TABLE research_lab_runs IS 'Periodic VectorBT sweep + Backtrader cross-check of the bot entries, per symbol (advisory research)';
CREATE INDEX IF NOT EXISTS research_lab_runs_symbol_idx ON research_lab_runs (symbol, created_at DESC);

-- =====================================================================
INSERT INTO system_flags (key, value, updated_by)
VALUES ('kill_switch', '{"engaged": false, "reason": "initial"}'::jsonb, 'schema')
ON CONFLICT (key) DO NOTHING;

INSERT INTO schema_migrations (version, description)
VALUES ('0001', 'Initial schema v0.1') ON CONFLICT (version) DO NOTHING;

-- =====================================================================
--  PERMISSIONS for the application role (only if it exists)
-- =====================================================================
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quantara_app') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA public TO quantara_app';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO quantara_app';
        EXECUTE 'GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO quantara_app';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO quantara_app';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO quantara_app';
    END IF;
END
$$;
