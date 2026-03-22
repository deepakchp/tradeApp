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

# ── 5. Verify ─────────────────────────────────────────────────
echo -n "[5/5] Verifying... "
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
echo "  Test from Python:"
echo "    python -c \"from modules.db import init_db; init_db()\""
