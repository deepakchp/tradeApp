#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
# scripts/setup_db.sh — PostgreSQL Database Setup for VRP System
# ─────────────────────────────────────────────────────────────────
# Usage:
#   chmod +x scripts/setup_db.sh
#   ./scripts/setup_db.sh
#
# What it does:
#   1. Creates the PostgreSQL role (vrp) if it doesn't exist
#   2. Creates the database (vrp_db) if it doesn't exist
#   3. Creates all tables (positions, legs, order_events)
#   4. Creates indexes for query performance
#   5. Verifies the setup
# ─────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Config (override via env vars) ─────────────────────────────
DB_USER="${DB_USER:-vrp}"
DB_PASS="${DB_PASS:-vrp}"
DB_NAME="${DB_NAME:-vrp_db}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${YELLOW}═══════════════════════════════════════════════════${NC}"
echo -e "${YELLOW}  VRP Trading System — Database Setup${NC}"
echo -e "${YELLOW}═══════════════════════════════════════════════════${NC}"
echo ""

# ── 1. Check PostgreSQL is running ────────────────────────────
echo -n "[1/5] Checking PostgreSQL... "
if pg_isready -h "$DB_HOST" -p "$DB_PORT" > /dev/null 2>&1; then
    echo -e "${GREEN}running${NC}"
else
    echo -e "${RED}not running${NC}"
    echo "  Start PostgreSQL first:"
    echo "    sudo service postgresql start"
    echo "    OR: supervisord -c supervisord.conf"
    exit 1
fi

# ── 2. Create role ────────────────────────────────────────────
echo -n "[2/5] Creating role '${DB_USER}'... "
ROLE_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}';" 2>/dev/null || echo "")
if [ "$ROLE_EXISTS" = "1" ]; then
    echo -e "${YELLOW}already exists${NC}"
else
    sudo -u postgres psql -c "CREATE ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASS}';" > /dev/null 2>&1
    echo -e "${GREEN}created${NC}"
fi

# ── 3. Create database ───────────────────────────────────────
echo -n "[3/5] Creating database '${DB_NAME}'... "
DB_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}';" 2>/dev/null || echo "")
if [ "$DB_EXISTS" = "1" ]; then
    echo -e "${YELLOW}already exists${NC}"
else
    sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};" > /dev/null 2>&1
    echo -e "${GREEN}created${NC}"
fi

# Grant privileges
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};" > /dev/null 2>&1

# ── 4. Create tables ─────────────────────────────────────────
echo -n "[4/5] Creating tables... "
sudo -u postgres psql -d "${DB_NAME}" <<'SQL' > /dev/null 2>&1

-- Grant schema access
GRANT ALL ON SCHEMA public TO vrp;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO vrp;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO vrp;

