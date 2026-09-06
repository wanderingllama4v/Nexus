-- NEXUS PostgreSQL Schema
-- Designed for all 5 phases. Phase 1 uses: runs, symbol_universe, symbol_scans, contract_scans.
-- Phase 2+ tables (agent_outputs, trades, iv_history) are created now so migrations are not needed later.

-- ── Runs ─────────────────────────────────────────────────────────────────────
-- One row per NEXUS orchestrator invocation.
CREATE TABLE IF NOT EXISTS runs (
    id              SERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    phase           SMALLINT NOT NULL DEFAULT 1,   -- 1-5
    status          VARCHAR(20) NOT NULL DEFAULT 'running',  -- running | completed | failed
    symbols_scanned INTEGER,
    contracts_found INTEGER,
    market_regime   VARCHAR(50),   -- set in Phase 2 by ATLAS
    notes           TEXT
);

-- ── Symbol universe ───────────────────────────────────────────────────────────
-- Symbols selected for this run by the flow scanner + momentum filter.
CREATE TABLE IF NOT EXISTS symbol_universe (
    id              SERIAL PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    symbol          VARCHAR(20) NOT NULL,
    price           NUMERIC(12, 4),
    prev_close      NUMERIC(12, 4),
    change_pct      NUMERIC(8, 4),       -- % change from prev close
    -- Flow data (if unusual flow detected)
    flow_detected   BOOLEAN NOT NULL DEFAULT FALSE,
    flow_direction  VARCHAR(5),          -- CALL | PUT
    flow_strike     NUMERIC(10, 2),
    flow_expiry     DATE,
    flow_volume     INTEGER,
    flow_oi         INTEGER,
    flow_vol_oi_ratio NUMERIC(8, 2),
    flow_notional   NUMERIC(16, 2),
    flow_confidence VARCHAR(10),         -- high | medium | low
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_symbol_universe_run ON symbol_universe(run_id);
CREATE INDEX IF NOT EXISTS idx_symbol_universe_symbol ON symbol_universe(symbol);

-- ── Symbol scans ──────────────────────────────────────────────────────────────
-- QUANT component output: technical analysis per symbol per run.
CREATE TABLE IF NOT EXISTS symbol_scans (
    id               SERIAL PRIMARY KEY,
    run_id           INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    universe_id      INTEGER REFERENCES symbol_universe(id) ON DELETE SET NULL,
    symbol           VARCHAR(20) NOT NULL,
    price            NUMERIC(12, 4),
    direction        VARCHAR(10) NOT NULL,   -- BULLISH | BEARISH | NEUTRAL
    technical_score  NUMERIC(5, 2) NOT NULL, -- 0-100
    -- Indicators
    rsi              NUMERIC(6, 2),
    atr              NUMERIC(12, 4),
    atr_pct          NUMERIC(6, 3),          -- ATR as % of price
    vwap             NUMERIC(12, 4),
    vwap_position    VARCHAR(6),             -- ABOVE | BELOW
    ema9             NUMERIC(12, 4),
    ema21            NUMERIC(12, 4),
    ema50            NUMERIC(12, 4),
    ema_aligned      BOOLEAN,                -- ema9 > ema21 > ema50
    volume_ratio     NUMERIC(6, 2),          -- vs 20-period avg
    momentum_pct     NUMERIC(8, 4),          -- 10-period rate of change
    relative_strength NUMERIC(8, 4),         -- return vs SPY (same period)
    -- Options context (if chain was fetched)
    atm_iv           NUMERIC(8, 4),
    iv_percentile    NUMERIC(5, 1),
    expected_move_1w NUMERIC(10, 2),
    skew_put_call    NUMERIC(8, 4),         -- put IV - call IV at 25-delta
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_symbol_scans_run ON symbol_scans(run_id);
CREATE INDEX IF NOT EXISTS idx_symbol_scans_score ON symbol_scans(run_id, technical_score DESC);

-- ── Contract scans ────────────────────────────────────────────────────────────
-- OPTIONS SCANNER output: filtered and scored contracts per symbol per run.
CREATE TABLE IF NOT EXISTS contract_scans (
    id              SERIAL PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    symbol_scan_id  INTEGER REFERENCES symbol_scans(id) ON DELETE SET NULL,
    symbol          VARCHAR(20) NOT NULL,
    contract_symbol VARCHAR(40) NOT NULL,    -- OCC padded symbol
    expiry          DATE NOT NULL,
    dte             SMALLINT NOT NULL,
    strike          NUMERIC(10, 2) NOT NULL,
    option_type     VARCHAR(5) NOT NULL,     -- call | put
    -- Pricing (from DXFeed Quote)
    bid             NUMERIC(10, 4),
    ask             NUMERIC(10, 4),
    mid             NUMERIC(10, 4),
    spread_pct      NUMERIC(6, 3),           -- (ask-bid)/mid × 100
    -- Activity (from DXFeed Summary + Trade)
    open_interest   INTEGER,
    day_volume      INTEGER,
    -- Greeks (from DXFeed Greeks)
    delta           NUMERIC(8, 4),
    gamma           NUMERIC(10, 6),
    theta           NUMERIC(10, 4),
    vega            NUMERIC(10, 4),
    iv              NUMERIC(8, 4),
    -- Score
    contract_score  NUMERIC(5, 2) NOT NULL,  -- 0-100
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_contract_scans_run ON contract_scans(run_id);
CREATE INDEX IF NOT EXISTS idx_contract_scans_symbol ON contract_scans(run_id, symbol);
CREATE INDEX IF NOT EXISTS idx_contract_scans_score ON contract_scans(run_id, contract_score DESC);

-- ── IV history ────────────────────────────────────────────────────────────────
-- Accumulated ATM IV observations per symbol per day.
-- Used to compute IV percentile once sufficient history exists (90+ days).
CREATE TABLE IF NOT EXISTS iv_history (
    id          SERIAL PRIMARY KEY,
    symbol      VARCHAR(20) NOT NULL,
    date        DATE NOT NULL,
    atm_iv      NUMERIC(8, 4) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, date)
);

CREATE INDEX IF NOT EXISTS idx_iv_history_symbol ON iv_history(symbol, date DESC);

-- ── Agent outputs (Phase 2+) ──────────────────────────────────────────────────
-- Raw inputs and outputs for every LLM agent call. Full audit trail.
CREATE TABLE IF NOT EXISTS agent_outputs (
    id           SERIAL PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    agent        VARCHAR(50) NOT NULL,   -- atlas | compass | hunter | edge | analyst | sentinel | judge | scout_*
    symbol       VARCHAR(20),            -- NULL for market-level agents (atlas, compass)
    input_data   JSONB,
    output_data  JSONB,
    score        NUMERIC(5, 2),          -- agent's primary numeric output if applicable
    tokens_in    INTEGER,
    tokens_out   INTEGER,
    latency_ms   INTEGER,
    model        VARCHAR(60),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_outputs_run ON agent_outputs(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_outputs_agent ON agent_outputs(run_id, agent);

-- ── Trades (Phase 5) ──────────────────────────────────────────────────────────
-- Final trade decisions and their lifecycle.
CREATE TABLE IF NOT EXISTS trades (
    id                SERIAL PRIMARY KEY,
    run_id            INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    contract_scan_id  INTEGER REFERENCES contract_scans(id) ON DELETE SET NULL,
    symbol            VARCHAR(20) NOT NULL,
    contract_symbol   VARCHAR(40) NOT NULL,
    option_type       VARCHAR(5) NOT NULL,
    direction         VARCHAR(10) NOT NULL,   -- BULLISH | BEARISH
    strike            NUMERIC(10, 2),
    expiry            DATE,
    dte_at_entry      SMALLINT,
    -- Decision
    decision          VARCHAR(10) NOT NULL,   -- TRADE | WATCH | PASS
    final_score       NUMERIC(5, 2),
    openai_score      NUMERIC(5, 2),
    claude_score      NUMERIC(5, 2),
    sentinel_flagged  BOOLEAN DEFAULT FALSE,
    guardian_blocked  BOOLEAN DEFAULT FALSE,
    -- Entry/exit
    entry_price       NUMERIC(10, 4),
    stop_price        NUMERIC(10, 4),
    target_price      NUMERIC(10, 4),
    contracts         SMALLINT,
    -- Outcome
    exit_price        NUMERIC(10, 4),
    pnl               NUMERIC(12, 2),
    pnl_pct           NUMERIC(8, 4),
    exit_reason       VARCHAR(50),   -- stop_hit | target_hit | expired | manual
    status            VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending | open | closed | expired
    opened_at         TIMESTAMPTZ,
    closed_at         TIMESTAMPTZ,
    notes             JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_run ON trades(run_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
