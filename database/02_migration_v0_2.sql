-- Run once on an EXISTING quantara database (new installs get this from 01_schema.sql). Safe to re-run.
BEGIN;

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

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quantara_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO quantara_app';
        EXECUTE 'GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO quantara_app';
    END IF;
END
$$;

COMMIT;