-- ── positions ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS positions (
    id                          SERIAL PRIMARY KEY,
    position_id                 VARCHAR(64) UNIQUE NOT NULL,
    symbol                      VARCHAR(32) NOT NULL,
    strategy                    VARCHAR(32) NOT NULL,
    state                       VARCHAR(16) NOT NULL DEFAULT 'pending',
    entry_time                  TIMESTAMP DEFAULT NOW(),
    entry_spot                  DOUBLE PRECISION DEFAULT 0.0,
    entry_iv                    DOUBLE PRECISION DEFAULT 0.0,
    max_profit                  DOUBLE PRECISION DEFAULT 0.0,
    beta                        DOUBLE PRECISION DEFAULT 1.0,
    challenged_side_recentered  BOOLEAN DEFAULT FALSE,
    last_adjustment_ts          DOUBLE PRECISION DEFAULT 0.0,
    closed_at                   TIMESTAMP,
    created_at                  TIMESTAMP DEFAULT NOW(),
    updated_at                  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_positions_position_id ON positions(position_id);
CREATE INDEX IF NOT EXISTS ix_positions_symbol      ON positions(symbol);
CREATE INDEX IF NOT EXISTS ix_positions_state       ON positions(state);

-- ── legs ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS legs (
    id           SERIAL PRIMARY KEY,
    position_id  VARCHAR(64) NOT NULL REFERENCES positions(position_id) ON DELETE CASCADE,
    symbol       VARCHAR(64) NOT NULL,
    strike       DOUBLE PRECISION NOT NULL,
    expiry       DATE NOT NULL,
    option_type  VARCHAR(4) NOT NULL,       -- 'CE' or 'PE'
    is_long      BOOLEAN NOT NULL,
    lots         INTEGER NOT NULL,
    lot_size     INTEGER DEFAULT 1,
    exchange     VARCHAR(8) DEFAULT 'NFO',
    entry_price  DOUBLE PRECISION DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS ix_legs_position_id ON legs(position_id);

-- ── order_events ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS order_events (
    id            SERIAL PRIMARY KEY,
    position_id   VARCHAR(64) NOT NULL REFERENCES positions(position_id) ON DELETE CASCADE,
    event_type    VARCHAR(48) NOT NULL,
    action        VARCHAR(48),
    leg_symbol    VARCHAR(64),
    fill_price    DOUBLE PRECISION,
    order_id      VARCHAR(64),
    details_json  TEXT,
    timestamp     TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_order_events_position_id ON order_events(position_id);
CREATE INDEX IF NOT EXISTS ix_order_events_ts          ON order_events(timestamp);

SQL
echo -e "${GREEN}done${NC}"

# ── 5. Insert sample data ────────────────────────────────────
echo -n "[5/6] Inserting sample data... "
sudo -u postgres psql -d "${DB_NAME}" <<'SQL' > /dev/null 2>&1

-- Skip if sample data already exists
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM positions WHERE position_id = 'SAMPLE-IC-001') THEN
        RAISE NOTICE 'Sample data already exists — skipping';
        RETURN;
    END IF;

    -- ══════════════════════════════════════════════════════════
    -- Position 1: RELIANCE Iron Condor (ACTIVE — 30 DTE)
    -- ══════════════════════════════════════════════════════════
    INSERT INTO positions (position_id, symbol, strategy, state, entry_time, entry_spot, entry_iv, max_profit, beta)
    VALUES ('SAMPLE-IC-001', 'RELIANCE', 'iron_condor', 'active',
            NOW() - INTERVAL '15 days', 2500.00, 28.0, 4700.00, 1.15);

    -- Short Put 2350 (16D)
    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-001', 'RELIANCE25APR2350PE', 2350.0, CURRENT_DATE + 30, 'PE', FALSE, 1, 250, 'NFO', 42.0);

    -- Long Put 2250 (10D wing)
    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-001', 'RELIANCE25APR2250PE', 2250.0, CURRENT_DATE + 30, 'PE', TRUE, 1, 250, 'NFO', 18.0);

    -- Short Call 2650 (16D)
    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-001', 'RELIANCE25APR2650CE', 2650.0, CURRENT_DATE + 30, 'CE', FALSE, 1, 250, 'NFO', 38.0);

    -- Long Call 2750 (10D wing)
    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-001', 'RELIANCE25APR2750CE', 2750.0, CURRENT_DATE + 30, 'CE', TRUE, 1, 250, 'NFO', 15.0);

    -- Entry event
    INSERT INTO order_events (position_id, event_type, action, details_json, timestamp)
    VALUES ('SAMPLE-IC-001', 'position.opened', 'entry',
            '{"net_credit": 47.0, "max_profit": 4700.0, "max_loss": 5300.0, "mode": "paper"}',
            NOW() - INTERVAL '15 days');

    -- ══════════════════════════════════════════════════════════
    -- Position 2: HDFCBANK Iron Condor (ACTIVE — challenged leg rolled)
    -- ══════════════════════════════════════════════════════════
    INSERT INTO positions (position_id, symbol, strategy, state, entry_time, entry_spot, entry_iv, max_profit, beta, challenged_side_recentered)
    VALUES ('SAMPLE-IC-002', 'HDFCBANK', 'iron_condor', 'active',
            NOW() - INTERVAL '22 days', 1650.00, 24.5, 3200.00, 0.95, TRUE);

    -- Short Put 1550 (16D)
    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-002', 'HDFCBANK25APR1550PE', 1550.0, CURRENT_DATE + 23, 'PE', FALSE, 2, 550, 'NFO', 22.0);

    -- Long Put 1475 (10D wing)
    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-002', 'HDFCBANK25APR1475PE', 1475.0, CURRENT_DATE + 23, 'PE', TRUE, 2, 550, 'NFO', 10.5);

    -- Short Call 1750 → Rolled to 1800 (16D after drift adjustment)
    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-002', 'HDFCBANK25APR1800CE', 1800.0, CURRENT_DATE + 23, 'CE', FALSE, 2, 550, 'NFO', 18.5);

    -- Long Call 1900 (10D wing)
    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-002', 'HDFCBANK25APR1900CE', 1900.0, CURRENT_DATE + 23, 'CE', TRUE, 2, 550, 'NFO', 7.0);

    -- Events: entry + roll
    INSERT INTO order_events (position_id, event_type, action, details_json, timestamp)
    VALUES ('SAMPLE-IC-002', 'position.opened', 'entry',
            '{"net_credit": 23.0, "max_profit": 3200.0, "mode": "paper"}',
            NOW() - INTERVAL '22 days');

    INSERT INTO order_events (position_id, event_type, action, leg_symbol, details_json, timestamp)
    VALUES ('SAMPLE-IC-002', 'position.rolled', 'roll_challenged_leg', 'HDFCBANK25APR1800CE',
            '{"old_strike": 1750, "new_strike": 1800, "target_delta": 0.16, "reason": "gradual_drift"}',
            NOW() - INTERVAL '8 days');

    -- ══════════════════════════════════════════════════════════
    -- Position 3: TCS Iron Condor (CLOSED — profit exit at 42%)
    -- ══════════════════════════════════════════════════════════
    INSERT INTO positions (position_id, symbol, strategy, state, entry_time, entry_spot, entry_iv, max_profit, beta, closed_at)
    VALUES ('SAMPLE-IC-003', 'TCS', 'iron_condor', 'closed',
            NOW() - INTERVAL '35 days', 3800.00, 22.0, 5600.00, 0.80,
            NOW() - INTERVAL '12 days');

    -- Short Put 3600
    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-003', 'TCS25MAR3600PE', 3600.0, CURRENT_DATE - 5, 'PE', FALSE, 1, 175, 'NFO', 55.0);

    -- Long Put 3450
    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-003', 'TCS25MAR3450PE', 3450.0, CURRENT_DATE - 5, 'PE', TRUE, 1, 175, 'NFO', 22.0);

    -- Short Call 4000
    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-003', 'TCS25MAR4000CE', 4000.0, CURRENT_DATE - 5, 'CE', FALSE, 1, 175, 'NFO', 48.0);

    -- Long Call 4150
    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-003', 'TCS25MAR4150CE', 4150.0, CURRENT_DATE - 5, 'CE', TRUE, 1, 175, 'NFO', 17.0);

    -- Events: entry + profit exit
    INSERT INTO order_events (position_id, event_type, action, details_json, timestamp)
    VALUES ('SAMPLE-IC-003', 'position.opened', 'entry',
            '{"net_credit": 64.0, "max_profit": 5600.0, "mode": "paper"}',
            NOW() - INTERVAL '35 days');

    INSERT INTO order_events (position_id, event_type, action, details_json, timestamp)
    VALUES ('SAMPLE-IC-003', 'position.closed', 'profit_target_40pct',
            '{"net_pnl": 2352.0, "profit_pct": 0.42, "fills": [{"leg": "TCS25MAR3600PE", "side": "buy", "fill_price": 12.5}, {"leg": "TCS25MAR3450PE", "side": "sell", "fill_price": 3.2}, {"leg": "TCS25MAR4000CE", "side": "buy", "fill_price": 10.8}, {"leg": "TCS25MAR4150CE", "side": "sell", "fill_price": 2.1}]}',
            NOW() - INTERVAL '12 days');

    -- ══════════════════════════════════════════════════════════
    -- Position 4: NIFTY Iron Condor (CLOSED — gamma exit at 14 DTE)
    -- ══════════════════════════════════════════════════════════
    INSERT INTO positions (position_id, symbol, strategy, state, entry_time, entry_spot, entry_iv, max_profit, beta, closed_at)
    VALUES ('SAMPLE-IC-004', 'NIFTY', 'iron_condor', 'closed',
            NOW() - INTERVAL '40 days', 22500.00, 15.2, 8500.00, 1.0,
            NOW() - INTERVAL '9 days');

    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-004', 'NIFTY25MAR21500PE', 21500.0, CURRENT_DATE - 2, 'PE', FALSE, 2, 50, 'NFO', 95.0);

    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-004', 'NIFTY25MAR21000PE', 21000.0, CURRENT_DATE - 2, 'PE', TRUE, 2, 50, 'NFO', 42.0);

    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-004', 'NIFTY25MAR23500CE', 23500.0, CURRENT_DATE - 2, 'CE', FALSE, 2, 50, 'NFO', 88.0);

    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-004', 'NIFTY25MAR24000CE', 24000.0, CURRENT_DATE - 2, 'CE', TRUE, 2, 50, 'NFO', 35.0);

    -- Events: entry + gamma exit
    INSERT INTO order_events (position_id, event_type, action, details_json, timestamp)
    VALUES ('SAMPLE-IC-004', 'position.opened', 'entry',
            '{"net_credit": 106.0, "max_profit": 8500.0, "mode": "paper"}',
            NOW() - INTERVAL '40 days');

    INSERT INTO order_events (position_id, event_type, action, details_json, timestamp)
    VALUES ('SAMPLE-IC-004', 'position.closed', 'gamma_exit_14dte',
            '{"net_pnl": 5200.0, "profit_pct": 0.31, "dte_at_exit": 14}',
            NOW() - INTERVAL '9 days');

    -- ══════════════════════════════════════════════════════════
    -- Position 5: INFY Iron Condor (CLOSED — vanna collapse exit)
    -- ══════════════════════════════════════════════════════════
    INSERT INTO positions (position_id, symbol, strategy, state, entry_time, entry_spot, entry_iv, max_profit, beta, closed_at)
    VALUES ('SAMPLE-IC-005', 'INFY', 'iron_condor', 'closed',
            NOW() - INTERVAL '18 days', 1550.00, 30.0, 2800.00, 0.85,
            NOW() - INTERVAL '6 days');

    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-005', 'INFY25APR1450PE', 1450.0, CURRENT_DATE + 12, 'PE', FALSE, 2, 400, 'NFO', 28.0);

    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-005', 'INFY25APR1375PE', 1375.0, CURRENT_DATE + 12, 'PE', TRUE, 2, 400, 'NFO', 12.0);

    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-005', 'INFY25APR1650CE', 1650.0, CURRENT_DATE + 12, 'CE', FALSE, 2, 400, 'NFO', 25.0);

    INSERT INTO legs (position_id, symbol, strike, expiry, option_type, is_long, lots, lot_size, exchange, entry_price)
    VALUES ('SAMPLE-IC-005', 'INFY25APR1725CE', 1725.0, CURRENT_DATE + 12, 'CE', TRUE, 2, 400, 'NFO', 9.0);

    -- Events: entry + vanna close
    INSERT INTO order_events (position_id, event_type, action, details_json, timestamp)
    VALUES ('SAMPLE-IC-005', 'position.opened', 'entry',
            '{"net_credit": 32.0, "max_profit": 2800.0, "mode": "paper"}',
            NOW() - INTERVAL '18 days');

    INSERT INTO order_events (position_id, event_type, action, details_json, timestamp)
    VALUES ('SAMPLE-IC-005', 'position.closed', 'vanna_collapse',
            '{"net_pnl": 1680.0, "profit_pct": 0.30, "iv_drop_pct": 0.22, "spot_drift_pct": 0.003, "reason": "IV dropped 22% from entry; price within 0.3% — vanna P&L pulled forward"}',
            NOW() - INTERVAL '6 days');

END $$;

SQL
echo -e "${GREEN}done${NC}"

# ── 6. Verify ─────────────────────────────────────────────────
echo -n "[6/6] Verifying... "
TABLE_COUNT=$(psql -U "${DB_USER}" -h "${DB_HOST}" -p "${DB_PORT}" -d "${DB_NAME}" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';" 2>/dev/null)

if [ "$TABLE_COUNT" -ge 3 ]; then
    echo -e "${GREEN}${TABLE_COUNT} tables found${NC}"
else
    echo -e "${RED}expected 3 tables, found ${TABLE_COUNT}${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Setup complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""
echo "  Connection string:"
echo "    postgresql://${DB_USER}:${DB_PASS}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
echo ""
echo "  Tables:"
psql -U "${DB_USER}" -h "${DB_HOST}" -p "${DB_PORT}" -d "${DB_NAME}" -c "\dt" 2>/dev/null
echo ""
echo "  Sample data summary:"
psql -U "${DB_USER}" -h "${DB_HOST}" -p "${DB_PORT}" -d "${DB_NAME}" -c "
    SELECT position_id, symbol, strategy, state,
           to_char(entry_time, 'DD-Mon-YY') AS entered,
           max_profit AS max_pnl
    FROM positions ORDER BY entry_time;
" 2>/dev/null
echo ""
echo "  Test from Python:"
echo "    python -c \"from modules.db import init_db; init_db()\""
