-- Strategy selector audit trail (app/selection/selector.py). Run once on an existing database, or let the bot
-- create it: this file only adds one table, so the bot's own database user may run it without owning the others.
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
