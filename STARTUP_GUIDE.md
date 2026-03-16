# VRP Trading System — Startup Guide

## Architecture Overview

```
┌───────────────┐     ┌──────────────┐     ┌──────────────┐
│  Flask API    │────▶│   Redis      │◀────│ Async Engine │
│  (app.py)     │     │  (pub/sub +  │     │ (WebSocket   │
│  Port 5050    │     │   cache)     │     │  ticks +     │
│               │     │  Port 6379   │     │  hedging)    │
└──────┬────────┘     └──────────────┘     └──────────────┘
       │                                          │
       │              ┌──────────────┐            │
       └─────────────▶│ PostgreSQL   │◀───────────┘
                      │ (positions,  │
                      │  orders, DB) │
                      │  Port 5432   │
                      └──────────────┘
```

---

## 1. Infrastructure Services

### Redis (Required)
Real-time tick cache, pub/sub for engine ↔ Flask communication.

```bash
# Install
sudo apt install redis-server

# Start
sudo service redis-server start

# Verify
redis-cli ping    # → PONG
```

**Default URL:** `redis://localhost:6379/0`

---

### PostgreSQL (Required)
Persistent storage for positions, order events, and fills.

```bash
# Install
sudo apt install postgresql

# Start
sudo service postgresql start

# Create database & user
sudo -u postgres psql -c "CREATE USER vrp WITH PASSWORD 'vrp';"
sudo -u postgres psql -c "CREATE DATABASE vrp_db OWNER vrp;"

# Verify
psql -U vrp -d vrp_db -h localhost -c "SELECT 1;"
```

**Default URL:** `postgresql://vrp:vrp@localhost:5432/vrp_db`

> Tables are auto-created on first startup via SQLAlchemy.

---

## 2. Python Dependencies

```bash
# Core
pip install flask flask-cors structlog redis

# Broker
pip install kiteconnect

# Computation
pip install numpy scipy cython setuptools

# Database
pip install sqlalchemy psycopg2-binary

# Async engine
pip install aiohttp "redis[hiredis]"

# Task queue (optional — for background reconciliation)
pip install celery

# Process supervision (optional)
pip install supervisor

# Timezone
pip install pytz
```

### Cython Fast Greeks (Optional but recommended)
Compiles the Black-Scholes engine to C for ~100x speedup:

```bash
cd /home/deepakchp/Project/Trading/Option/tradeApp
python setup.py build_ext --inplace
```

---

## 3. Environment Variables

Create a `.env` file or export these variables:

```bash
# ── Broker (Required for live trading) ──
export KITE_API_KEY="your_kite_api_key"
export KITE_API_SECRET="your_kite_api_secret"

# ── Trading Mode ──
export PAPER_TRADE="true"         # "true" = simulated | "false" = live orders

# ── Infrastructure ──
export REDIS_URL="redis://localhost:6379/0"
export DATABASE_URL="postgresql://vrp:vrp@localhost:5432/vrp_db"

# ── Flask ──
export FLASK_SECRET="change-me-in-production"
export FLASK_HOST="0.0.0.0"
export FLASK_PORT="5050"
export DEBUG="false"
```

---

## 4. Application Processes

### Process 1: Flask API Server (Required)
Main web application — dashboard, trade execution, REST API.

```bash
python app.py
# → Starts on http://0.0.0.0:5050
```

### Process 2: Async Engine (Required for live hedging)
WebSocket tick consumer, real-time Greeks recomputation, delta hedging.

```bash
python async_engine.py
```

### Process 3: Celery Worker (Optional)
Background task queue for reconciliation, non-critical order retries.

```bash
celery -A modules.tasks.celery_app worker --loglevel=info --concurrency=2
```

---

## 5. Quick Start (3 terminals)

```bash
# Terminal 1 — Infrastructure
sudo service redis-server start
sudo service postgresql start

# Terminal 2 — Flask API
cd /home/deepakchp/Project/Trading/Option/tradeApp
export PAPER_TRADE=true
python app.py

# Terminal 3 — Async Engine
cd /home/deepakchp/Project/Trading/Option/tradeApp
python async_engine.py
```

Then open: **http://localhost:5050/login**

---

## 6. Using Supervisord (Production)

Start all processes with one command:

```bash
pip install supervisor
supervisord -c supervisord.conf
```

Manage processes:

```bash
supervisorctl status              # View all process states
supervisorctl restart vrp_system: # Restart all
supervisorctl stop celery_worker  # Stop one process
supervisorctl tail -f flask_app   # Follow logs
```

---

## Component Summary

| Component | Required | Port | Purpose |
|---|---|---|---|
| **Redis** | ✅ Yes | 6379 | Tick cache, pub/sub |
| **PostgreSQL** | ✅ Yes | 5432 | Position/order persistence |
| **Flask (app.py)** | ✅ Yes | 5050 | Web UI + REST API |
| **Async Engine** | ⚡ For live | — | Real-time ticks + hedging |
| **Celery Worker** | 🔄 Optional | — | Background tasks |
| **Zerodha Kite** | 🔗 For live | — | Broker API (paper mode works without) |
| **Cython build** | 🚀 Optional | — | ~100x faster Greeks |
