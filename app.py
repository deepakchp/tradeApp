"""
app.py — Flask Orchestration Layer
====================================
API Endpoints:
  POST /api/v1/trade/enter          — Run VRP gate and open a new position
  GET  /api/v1/positions            — List all active positions with Greeks
  GET  /api/v1/portfolio/greeks     — Portfolio-level aggregated Greeks
  POST /api/v1/trade/adjust         — Manually trigger an adjustment
  GET  /api/v1/risk/stress-test     — Run VIX-doubling stress scenario
  POST /api/v1/kill-switch          — EMERGENCY: cancel all orders, flatten all positions
  GET  /api/v1/health               — System health check
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Dict, List, Optional

import structlog
from flask import Flask, jsonify, request, g, render_template, redirect, url_for
from flask_cors import CORS

from config import (
    FLASK_SECRET, FLASK_HOST, FLASK_PORT, DEBUG,
    LOG_LEVEL, LOG_FILE, RISK_CFG, BROKER, VRP_GATE,
    UNDERLYING_INSTRUMENTS, SYMBOL_EXCHANGE,
    GREEKS_CFG, LIFECYCLE_CFG, SLIPPAGE_CFG, WHIPSAW_CFG, SKEW_CFG,
    PAPER_TRADE, REDIS_URL, DATABASE_URL,
    MTM_MONITOR_INTERVAL_SEC, DELTA_MONITOR_INTERVAL_SEC, CELERY_TIMEZONE,
    KITE_API_KEY, KITE_API_SECRET,
    AUTO_SCAN_SYMBOLS, LOT_SIZES, NIFTY50_SYMBOLS,
)
from engine import (
    BlackScholesEngine,
    DynamicHedgingEngine,
    PortfolioGreeksEngine,
    Position, PositionState, StrategyType,
    SizingEngine, SlippageController, StrategySelector,
    VRPGateValidator,
)
from modules.data_engine import DataEngine, IVSurface
from modules.tasks import celery_app, order_log
from modules.broker import KiteBroker
from modules.scanner import AutoScanner, PendingSignal
from modules.backtester import BacktestEngine, BacktestBatchEngine, BacktestConfig
from modules.data_stream import MarketDataStreamer
import redis


# ─────────────────────────────────────────────────────────────────
# STRUCTURED LOGGING SETUP
# ─────────────────────────────────────────────────────────────────

def configure_logging() -> None:
    """Configure structlog for structured JSON output to file + stderr."""
    import structlog

    log_level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)

    # File handler (JSONL — one JSON object per line)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setLevel(log_level)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)

    logging.basicConfig(
        level=log_level,
        handlers=[file_handler, console_handler],
        format="%(message)s",
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


configure_logging()
log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────
# SYSTEM STATE  (in-process store — use Redis/DB in production)
# ─────────────────────────────────────────────────────────────────

@dataclass
class SystemState:
    positions:       Dict[str, Position] = field(default_factory=dict)
    kill_switch_active: bool = False
    aum:             float = RISK_CFG.aum_inr
    circuit_breaker_active: bool = False

    # Engines
    bs_engine:       BlackScholesEngine    = field(default_factory=BlackScholesEngine)
    hedging_engine:  DynamicHedgingEngine  = field(default_factory=DynamicHedgingEngine)
    portfolio_engine: PortfolioGreeksEngine = field(default_factory=PortfolioGreeksEngine)
    strategy_selector: StrategySelector    = field(default_factory=StrategySelector)
    vrp_validator:   VRPGateValidator      = field(default_factory=VRPGateValidator)
    slippage:        SlippageController    = field(default_factory=SlippageController)
    sizing:          SizingEngine          = field(default_factory=SizingEngine)
    data_engine:     Optional[DataEngine]  = None
    broker:          Optional[KiteBroker]  = None
    scanner:         Optional[AutoScanner] = None
    streamer:        Optional[MarketDataStreamer] = None

_system_state: Optional[SystemState] = None
_redis_client = redis.from_url(REDIS_URL, decode_responses=True)
_market_data:  Dict[str, Any] = {
    "spot":       {},
    "iv":         {},
    "spot_5m":    {},
    "volume":     {},
    "avg_volume": {},
    "india_vix":  15.0,
}
_backtest_cache: Optional[Dict[str, Any]] = None
_last_market_data: Dict[str, Any] = _market_data.copy()


def get_system_state() -> SystemState:
    global _system_state
    if _system_state is None:
        _system_state = SystemState()

        # ── PostgreSQL: Initialize tables & rebuild positions from DB ──
        try:
            from modules.db import init_db, load_all_positions
            init_db()
            recovered = load_all_positions()
            if recovered:
                _system_state.positions.update(recovered)
                log.info("startup.positions_recovered_from_db", count=len(recovered))
        except Exception as e:
            log.warning("startup.db_recovery_failed", error=str(e))

        # Initialize Kite broker if credentials are configured
        if KITE_API_KEY:
            _system_state.broker = KiteBroker()
            log.info("broker.initialized", api_key=KITE_API_KEY[:4] + "****")
            
            # If access token was provided in .env, bridge the streamer immediately
            if _system_state.broker.is_connected:
                ticker = _system_state.broker.get_ticker()
                if ticker:
                    from modules.data_stream import MarketDataStreamer
                    _system_state.streamer = MarketDataStreamer(ticker)
                    _system_state.streamer.start()
                    log.info("streamer.auto_started_from_env")
    return _system_state


def get_market_data() -> Dict[str, Any]:
    global _last_market_data
    try:
        # Pull real-time data from Redis replica
        ltp_data = _redis_client.hgetall("vrp:market_data:ltp")
        vol_data = _redis_client.hgetall("vrp:market_data:vol")
        
        market = {
            "spot": {},
            "iv": {}, # Still need IV surface logic here or separate from _last_market_data
            "spot_5m": _last_market_data.get("spot_5m", {}),
            "volume": {},
            "avg_volume": _last_market_data.get("avg_volume", {}),
            "india_vix": _last_market_data.get("india_vix", 15.0),
        }
        
        # We need token-to-symbol mapping, in a real system we'd look this up
        # For now, we will assume the caller pulls the Ltp dict to look up by token if needed
        # Or we can just return the raw token -> value mapping
        market["raw_ltp"] = {int(k): float(v) for k, v in ltp_data.items()}
        market["raw_vol"] = {int(k): float(v) for k, v in vol_data.items()}
        
        # Merge over any manual fetches done via the old /api/v1/market/live
        market["spot"].update(_last_market_data.get("spot", {}))
        
        return market
    except Exception as e:
        log.error("market_data.redis_pull_failed", error=str(e))
        return _last_market_data


def get_execution_engine() -> SystemState:
    return get_system_state()


# ─────────────────────────────────────────────────────────────────
# FLASK APP FACTORY
# ─────────────────────────────────────────────────────────────────

def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = FLASK_SECRET
    CORS(app, resources={r"/api/*": {"origins": "*"}})

    # ── Request logging middleware ────────────────────────────────
    @app.before_request
    def _log_request():
        g.request_id = str(uuid.uuid4())[:8]
        g.start_time = datetime.utcnow()
        structlog.contextvars.bind_contextvars(request_id=g.request_id)
        log.info("http.request",
                 method=request.method, path=request.path,
                 remote=request.remote_addr)

    @app.after_request
    def _log_response(response):
        duration_ms = (datetime.utcnow() - g.start_time).total_seconds() * 1000
        log.info("http.response",
                 status=response.status_code,
                 duration_ms=round(duration_ms, 1))
        return response

    # ── Error handler ─────────────────────────────────────────────
    @app.errorhandler(Exception)
    def _handle_error(exc):
        log.exception("http.unhandled_error", error=str(exc))
        return jsonify({"error": "Internal server error", "detail": str(exc)}), 500

    # ─────────────────────────────────────────────────────────────
    # ENDPOINT: Health Check
    # ─────────────────────────────────────────────────────────────
    @app.route("/api/v1/health", methods=["GET"])
    def health():
        state = get_system_state()
        return jsonify({
            "status":              "ok",
            "kill_switch_active":  state.kill_switch_active,
            "circuit_breaker":     state.circuit_breaker_active,
            "active_positions":    len([
                p for p in state.positions.values()
                if p.state == PositionState.ACTIVE
            ]),
            "aum_inr":             state.aum,
            "broker_connected":    state.broker.is_connected if state.broker else False,
            "timestamp":           datetime.utcnow().isoformat(),
        })

    # ─────────────────────────────────────────────────────────────
    # ENDPOINT: System Process Status
    # ─────────────────────────────────────────────────────────────
    @app.route("/api/v1/system/status", methods=["GET"])
    def system_status():
        """Return operational status of all system components."""
        state = get_system_state()

        # Redis status
        redis_ok = False
        try:
            redis_ok = _redis_client.ping()
        except Exception:
            pass

        # PostgreSQL status
        pg_ok = False
        try:
            from modules.db import SessionLocal
            sess = SessionLocal()
            sess.execute(__import__("sqlalchemy").text("SELECT 1"))
            sess.close()
            pg_ok = True
        except Exception:
            pass

        # Celery status
        celery_ok = False
        try:
            from modules.tasks import celery_app as _celery
            insp = _celery.control.inspect(timeout=1.0)
            active = insp.active()
            celery_ok = active is not None and len(active) > 0
        except Exception:
            pass

        # Broker
        broker_connected = state.broker.is_connected if state.broker else False
        broker_user = None
        if broker_connected and state.broker.user_profile:
            broker_user = {
                "user_id": state.broker.user_profile.get("user_id"),
                "user_name": state.broker.user_profile.get("user_name"),
            }

        return jsonify({
            "broker": {
                "status": "connected" if broker_connected else "disconnected",
                "user": broker_user,
            },
            "scanner": {
                "status": "running" if (state.scanner and state.scanner.is_running) else "stopped",
            },
            "streamer": {
                "status": "running" if (state.streamer and state.streamer.is_running) else "stopped",
            },
            "redis": {
                "status": "connected" if redis_ok else "disconnected",
            },
            "postgresql": {
                "status": "connected" if pg_ok else "disconnected",
            },
            "celery": {
                "status": "running" if celery_ok else "stopped",
            },
            "paper_trade": PAPER_TRADE,
            "kill_switch": state.kill_switch_active,
        })

    # ─────────────────────────────────────────────────────────────
    # ENDPOINT: Enter a Trade
    # ─────────────────────────────────────────────────────────────
    @app.route("/api/v1/trade/enter", methods=["POST"])
    def enter_trade():
        """
        Run the full VRP gate + strategy selection + size + place trade.

        Expected JSON body:
        {
          "symbol":        "BANKNIFTY",
          "spot":          52000.0,
          "iv_30d":        22.5,
          "rv_30d":        14.2,
          "iv_percentile": 67.0,
          "iv_rv_ratio":   1.58,
          "iv_16d_put":    26.0,
          "iv_50d_atm":    22.5,
          "straddle_spread_pct": 0.0018,    # (deprecated, ignored)
          "india_vix":     15.2,
          "beta":          1.12,
          "lot_size":      15,
          "entry_dte":     47
        }
        """
        state = get_system_state()

        if state.kill_switch_active:
            return jsonify({"error": "Kill switch is active — no new trades"}), 403

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        # Guard: max concurrent positions
        active_count = sum(
            1 for p in state.positions.values()
            if p.state == PositionState.ACTIVE
        )
        if active_count >= RISK_CFG.max_concurrent_positions:
            return jsonify({
                "error": f"Max concurrent positions ({RISK_CFG.max_concurrent_positions}) reached"
            }), 409

        # Guard: duplicate symbol — only one active position per underlying allowed
        incoming_symbol = data.get("symbol", "")
        if incoming_symbol:
            dup = [
                p for p in state.positions.values()
                if p.symbol == incoming_symbol and p.state == PositionState.ACTIVE
            ]
            if dup:
                return jsonify({
                    "error": (
                        f"Duplicate blocked: an active position for '{incoming_symbol}' "
                        f"already exists ({dup[0].position_id}). "
                        "Close or roll the existing position before opening a new one."
                    )
                }), 409

        # Guard: circuit-breaker — tighten VRP gate after drawdown month
        vrp_validator = VRPGateValidator(tightened=state.circuit_breaker_active)

        # Guard: GICS sector concentration cap (≤ 3 positions per sector)
        incoming_sector = data.get("sector", "")
        if incoming_sector:
            sector_count = sum(
                1 for p in state.positions.values()
                if p.state == PositionState.ACTIVE
                and getattr(p, "sector", "") == incoming_sector
            )
            if sector_count >= RISK_CFG.max_positions_per_sector:
                return jsonify({
                    "error": (
                        f"Sector concentration limit reached: {sector_count} active positions "
                        f"in '{incoming_sector}' (max {RISK_CFG.max_positions_per_sector})"
                    )
                }), 409

        # Guard: pairwise correlation check — if avg corr > 0.65, block entry
        # In production: pass returns_matrix from DataEngine; here we check the flag
        corr_breach = data.get("avg_pairwise_corr", 0.0)
        if corr_breach > RISK_CFG.max_avg_pairwise_corr:
            return jsonify({
                "error": (
                    f"Portfolio correlation {corr_breach:.2f} exceeds "
                    f"{RISK_CFG.max_avg_pairwise_corr} threshold. "
                    "Reduce gross notional by 30% or wait for correlation to normalize."
                ),
                "action_required": "reduce_gross_notional_30pct",
            }), 409

        # Build IV surface from request data
        surface = IVSurface(
            symbol         = data["symbol"],
            spot           = float(data["spot"]),
            iv_30d         = float(data["iv_30d"]),
            rv_30d         = float(data["rv_30d"]),
            iv_rank        = float(data.get("iv_rank", 50.0)),
            iv_percentile  = float(data["iv_percentile"]),
            put_skew_ratio = float(data["iv_16d_put"]) / float(data["iv_50d_atm"]),
            iv_rv_ratio    = float(data["iv_rv_ratio"]),
            iv_rv_spread   = float(data["iv_30d"]) - float(data["rv_30d"]),
        )

        # VRP Gate Validation — uses tightened threshold if circuit-breaker is active
        gate_result = vrp_validator.validate(surface)
        if not gate_result.passed:
            return jsonify({
                "error":    "VRP gate failed",
                "failures": gate_result.failures,
                "gate": {
                    "iv_rv_spread":  round(gate_result.iv_rv_spread, 2),
                    "ivp":           round(gate_result.ivp, 1),
                    "iv_rv_ratio":   round(gate_result.iv_rv_ratio, 2),
                },
            }), 422

        # Strategy Selection
        india_vix = float(data.get("india_vix", 15.0))
        strategy_type, short_delta, wing_delta = state.strategy_selector.select(
            surface, india_vix
        )

        # Slippage edge check (simplified; full check per-leg at execution)
        expected_credit  = float(data.get("expected_credit", 0.0))
        transaction_cost = float(data.get("transaction_cost", 0.0))
        if expected_credit > 0 and transaction_cost > 0:
            if not state.slippage.check_min_edge(expected_credit, transaction_cost):
                return jsonify({
                    "error": "Insufficient edge — credit < 2× transaction costs"
                }), 422

        # Create Position
        position_id = str(uuid.uuid4())[:12]
        position = Position(
            position_id  = position_id,
            symbol       = data["symbol"],
            strategy     = strategy_type,
            state        = PositionState.ACTIVE,
            entry_spot   = float(data["spot"]),
            entry_iv     = float(data["iv_30d"]),
            beta         = float(data.get("beta", 1.0)),
            max_profit   = expected_credit * float(data.get("lot_size", 1)),
        )
        # Persist sector for concentration cap checks on future entries
        position.sector = incoming_sector
        state.positions[position_id] = position

        # ── Persist to PostgreSQL ──────────────────────────────────
        try:
            from modules.db import persist_position, persist_order_event
            persist_position(position)
            persist_order_event(
                position_id=position_id,
                event_type="position.opened",
                action=strategy_type.value,
                details={"short_delta": short_delta, "wing_delta": wing_delta},
            )
        except Exception as e:
            log.error("db.persist_on_entry_failed", error=str(e))

        order_log.log_event(
            "position.opened",
            position_id   = position_id,
            symbol        = data["symbol"],
            strategy      = strategy_type.value,
            short_delta   = short_delta,
            wing_delta    = wing_delta,
            india_vix     = india_vix,
            iv_rv_spread  = round(surface.iv_rv_spread, 2),
            ivp           = round(surface.iv_percentile, 1),
        )

        # Sub to streamer
        if state.streamer and state.broker:
            try:
                # Need to resolve Kite instrument token for the symbol
                # For an option leg this is more complex, but for the underlying spot:
                underlying = UNDERLYING_INSTRUMENTS.get(data["symbol"])
                if underlying:
                    # e.g., "NSE:NIFTY BANK"
                    parts = underlying.split(":")
                    if len(parts) == 2:
                        inst_token = state.broker.get_instrument_token(
                            exchange=parts[0],
                            tradingsymbol=parts[1]
                        )
                    else:
                        inst_token = state.broker.get_instrument_token(
                            exchange=SYMBOL_EXCHANGE.get(data["symbol"], "NFO"),
                            tradingsymbol=data["symbol"]
                        )
                    
                    if inst_token:
                        state.streamer.subscribe([inst_token])
                        log.info("streamer.subscribed_new_position", symbol=data["symbol"], token=inst_token)
            except Exception as e:
                log.error("streamer.subscribe_failed", symbol=data["symbol"], error=str(e))

        log.info("trade.entered",
                 position_id=position_id, strategy=strategy_type.value)

        return jsonify({
            "status":       "position_opened",
            "position_id":  position_id,
            "strategy":     strategy_type.value,
            "short_delta":  short_delta,
            "wing_delta":   wing_delta,
            "vrp_gate":     {
                "iv_rv_spread":    round(surface.iv_rv_spread, 2),
                "ivp":             round(surface.iv_percentile, 1),
                "iv_rv_ratio":     round(surface.iv_rv_ratio, 2),
                "tightened_mode":  state.circuit_breaker_active,
            },
        }), 201

    # ─────────────────────────────────────────────────────────────
    # ENDPOINT: List Positions
    # ─────────────────────────────────────────────────────────────
    @app.route("/api/v1/positions", methods=["GET"])
    def list_positions():
        state = get_system_state()
        market = get_market_data()

        # ── Live refresh: update current_price + Greeks for active positions ─
        active_positions = [
            p for p in state.positions.values()
            if p.state == PositionState.ACTIVE and p.legs
        ]

        if active_positions and state.broker and state.broker.is_connected:
            # 1. Build batch LTP request — one call for all option legs + underlyings
            leg_keys = {}        # "NFO:SYMBOL" -> list of (pos, leg)
            underlying_keys = set()
            for pos in active_positions:
                # Underlying spot price key (NSE equity or NSE index)
                if pos.symbol in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
                    ul_key = f"NSE:{pos.symbol} 50" if pos.symbol == "NIFTY" else f"NSE:{pos.symbol}"
                else:
                    ul_key = f"NSE:{pos.symbol}"
                underlying_keys.add(ul_key)

                for leg in pos.legs:
                    key = f"{leg.exchange}:{leg.symbol}"
                    leg_keys.setdefault(key, []).append((pos, leg))

            try:
                # Batch fetch: option LTPs + underlying spots in one call
                all_keys = list(leg_keys.keys()) + list(underlying_keys)
                quotes = state.broker.get_quote(all_keys)

                # 2. Extract underlying spot prices
                spot_prices = {}
                for uk in underlying_keys:
                    q = quotes.get(uk, {})
                    ltp = q.get("last_price")
                    if ltp and ltp > 0:
                        # Derive the symbol name from the key
                        sym = uk.split(":")[1]
                        if sym == "NIFTY 50":
                            sym = "NIFTY"
                        elif sym == "NIFTY BANK":
                            sym = "BANKNIFTY"
                        spot_prices[sym] = ltp

                # 3. Update each leg's current_price and Greeks
                for key, pairs in leg_keys.items():
                    q = quotes.get(key, {})
                    live_ltp = q.get("last_price")
                    if live_ltp is None or live_ltp <= 0:
                        continue

                    for pos, leg in pairs:
                        leg.current_price = live_ltp

                        # Recompute Greeks with live spot
                        spot = spot_prices.get(pos.symbol, pos.entry_spot)
                        T = leg.dte / 365.0
                        if T > 0 and spot > 0:
                            try:
                                # Derive IV from live option price
                                impl_vol = state.bs_engine.implied_vol(
                                    S=spot, K=leg.strike, T=T,
                                    market_price=live_ltp,
                                    option_type=leg.option_type,
                                )
                                if impl_vol > 0:
                                    leg.greeks = state.bs_engine.greeks(
                                        S=spot, K=leg.strike, T=T,
                                        sigma=impl_vol,
                                        option_type=leg.option_type,
                                    )
                            except Exception:
                                pass  # Keep existing greeks if recompute fails

                log.debug("positions.live_refresh",
                          legs_updated=sum(len(v) for v in leg_keys.values()),
                          spots=spot_prices)

            except Exception as e:
                log.warning("positions.live_refresh_failed", error=str(e))

        # ── Build response ──────────────────────────────────────────
        positions_data = []
        for pos in state.positions.values():
            positions_data.append({
                "position_id":     pos.position_id,
                "symbol":          pos.symbol,
                "strategy":        pos.strategy.value,
                "state":           pos.state.value,
                "entry_time":      pos.entry_time.isoformat(),
                "entry_spot":      pos.entry_spot,
                "entry_iv":        pos.entry_iv,
                "min_dte":         pos.min_dte,
                "net_pnl":         round(pos.net_pnl, 2),
                "profit_pct":      round(pos.profit_pct * 100, 1),
                "portfolio_delta": round(pos.portfolio_delta, 4),
                "portfolio_vega":  round(pos.portfolio_vega, 4),
                "legs_count":      len(pos.legs),
            })
        return jsonify({"positions": positions_data, "count": len(positions_data)})

    # ─────────────────────────────────────────────────────────────
    # ENDPOINT: Position Execution Details (Legs + Order Events)
    # ─────────────────────────────────────────────────────────────
    @app.route("/api/v1/positions/<position_id>/details", methods=["GET"])
    def position_details(position_id):
        state = get_system_state()
        pos = state.positions.get(position_id)

        if not pos:
            return jsonify({"error": "Position not found"}), 404

        # Fetch order events from database
        events_data = []
        try:
            from modules.db import SessionLocal, OrderEventRecord
            session = SessionLocal()
            try:
                events = (
                    session.query(OrderEventRecord)
                    .filter_by(position_id=position_id)
                    .order_by(OrderEventRecord.timestamp.desc())
                    .all()
                )
                events_data = [
                    {
                        "event_type": e.event_type,
                        "action": e.action,
                        "leg_symbol": e.leg_symbol,
                        "fill_price": e.fill_price,
                        "order_id": e.order_id,
                        "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                        "details": json.loads(e.details_json) if e.details_json else {},
                    }
                    for e in events
                ]
            finally:
                session.close()
        except Exception as db_err:
            log.warning("position_details.db_unavailable", position_id=position_id, error=str(db_err))

        # Build legs data
        legs_data = [
            {
                "symbol": leg.symbol,
                "strike": leg.strike,
                "expiry": leg.expiry.isoformat() if isinstance(leg.expiry, date) else leg.expiry,
                "option_type": leg.option_type.value if hasattr(leg.option_type, 'value') else leg.option_type,
                "is_long": leg.is_long,
                "lots": leg.lots,
                "lot_size": leg.lot_size,
                "entry_price": leg.entry_price,
                "current_price": leg.current_price,
                "delta": round(leg.greeks.delta, 4) if leg.greeks else None,
                "gamma": round(leg.greeks.gamma, 4) if leg.greeks else None,
                "vega": round(leg.greeks.vega, 4) if leg.greeks else None,
                "theta": round(leg.greeks.theta, 4) if leg.greeks else None,
            }
            for leg in pos.legs
        ]

        # Compute lots / lot_size from first leg (all legs share the same)
        total_lots = pos.legs[0].lots if pos.legs else 0
        lot_size = pos.legs[0].lot_size if pos.legs else 0

        # Estimate margin used: for IC/spread = wing width * lot_size * lots
        # For strangles = approximate notional
        margin_used = 0.0
        short_legs = [l for l in pos.legs if not l.is_long]
        long_legs = [l for l in pos.legs if l.is_long]
        if long_legs and short_legs:
            # Defined-risk: margin ≈ max wing width * lot_size * lots
            max_width = 0
            for sl in short_legs:
                for ll in long_legs:
                    if sl.option_type == ll.option_type:
                        max_width = max(max_width, abs(sl.strike - ll.strike))
            margin_used = max_width * lot_size * total_lots
        elif short_legs:
            # Naked/strangle: approximate margin as 5x credit
            margin_used = pos.max_profit * 5 if pos.max_profit > 0 else 0

        max_profit_pct = (pos.max_profit / margin_used * 100) if margin_used > 0 else 0.0

        return jsonify({
            "position_id": pos.position_id,
            "symbol": pos.symbol,
            "strategy": pos.strategy.value if hasattr(pos.strategy, 'value') else pos.strategy,
            "state": pos.state.value if hasattr(pos.state, 'value') else pos.state,
            "entry_time": pos.entry_time.isoformat() if pos.entry_time else None,
            "entry_spot": pos.entry_spot,
            "entry_iv": pos.entry_iv,
            "max_profit": pos.max_profit,
            "max_profit_pct": round(max_profit_pct, 1),
            "net_pnl": round(pos.net_pnl, 2),
            "profit_pct": round(pos.profit_pct * 100, 1),
            "lots": total_lots,
            "lot_size": lot_size,
            "margin_used": round(margin_used, 2),
            "legs": legs_data,
            "events": events_data,
        })

    # ─────────────────────────────────────────────────────────────
    # ENDPOINT: Portfolio Greeks
    # ─────────────────────────────────────────────────────────────
    @app.route("/api/v1/portfolio/greeks", methods=["GET"])
    def portfolio_greeks():
        state     = get_system_state()
        market    = get_market_data()
        nifty_spot = market.get("spot", {}).get("NIFTY", 22000.0)
        india_vix  = market.get("india_vix", 15.0)

        active = [p for p in state.positions.values() if p.state == PositionState.ACTIVE]

        bwd = state.portfolio_engine.beta_weighted_delta(active, nifty_spot, state.aum)
        total_vega = state.portfolio_engine.total_vega(active)
        stress_pnl = state.portfolio_engine.vix_stress_pnl(active, india_vix)
        stress_pct = stress_pnl / state.aum if state.aum else 0.0

        return jsonify({
            "active_positions":         len(active),
            "beta_weighted_delta":      round(bwd, 2),
            "delta_limit":              f"±{RISK_CFG.portfolio_delta_limit_per_cr} per ₹1Cr",
            "delta_within_limit":       abs(bwd) <= RISK_CFG.portfolio_delta_limit_per_cr,
            "total_portfolio_vega":     round(total_vega, 2),
            "layer1_vega_limit_ok":     abs(total_vega) <= abs(
                state.aum * RISK_CFG.layer1_vix_pt_drawdown_pct * 100
            ),
            "layer2_stress_pnl":        round(stress_pnl, 0),
            "layer2_stress_pct_aum":    round(stress_pct * 100, 2),
            "layer2_within_limit":      abs(stress_pct) <= RISK_CFG.layer2_vix_double_max_loss_pct,
            "india_vix":                india_vix,
            "nifty_spot":               nifty_spot,
            "aum_inr":                  state.aum,
        })

    # ─────────────────────────────────────────────────────────────
    # ENDPOINT: Risk Stress Test
    # ─────────────────────────────────────────────────────────────
    @app.route("/api/v1/risk/stress-test", methods=["GET"])
    def stress_test():
        state     = get_system_state()
        market    = get_market_data()
        india_vix = float(request.args.get("vix", market.get("india_vix", 15.0)))
        vix_mult  = float(request.args.get("vix_multiplier", 2.0))
        stressed_vix = india_vix * vix_mult

        active = [p for p in state.positions.values() if p.state == PositionState.ACTIVE]
        stress_pnl = state.portfolio_engine.vix_stress_pnl(active, india_vix, stressed_vix)
        stress_pct = stress_pnl / state.aum if state.aum else 0.0

        return jsonify({
            "scenario":          f"VIX {india_vix:.1f} → {stressed_vix:.1f} (+{vix_mult:.1f}×)",
            "estimated_pnl":     round(stress_pnl, 0),
            "pct_of_aum":        round(stress_pct * 100, 2),
            "max_allowed_pct":   RISK_CFG.layer2_vix_double_max_loss_pct * 100,
            "breach":            abs(stress_pct) > RISK_CFG.layer2_vix_double_max_loss_pct,
            "note": (
                "Linear vega approximation — actual loss larger due to "
                "volga (vega convexity). Use as floor, not ceiling."
            ),
        })

    # ─────────────────────────────────────────────────────────────
    # ENDPOINT: Manual Adjustment Trigger
    # ─────────────────────────────────────────────────────────────
    @app.route("/api/v1/trade/adjust", methods=["POST"])
    def manual_adjust():
        """
        Manually trigger an evaluation + adjustment for a specific position.
        Useful for operator override during unusual market conditions.
        """
        state = get_system_state()
        if state.kill_switch_active:
            return jsonify({"error": "Kill switch active"}), 403

        data        = request.get_json(silent=True) or {}
        position_id = data.get("position_id")
        pos = state.positions.get(position_id)

        if not pos:
            return jsonify({"error": "Position not found"}), 404

        market = get_market_data()
        decision = state.hedging_engine.evaluate_position(
            position      = pos,
            current_spot  = float(data.get("spot", market.get("spot", {}).get(pos.symbol, 0))),
            current_iv    = float(data.get("iv",   market.get("iv",   {}).get(pos.symbol, pos.entry_iv))),
            india_vix     = float(data.get("vix",  market.get("india_vix", 15.0))),
            spot_5min_ago = float(data.get("spot_5m", 0)),
            volume_now    = int(data.get("volume", 0)),
            avg_volume    = int(data.get("avg_volume", 1)),
        )

        if decision.action.value != "none":
            from modules.tasks import execute_adjustment
            execute_adjustment.delay(
                position_id  = position_id,
                action       = decision.action.value,
                leg_symbol   = decision.leg_symbol,
                target_delta = decision.target_delta,
            )

        return jsonify({
            "position_id": position_id,
            "action":      decision.action.value,
            "reason":      decision.reason,
            "queued":      decision.action.value != "none",
        })

    # ─────────────────────────────────────────────────────────────
    # ENDPOINT: Internal Native Execution (Bypass Celery)
    # ─────────────────────────────────────────────────────────────
    @app.route("/api/v1/internal/execute_adjustment", methods=["POST"])
    def internal_execute_adjustment():
        """
        Executes adjustment synchronously without Celery. 
        Intended to be called asynchronously via aiohttp by async_engine.py.
        """
        data = request.get_json(silent=True) or {}
        from modules.tasks import _do_adjustment
        
        result = _do_adjustment(
            position_id  = data.get("position_id"),
            action       = data.get("action"),
            leg_symbol   = data.get("leg_symbol"),
            target_delta = data.get("target_delta"),
            details      = data.get("details"),
        )
        return jsonify(result)
        
    @app.route("/api/v1/internal/execute_position_exit", methods=["POST"])
    def internal_execute_position_exit():
        """
        Executes position exit synchronously without Celery.
        """
        data = request.get_json(silent=True) or {}
        from modules.tasks import _do_position_exit
        
        result = _do_position_exit(
            position_id=data.get("position_id"),
            reason=data.get("reason"),
        )
        return jsonify(result)

    # ─────────────────────────────────────────────────────────────
    # ENDPOINT: KILL SWITCH  🔴
    # ─────────────────────────────────────────────────────────────
    @app.route("/api/v1/kill-switch", methods=["POST"])
    def kill_switch():
        """
        EMERGENCY ENDPOINT — Immediately:
          1. Activates the kill switch flag (blocks all new trades)
          2. Cancels ALL pending/open orders via broker API
          3. Flattens ALL active positions (market order for speed, not limit-chase)
          4. Emits structured kill_switch log events for every action
          5. Returns a full summary of all positions closed

        Authorization: Requires X-Kill-Switch-Token header matching env var.
        """
        state = get_system_state()

        # ── Authorization ────────────────────────────────────────
        auth_token    = request.headers.get("X-Kill-Switch-Token", "")
        expected_token = os.getenv("KILL_SWITCH_TOKEN", "")
        if expected_token and auth_token != expected_token:
            log.warning("kill_switch.unauthorized",
                        remote=request.remote_addr)
            return jsonify({"error": "Unauthorized — invalid kill switch token"}), 401

        # ── Activate Kill Switch ─────────────────────────────────
        state.kill_switch_active = True
        activated_at = datetime.utcnow().isoformat()

        order_log.log_event(
            "kill_switch.activated",
            activated_by = request.headers.get("X-Operator-ID", "unknown"),
            remote_addr  = request.remote_addr,
            activated_at = activated_at,
            reason       = request.get_json(silent=True, force=True).get("reason", "manual")
            if request.content_type == "application/json" else "manual",
        )

        log.critical(
            "KILL_SWITCH_ACTIVATED",
            activated_at = activated_at,
            active_positions = len([
                p for p in state.positions.values()
                if p.state == PositionState.ACTIVE
            ]),
        )

        # ── Cancel Pending Celery Tasks ──────────────────────────
        try:
            celery_app.control.purge()
            log.info("kill_switch.celery_queue_purged")
        except Exception as e:
            log.error("kill_switch.celery_purge_failed", error=str(e))

        # ── Stop Scanner ───────────────────────────────────────────
        if state.scanner and state.scanner.is_running:
            state.scanner.stop()
            log.info("kill_switch.scanner_stopped")

        # ── Flatten All Active Positions ─────────────────────────
        closed_positions = []
        failed_positions = []

        for pos in list(state.positions.values()):
            if pos.state not in (PositionState.ACTIVE, PositionState.ADJUSTING):
                continue

            try:
                pos.state = PositionState.CLOSING

                leg_actions = []
                for leg in pos.legs:
                    side = "sell" if leg.is_long else "buy"

                    if not PAPER_TRADE and state.broker and state.broker.is_connected:
                        # LIVE: Use MARKET ORDER for emergency speed
                        try:
                            tx_type = "SELL" if leg.is_long else "BUY"
                            order_id = state.broker.place_order(
                                tradingsymbol=leg.symbol,
                                exchange="NFO",
                                transaction_type=tx_type,
                                quantity=leg.lots * leg.lot_size,
                                order_type="MARKET",
                                product="NRML",
                            )
                            fill_price = leg.current_price  # Approximate; actual fill TBD
                            log.info("kill_switch.order_placed",
                                     order_id=order_id, leg=leg.symbol,
                                     tx_type=tx_type, order_type="MARKET")
                        except Exception as e:
                            log.error("kill_switch.order_failed",
                                      leg=leg.symbol, error=str(e))
                            fill_price = leg.current_price
                    else:
                        # PAPER: Simulated fill
                        fill_price = leg.current_price

                    leg_actions.append({
                        "leg_symbol":  leg.symbol,
                        "side":        side,
                        "lots":        leg.lots,
                        "fill_price":  fill_price,
                        "order_type":  "MARKET",
                    })

                    order_log.log_event(
                        "order.filled",
                        position_id  = pos.position_id,
                        leg_symbol   = leg.symbol,
                        side         = side,
                        fill_price   = fill_price,
                        order_type   = "MARKET",
                        triggered_by = "kill_switch",
                    )

                pos.state = PositionState.CLOSED
                net_pnl   = pos.net_pnl

                # ── Persist to PostgreSQL ──────────────────────────────
                try:
                    from modules.db import persist_position, persist_order_event
                    persist_position(pos)
                    persist_order_event(
                        position_id=pos.position_id,
                        event_type="position.closed",
                        action="kill_switch",
                        details={"net_pnl": round(net_pnl, 2), "legs": leg_actions},
                    )
                except Exception as e:
                    log.error("db.persist_kill_switch_close_failed",
                              position_id=pos.position_id, error=str(e))

                order_log.log_event(
                    "position.closed",
                    position_id  = pos.position_id,
                    symbol       = pos.symbol,
                    strategy     = pos.strategy.value,
                    net_pnl      = round(net_pnl, 2),
                    triggered_by = "kill_switch",
                )

                closed_positions.append({
                    "position_id": pos.position_id,
                    "symbol":      pos.symbol,
                    "strategy":    pos.strategy.value,
                    "net_pnl":     round(net_pnl, 2),
                    "legs":        leg_actions,
                })

            except Exception as exc:
                log.error("kill_switch.position_close_failed",
                          position_id=pos.position_id, error=str(exc))
                failed_positions.append({
                    "position_id": pos.position_id,
                    "error":       str(exc),
                })

        total_pnl = sum(p["net_pnl"] for p in closed_positions)

        order_log.log_event(
            "kill_switch.completed",
            closed_count  = len(closed_positions),
            failed_count  = len(failed_positions),
            total_pnl     = round(total_pnl, 2),
            completed_at  = datetime.utcnow().isoformat(),
        )

        log.critical(
            "KILL_SWITCH_COMPLETED",
            closed  = len(closed_positions),
            failed  = len(failed_positions),
            total_pnl = round(total_pnl, 2),
        )

        return jsonify({
            "status":           "kill_switch_executed",
            "activated_at":     activated_at,
            "completed_at":     datetime.utcnow().isoformat(),
            "positions_closed": len(closed_positions),
            "positions_failed": len(failed_positions),
            "total_pnl":        round(total_pnl, 2),
            "closed_positions": closed_positions,
            "failed_positions": failed_positions,
            "warning": (
                "All positions have been flattened using MARKET ORDERS. "
                "Review fills before re-engaging the system."
            ),
        })

    # ─────────────────────────────────────────────────────────────
    # ENDPOINT: Reset Kill Switch (operator must explicitly re-arm)
    # ─────────────────────────────────────────────────────────────
    @app.route("/api/v1/kill-switch/reset", methods=["POST"])
    def kill_switch_reset():
        state = get_system_state()
        auth_token     = request.headers.get("X-Kill-Switch-Token", "")
        expected_token = os.getenv("KILL_SWITCH_TOKEN", "")
        if expected_token and auth_token != expected_token:
            return jsonify({"error": "Unauthorized"}), 401

        state.kill_switch_active = False
        order_log.log_event(
            "kill_switch.reset",
            reset_by    = request.headers.get("X-Operator-ID", "unknown"),
            reset_at    = datetime.utcnow().isoformat(),
        )
        log.warning("kill_switch.reset")
        return jsonify({"status": "kill_switch_disarmed", "trading_enabled": True})

    # ─────────────────────────────────────────────────────────────
    # ENDPOINT: Circuit Breaker (after 4% monthly drawdown)
    # ─────────────────────────────────────────────────────────────
    @app.route("/api/v1/risk/circuit-breaker", methods=["POST"])
    def circuit_breaker():
        """
        Activates the post-drawdown circuit breaker per v2 spec:
          - Reduces max concurrent positions by 50%
          - Raises VRP gate threshold to IV-RV > 7 vol points
          - Clears automatically when caller POSTs with {"activate": false}

        POST body: {"activate": true, "monthly_drawdown_pct": 4.2, "reason": "..."}
        """
        state = get_system_state()
        data  = request.get_json(silent=True) or {}
        activate = data.get("activate", True)

        state.circuit_breaker_active = activate

        if activate:
            monthly_dd = float(data.get("monthly_drawdown_pct", 0))
            if monthly_dd < RISK_CFG.monthly_drawdown_trigger_pct * 100:
                return jsonify({
                    "error": (
                        f"Drawdown {monthly_dd:.1f}% is below the "
                        f"{RISK_CFG.monthly_drawdown_trigger_pct*100:.0f}% trigger threshold"
                    )
                }), 422

            order_log.log_event(
                "risk.circuit_breaker",
                action             = "activated",
                monthly_drawdown_pct = monthly_dd,
                new_max_positions  = int(RISK_CFG.max_concurrent_positions * RISK_CFG.cb_position_count_reduction),
                tightened_iv_rv_gate = VRP_GATE.tightened_iv_rv_spread,
                reason             = data.get("reason", "monthly_drawdown_limit"),
            )
            log.warning("circuit_breaker.activated",
                        drawdown_pct=monthly_dd,
                        tightened_spread=VRP_GATE.tightened_iv_rv_spread)
        else:
            order_log.log_event("risk.circuit_breaker", action="deactivated")
            log.info("circuit_breaker.deactivated")

        return jsonify({
            "circuit_breaker_active": state.circuit_breaker_active,
            "effect": {
                "max_concurrent_positions": (
                    int(RISK_CFG.max_concurrent_positions * RISK_CFG.cb_position_count_reduction)
                    if activate else RISK_CFG.max_concurrent_positions
                ),
                "min_iv_rv_spread_required": (
                    VRP_GATE.tightened_iv_rv_spread if activate
                    else VRP_GATE.min_iv_rv_spread_pts
                ),
            },
        })

    # ─────────────────────────────────────────────────────────────
    # BROKER: Kite Connect Login / Callback / Status
    # ─────────────────────────────────────────────────────────────
    @app.route("/api/v1/broker/login", methods=["GET"])
    def broker_login():
        """Return the Zerodha Kite OAuth login URL."""
        state = get_system_state()
        if not state.broker:
            return jsonify({"error": "Broker not configured — set KITE_API_KEY in .env"}), 503
        return jsonify({"login_url": state.broker.get_login_url()})

    @app.route("/api/v1/broker/callback", methods=["GET"])
    def broker_callback():
        """
        Kite Connect redirects here after successful login.
        Exchanges request_token for access_token, then redirects to dashboard.
        """
        state = get_system_state()
        if not state.broker:
            return jsonify({"error": "Broker not configured"}), 503

        request_token = request.args.get("request_token")
        if not request_token:
            return jsonify({"error": "Missing request_token parameter"}), 400

        try:
            session_data = state.broker.generate_session(request_token)
            log.info("broker.login_success",
                     user_id=session_data.get("user_id"),
                     user_name=session_data.get("user_name"))

            # Auto-start the scanner after successful broker login
            if state.scanner is None:
                state.scanner = AutoScanner(broker=state.broker, system_state=state)
            state.scanner.start()

            # Auto-start the WebSocket streamer
            if state.streamer is None:
                ticker = state.broker.get_ticker()
                if ticker:
                    from modules.data_stream import MarketDataStreamer
                    state.streamer = MarketDataStreamer(ticker)
            if state.streamer and not state.streamer.is_running:
                state.streamer.start()

            return redirect(url_for("login_page"))
        except Exception as exc:
            log.error("broker.login_failed", error=str(exc))
            return jsonify({"error": "Kite login failed", "detail": str(exc)}), 401

    @app.route("/api/v1/broker/status", methods=["GET"])
    def broker_status():
        """Check whether the broker session is active."""
        state = get_system_state()
        if not state.broker:
            return jsonify({
                "connected": False,
                "reason": "Broker not configured — set KITE_API_KEY in .env",
            })

        if not state.broker.is_connected:
            return jsonify({
                "connected": False,
                "login_url": state.broker.get_login_url(),
                "reason": "Not logged in — complete the Kite OAuth flow first",
            })

        profile = state.broker.user_profile or {}
        return jsonify({
            "connected": True,
            "user_id": profile.get("user_id"),
            "user_name": profile.get("user_name"),
            "broker": profile.get("broker", "ZERODHA"),
            "email": profile.get("email"),
        })

    @app.route("/api/v1/broker/disconnect", methods=["POST"])
    def broker_disconnect():
        """Disconnect from Kite — clears access token and stops scanner."""
        state = get_system_state()
        if not state.broker:
            return jsonify({"error": "Broker not configured"}), 503

        # Stop scanner if running
        if state.scanner and state.scanner.is_running:
            state.scanner.stop()
            log.info("broker.disconnect.scanner_stopped")

        # Stop streamer if running
        if state.streamer and state.streamer.is_running:
            state.streamer.stop()
            log.info("broker.disconnect.streamer_stopped")

        # Clear the access token
        state.broker.kite.set_access_token(None)
        state.broker._access_token = None
        state.broker._user_profile = None
        state.broker.clear_instruments_cache()

        log.info("broker.disconnected")
        return jsonify({"disconnected": True})

    # ─────────────────────────────────────────────────────────────
    # LIVE MARKET DATA (from Kite Connect)
    # ─────────────────────────────────────────────────────────────
    @app.route("/api/v1/market/live", methods=["GET"])
    def market_live():
        """Fetch live quotes for NIFTY, BANKNIFTY, India VIX from Kite."""
        state = get_system_state()
        if not state.broker or not state.broker.is_connected:
            return jsonify({"available": False})

        try:
            instruments = ["NSE:NIFTY 50", "NSE:NIFTY BANK", "NSE:INDIA VIX"]
            quotes = state.broker.get_quote(instruments)

            result = {}
            for inst_key, q in quotes.items():
                ohlc = q.get("ohlc", {})
                prev_close = ohlc.get("close", 0)
                last = q.get("last_price", 0)
                change = last - prev_close if prev_close else 0
                change_pct = (change / prev_close * 100) if prev_close else 0

                result[inst_key] = {
                    "last_price": last,
                    "change": round(change, 2),
                    "change_pct": round(change_pct, 2),
                    "open": ohlc.get("open", 0),
                    "high": ohlc.get("high", 0),
                    "low": ohlc.get("low", 0),
                    "close": prev_close,
                    "volume": q.get("volume", 0),
                }

            # Update the in-memory market data so greeks/stress use live values
            market = get_market_data()
            nifty = result.get("NSE:NIFTY 50", {})
            banknifty = result.get("NSE:NIFTY BANK", {})
            vix = result.get("NSE:INDIA VIX", {})
            if nifty.get("last_price"):
                market["spot"]["NIFTY"] = nifty["last_price"]
            if banknifty.get("last_price"):
                market["spot"]["BANKNIFTY"] = banknifty["last_price"]
            if vix.get("last_price"):
                market["india_vix"] = vix["last_price"]

            return jsonify({"available": True, "quotes": result})

        except Exception as exc:
            log.error("market.live_fetch_failed", error=str(exc))
            return jsonify({"available": False, "error": str(exc)})

    @app.route("/api/v1/account/overview", methods=["GET"])
    def account_overview():
        """Fetch account margins and funds from Kite."""
        state = get_system_state()
        if not state.broker or not state.broker.is_connected:
            return jsonify({"available": False})

        try:
            margins = state.broker.get_margins()
            equity = margins.get("equity", {})
            avail = equity.get("available", {})
            utilised = equity.get("utilised", {})

            net = equity.get("net", 0)
            available_cash = avail.get("live_balance", 0)
            collateral = avail.get("collateral", 0)
            used_margin = utilised.get("debits", 0)
            opening_balance = avail.get("opening_balance", 0)

            return jsonify({
                "available": True,
                "net": round(net, 2),
                "available_cash": round(available_cash, 2),
                "collateral": round(collateral, 2),
                "used_margin": round(used_margin, 2),
                "opening_balance": round(opening_balance, 2),
            })

        except Exception as exc:
            log.error("account.overview_failed", error=str(exc))
            return jsonify({"available": False, "error": str(exc)})

    @app.route("/api/v1/broker/positions", methods=["GET"])
    def broker_positions():
        """Fetch live positions from Kite."""
        state = get_system_state()
        if not state.broker or not state.broker.is_connected:
            return jsonify({"available": False})

        try:
            positions = state.broker.get_positions()
            net = positions.get("net", [])

            result = []
            total_pnl = 0.0
            for p in net:
                if p.get("quantity", 0) == 0:
                    continue
                pnl = p.get("pnl", 0)
                total_pnl += pnl
                result.append({
                    "tradingsymbol": p.get("tradingsymbol", ""),
                    "exchange": p.get("exchange", ""),
                    "quantity": p.get("quantity", 0),
                    "average_price": round(p.get("average_price", 0), 2),
                    "last_price": round(p.get("last_price", 0), 2),
                    "pnl": round(pnl, 2),
                    "product": p.get("product", ""),
                    "instrument_type": p.get("instrument_type", ""),
                })

            return jsonify({
                "available": True,
                "positions": result,
                "count": len(result),
                "total_pnl": round(total_pnl, 2),
            })

        except Exception as exc:
            log.error("broker.positions_failed", error=str(exc))
            return jsonify({"available": False, "error": str(exc)})

    # ─────────────────────────────────────────────────────────────
    # SCANNER: Auto-Entry Signal Endpoints
    # ─────────────────────────────────────────────────────────────
    @app.route("/api/v1/scanner/signals", methods=["GET"])
    def scanner_signals():
        """Return pending scanner signals for the dashboard."""
        state = get_system_state()
        if not state.scanner:
            return jsonify({
                "signals": [],
                "scanner_active": False,
                "reason": "Scanner not initialized — connect to Kite first",
            })

        pending = state.scanner.get_pending_signals()
        return jsonify({
            "signals": [s.to_dict() for s in pending],
            "scanner_active": state.scanner.is_running,
            "count": len(pending),
        })

    @app.route("/api/v1/scanner/execute/<signal_id>", methods=["POST"])
    def scanner_execute(signal_id: str):
        """Execute a pending scanner signal — place orders."""
        state = get_system_state()
        if state.kill_switch_active:
            return jsonify({"error": "Kill switch is active — no new trades"}), 403
        if not state.scanner:
            return jsonify({"error": "Scanner not initialized"}), 503

        result = state.scanner.execute_signal(signal_id)
        if "error" in result:
            return jsonify(result), 400
        return jsonify(result), 201

    @app.route("/api/v1/scanner/margin/<signal_id>", methods=["GET"])
    def scanner_margin(signal_id: str):
        """Fetch margin required for a pending signal's legs via Kite basket margin API."""
        state = get_system_state()
        if not state.broker or not state.broker.is_connected:
            return jsonify({"error": "Broker not connected"}), 503
        if not state.scanner:
            return jsonify({"error": "Scanner not initialized"}), 503

        signal = state.scanner._signals.get(signal_id)
        if not signal:
            return jsonify({"error": "Signal not found or expired"}), 404

        try:
            # Build order params for basket margin API
            orders = []
            for leg in signal.legs:
                orders.append({
                    "exchange": "NFO",
                    "tradingsymbol": leg.tradingsymbol,
                    "transaction_type": "BUY" if leg.is_long else "SELL",
                    "variety": "regular",
                    "product": "NRML",
                    "order_type": "LIMIT",
                    "quantity": signal.lot_size * signal.lots,
                    "price": leg.ltp,
                })

            basket = state.broker.get_basket_margins(orders)
            if isinstance(basket, dict) and "error" in basket:
                return jsonify({"error": basket["error"]}), 500

            # basket_order_margins returns {"initial": {}, "final": {}, "orders": [...]}
            initial = basket.get("initial", {}) if isinstance(basket, dict) else {}
            final = basket.get("final", {}) if isinstance(basket, dict) else {}

            # Also fetch available margin
            margins = state.broker.get_margins()
            equity = margins.get("equity", {})
            avail = equity.get("available", {})
            available_cash = avail.get("live_balance", 0)
            collateral = avail.get("collateral", 0)
            total_available = available_cash + collateral

            return jsonify({
                "margin_required": round(final.get("total", initial.get("total", 0)), 2),
                "initial_margin": round(initial.get("total", 0), 2),
                "final_margin": round(final.get("total", 0), 2),
                "available_margin": round(total_available, 2),
                "available_cash": round(available_cash, 2),
                "collateral": round(collateral, 2),
                "sufficient": total_available >= final.get("total", initial.get("total", 0)),
            })

        except Exception as exc:
            log.error("scanner.margin_failed", signal_id=signal_id, error=str(exc))
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/v1/scanner/dismiss/<signal_id>", methods=["POST"])
    def scanner_dismiss(signal_id: str):
        """Dismiss a pending scanner signal."""
        state = get_system_state()
        if not state.scanner:
            return jsonify({"error": "Scanner not initialized"}), 503

        result = state.scanner.dismiss_signal(signal_id)
        if "error" in result:
            return jsonify(result), 400
        return jsonify(result)

    @app.route("/api/v1/scanner/control", methods=["POST"])
    def scanner_control():
        """Start or stop the scanner. POST body: {"action": "start"|"stop"}"""
        state = get_system_state()
        data = request.get_json(silent=True) or {}
        action = data.get("action", "")

        if not state.broker or not state.broker.is_connected:
            return jsonify({"error": "Broker not connected — scanner requires live data"}), 503

        if state.scanner is None:
            state.scanner = AutoScanner(broker=state.broker, system_state=state)

        if action == "start":
            state.scanner.start()
            return jsonify({"status": "scanner_started", "active": True})
        elif action == "stop":
            state.scanner.stop()
            return jsonify({"status": "scanner_stopped", "active": False})
        else:
            return jsonify({"error": "Invalid action — use 'start' or 'stop'"}), 400

    # ─────────────────────────────────────────────────────────────
    # UI ROUTES (serve Jinja2 templates)
    # ─────────────────────────────────────────────────────────────

    # ── Login Gate: redirect unauthenticated users to /login ────
    @app.before_request
    def require_broker_login():
        """Gate all page routes behind broker auth. APIs are exempt."""
        # Exempt paths: login page, static, APIs, broker auth endpoints
        exempt_prefixes = ("/login", "/static", "/api/", "/favicon")
        if any(request.path.startswith(p) for p in exempt_prefixes):
            return None
        state = get_system_state()
        if not state.broker or not state.broker.is_connected:
            return redirect(url_for("login_page"))
        return None

    @app.route("/")
    def index():
        return redirect(url_for("login_page"))

    @app.route("/login")
    def login_page():
        """Zerodha Kite login screen."""
        state = get_system_state()
        connected = state.broker and state.broker.is_connected
        ctx = {
            "connected": connected,
            "login_url": state.broker.get_login_url() if state.broker else "#",
            "error": request.args.get("error"),
            "market_status": "Pre-Open",
        }
        if connected and state.broker.user_profile:
            ctx["user_name"] = state.broker.user_profile.get("user_name", "Trader")
            ctx["user_id"] = state.broker.user_profile.get("user_id", "")
        return render_template("login.html", **ctx)

    @app.route("/dashboard")
    def dashboard():
        return render_template("dashboard.html")

    @app.route("/positions")
    def positions_page():
        return render_template("positions.html")

    @app.route("/trade")
    def trade_entry():
        return render_template("trade_entry.html")

    @app.route("/greeks")
    def greeks_page():
        return render_template("greeks.html")

    @app.route("/stress-test")
    def stress_test_page():
        return render_template("stress_test.html")

    @app.route("/risk")
    def risk_page():
        return render_template("risk.html")

    @app.route("/settings")
    def settings_page():
        return render_template("settings.html")

    @app.route("/backtest")
    def backtest_page():
        return render_template(
            "backtest.html",
            scan_symbols=AUTO_SCAN_SYMBOLS,
            lot_sizes=LOT_SIZES,
        )

    # ─────────────────────────────────────────────────────────────
    # API: System Config (read-only, for Settings page)
    # ─────────────────────────────────────────────────────────────
    @app.route("/api/v1/config", methods=["GET"])
    def get_config():
        return jsonify({
            "vrp_gate": {
                "Index (Tier 1)": {
                    "Min IV-RV Spread": f"{VRP_GATE.min_iv_rv_spread_pts} pts",
                    "Min IV Percentile": f"{VRP_GATE.min_ivp_percentile}%",
                    "Min IV/RV Ratio": f"{VRP_GATE.min_iv_rv_ratio}",
                },
                "Stock (Tier 2)": {
                    "Min IV-RV Spread": f"{VRP_GATE.stock_min_iv_rv_spread_pts} pts",
                    "Min IV Percentile": f"{VRP_GATE.stock_min_ivp_percentile}%",
                    "Min IV/RV Ratio": f"{VRP_GATE.stock_min_iv_rv_ratio}",
                },
                "Tightened IV-RV Spread": f"{VRP_GATE.tightened_iv_rv_spread} pts",
            },
            "greeks": {
                "Short Delta Target": f"{GREEKS_CFG.short_delta_target} (16D)",
                "Long Wing Delta": f"{GREEKS_CFG.long_wing_delta} (10D)",
                "Delta Trigger": f"{GREEKS_CFG.delta_trigger} (30D)",
                "Delta Hysteresis Reset": f"{GREEKS_CFG.delta_hysteresis_reset} (20D)",
                "Portfolio Delta Band": f"\u00b1{GREEKS_CFG.portfolio_delta_band} Nifty units",
                "Roll To Delta": f"{GREEKS_CFG.roll_to_delta}",
                "Untested Roll Delta": f"{GREEKS_CFG.untested_roll_delta}",
                "Untested IV Return Threshold": f"{GREEKS_CFG.untested_iv_return_threshold * 100:.0f}%",
                "ITM Breach %": f"{GREEKS_CFG.itm_breach_pct * 100:.0f}%",
                "Delta EWMA Span": f"{GREEKS_CFG.delta_ewma_span} ticks",
            },
            "lifecycle": {
                "Entry DTE Range": f"{LIFECYCLE_CFG.entry_dte_min}-{LIFECYCLE_CFG.entry_dte_max} days",
                "Gamma Exit DTE": f"{LIFECYCLE_CFG.gamma_exit_dte} days",
                "Profit Target": f"{LIFECYCLE_CFG.profit_target_pct * 100:.0f}%",
                "Vanna IV Drop": f"{LIFECYCLE_CFG.vanna_iv_drop_pct * 100:.0f}%",
                "Vanna Price Band": f"{LIFECYCLE_CFG.vanna_price_band_pct * 100:.1f}%",
                "Nifty Intraday Rebalance": f"{LIFECYCLE_CFG.nifty_intraday_rebalance_pct * 100:.0f}%",
                "Low-Vol VIX Threshold": f"{LIFECYCLE_CFG.low_vol_vix_threshold}",
                "Low-Vol Notional Reduction": f"{LIFECYCLE_CFG.low_vol_notional_reduction * 100:.0f}%",
                "Low-Vol Short Delta": f"{LIFECYCLE_CFG.low_vol_short_delta} (12D)",
                "Low-Vol Tail Budget": f"{LIFECYCLE_CFG.low_vol_tail_budget_pct * 100:.1f}%",
            },
            "risk": {
                "AUM": f"\u20b9{RISK_CFG.aum_inr / 1e7:.1f} Cr",
                "Max Premium Risk": f"{RISK_CFG.max_premium_risk_pct * 100:.0f}% per name",
                "Max Loss Per Name": f"{RISK_CFG.max_loss_per_name_pct * 100:.0f}% AUM",
                "Max Concurrent Positions": f"{RISK_CFG.max_concurrent_positions}",
                "Max Per Sector": f"{RISK_CFG.max_positions_per_sector}",
                "Delta Limit (per Cr)": f"\u00b1{RISK_CFG.portfolio_delta_limit_per_cr}",
                "Intraday Rebalance Band": f"\u00b1{RISK_CFG.intraday_rebalance_band}",
                "L1 VIX Pt Drawdown": f"{RISK_CFG.layer1_vix_pt_drawdown_pct * 100:.2f}% AUM",
                "L2 VIX Double Max Loss": f"{RISK_CFG.layer2_vix_double_max_loss_pct * 100:.0f}% AUM",
                "Max Avg Pairwise Corr": f"{RISK_CFG.max_avg_pairwise_corr}",
                "Corr Notional Reduction": f"{RISK_CFG.corr_notional_reduction * 100:.0f}%",
                "Monthly DD Trigger": f"{RISK_CFG.monthly_drawdown_trigger_pct * 100:.0f}%",
                "CB Position Reduction": f"{RISK_CFG.cb_position_count_reduction * 100:.0f}%",
            },
            "slippage": {
                "Initial Limit Offset": f"{SLIPPAGE_CFG.initial_limit_offset_pct * 100:.1f}%",
                "Chase Step": f"{SLIPPAGE_CFG.chase_step_pct * 100:.2f}%",
                "Chase Interval": f"{SLIPPAGE_CFG.chase_interval_sec}s",
                "Max Chase Steps": f"{SLIPPAGE_CFG.max_chase_steps}",
                "Max Slippage Budget": f"{SLIPPAGE_CFG.max_slippage_budget_pct * 100:.0f}%",
                "Min Edge Multiple": f"{SLIPPAGE_CFG.min_edge_multiple}x",
            },
            "whipsaw": {
                "Cooldown Period": f"{WHIPSAW_CFG.cooldown_minutes} min",
                "Redis Prefix": WHIPSAW_CFG.redis_cooldown_prefix,
            },
            "infrastructure": {
                "Broker": BROKER,
                "Broker Connected": str(get_system_state().broker.is_connected) if get_system_state().broker else "N/A",
                "Paper Trade": str(PAPER_TRADE),
                "Flask Port": str(FLASK_PORT),
                "Redis URL": REDIS_URL,
                "Database URL": DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL,
                "MTM Monitor Interval": f"{MTM_MONITOR_INTERVAL_SEC}s",
                "Delta Monitor Interval": f"{DELTA_MONITOR_INTERVAL_SEC}s",
                "Celery Timezone": CELERY_TIMEZONE,
                "Skew Threshold": f"{SKEW_CFG.put_skew_ratio_threshold}",
            },
        })

    # ─────────────────────────────────────────────────────────────
    # API: Backtest
    # ─────────────────────────────────────────────────────────────

    @app.route("/api/v1/backtest/run", methods=["POST"])
    def run_backtest():
        """Run VRP strategy backtest over historical data."""
        global _backtest_cache
        state = get_system_state()

        if not state.broker or not state.broker.is_connected:
            return jsonify({
                "error": "Broker not connected. Log in to Kite first to access historical data."
            }), 503

        data = request.get_json(silent=True) or {}
        mode = data.get("mode", "single")
        symbol = data.get("symbol", "NIFTY")

        config = BacktestConfig()
        config.symbol = symbol
        if "initial_aum" in data:
            config.initial_aum = float(data["initial_aum"])
        if "start_date" in data:
            from datetime import date as _date
            config.start_date = _date.fromisoformat(data["start_date"])
        if "end_date" in data:
            from datetime import date as _date
            config.end_date = _date.fromisoformat(data["end_date"])

        try:
            if mode == "batch":
                from config import BACKTEST_STOCK_SYMBOLS
                config.symbols = BACKTEST_STOCK_SYMBOLS
                engine = BacktestBatchEngine(broker=state.broker, config=config)
            else:
                engine = BacktestEngine(broker=state.broker, config=config)

            result = engine.run()

            _backtest_cache = result.to_dict()
            _backtest_cache["available"] = True

            log.info(
                "backtest.completed",
                mode=mode,
                run_time=round(result.run_time_sec, 2),
            )

            return jsonify({
                "status": "completed",
                "mode": mode,
                "run_time_sec": round(result.run_time_sec, 2),
                "summary": _backtest_cache["summary"],
            })

        except Exception as exc:
            log.error("backtest.failed", mode=mode, error=str(exc))
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/v1/backtest/results", methods=["GET"])
    def backtest_results():
        """Return cached backtest results (full data for charts and tables)."""
        if _backtest_cache is None or not _backtest_cache.get("available"):
            return jsonify({"available": False})
        return jsonify(_backtest_cache)

    # ─────────────────────────────────────────────────────────────
    # NIFTY 50 IV METRICS SCREEN  (background scan + cache)
    # ─────────────────────────────────────────────────────────────

    _n100_cache: Dict[str, Any] = {
        "metrics": [],
        "vix": None,
        "timestamp": None,
        "status": "idle",         # idle | scanning | ready
        "progress": 0,
        "total_symbols": len(NIFTY50_SYMBOLS),
    }
    _n100_lock = threading.Lock()

    def _run_nifty100_scan(state):
        """Background thread: scan all symbols with rate limiting."""
        with _n100_lock:
            if _n100_cache["status"] == "scanning":
                return  # Already running
            _n100_cache["status"] = "scanning"
            _n100_cache["progress"] = 0

        try:
            if state.scanner is None:
                state.scanner = AutoScanner(broker=state.broker, system_state=state)

            symbols = list(NIFTY50_SYMBOLS)
            results: List[Dict[str, Any]] = []

            # Fetch VIX once
            india_vix = None
            try:
                vix_ltp = state.broker.get_ltp(["NSE:INDIA VIX"])
                india_vix = vix_ltp.get("NSE:INDIA VIX", {}).get("last_price")
            except Exception:
                pass

            from config import UNDERLYING_INSTRUMENTS, SYMBOL_SECTOR

            for i, symbol in enumerate(symbols):
                try:
                    inst_key = UNDERLYING_INSTRUMENTS.get(symbol)
                    if not inst_key:
                        continue

                    ltp = state.broker.get_ltp([inst_key])
                    spot = ltp.get(inst_key, {}).get("last_price", 0)
                    if spot <= 0:
                        continue

                    time.sleep(0.35)  # Rate limit: ~3 req/s

                    expiry = state.scanner._get_target_expiry(symbol)
                    if not expiry:
                        continue

                    options = state.broker.find_options(symbol, expiry)
                    if not options:
                        continue

                    surface = state.scanner._build_iv_surface(symbol, spot, expiry)
                    if not surface:
                        continue

                    vrp_result = state.scanner.vrp_validator.validate(
                        surface
                    )

                    # Strategy selection (only meaningful when VRP gate passes)
                    strategy = None
                    if vrp_result.passed and india_vix is not None:
                        strat_type, short_d, wing_d = state.scanner.strategy_selector.select(
                            surface, india_vix
                        )
                        strategy = strat_type.value

                    results.append({
                        "symbol": symbol,
                        "sector": SYMBOL_SECTOR.get(symbol, ""),
                        "spot": round(spot, 2),
                        "iv_30d": round(surface.iv_30d, 1),
                        "rv_30d": round(surface.rv_30d, 1),
                        "iv_rv_spread": round(surface.iv_rv_spread, 2),
                        "iv_percentile": round(surface.iv_percentile, 1),
                        "iv_rv_ratio": round(surface.iv_rv_ratio, 2),
                        "put_skew": round(surface.put_skew_ratio, 2),
                        "call_skew": round(surface.call_skew_ratio, 2),
                        "vrp_gate_passed": vrp_result.passed,
                        "vrp_failures": vrp_result.failures,
                        "strategy": strategy,
                    })

                    time.sleep(0.35)  # Rate limit between symbols

                except Exception as e:
                    log.warning("n100.symbol_failed", symbol=symbol, error=str(e))

                # Update progress (live)
                with _n100_lock:
                    _n100_cache["progress"] = i + 1
                    _n100_cache["metrics"] = list(results)
                    _n100_cache["vix"] = round(india_vix, 2) if india_vix is not None else None
                    _n100_cache["timestamp"] = datetime.utcnow().isoformat()

            with _n100_lock:
                _n100_cache["metrics"] = results
                _n100_cache["vix"] = round(india_vix, 2) if india_vix is not None else None
                _n100_cache["status"] = "ready"
                _n100_cache["timestamp"] = datetime.utcnow().isoformat()

            log.info("n100.scan_complete", count=len(results),
                     total=len(symbols))

        except Exception as e:
            log.error("n100.scan_failed", error=str(e))
            with _n100_lock:
                _n100_cache["status"] = "ready"  # Mark ready so UI isn't stuck

    @app.route("/nifty100")
    def nifty100_page():
        return render_template("nifty100.html")

    @app.route("/api/v1/metrics/nifty100", methods=["GET"])
    def nifty100_metrics():
        """
        Return live IV metrics for all NIFTY100 derivative scripts.
        First call triggers a background scan; subsequent calls return cached data.
        Cache auto-refreshes every 2 minutes.
        """
        state = get_system_state()

        if not state.broker or not state.broker.is_connected:
            return jsonify({
                "error": "Broker not connected — Kite login required for live data",
                "metrics": [],
                "vix": None,
                "status": "disconnected",
                "timestamp": datetime.utcnow().isoformat(),
            }), 503

        # Check if we need a fresh scan (first time or stale > 2 min)
        need_scan = False
        with _n100_lock:
            if _n100_cache["status"] == "idle":
                need_scan = True
            elif _n100_cache["status"] == "ready" and _n100_cache["timestamp"]:
                from datetime import timezone
                age = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(_n100_cache["timestamp"]).replace(
                           tzinfo=timezone.utc
                       )).total_seconds()
                if age > 120:
                    need_scan = True

        if need_scan:
            thread = threading.Thread(
                target=_run_nifty100_scan, args=(state,), daemon=True
            )
            thread.start()

        # Force rescan
        if request.args.get("refresh") == "1":
            with _n100_lock:
                if _n100_cache["status"] != "scanning":
                    thread = threading.Thread(
                        target=_run_nifty100_scan, args=(state,), daemon=True
                    )
                    thread.start()

        with _n100_lock:
            return jsonify({
                "metrics": _n100_cache["metrics"],
                "count": len(_n100_cache["metrics"]),
                "total_symbols": _n100_cache["total_symbols"],
                "vix": _n100_cache["vix"],
                "status": _n100_cache["status"],
                "progress": _n100_cache["progress"],
                "timestamp": _n100_cache["timestamp"] or datetime.utcnow().isoformat(),
            })

    # ── NIFTY 50 IV Metrics – Trade Detail Builder ──────────────
    @app.route("/api/v1/metrics/nifty100/trade-details", methods=["POST"])
    def nifty100_trade_details():
        """
        Compute full trade details (legs, credit, max loss, sizing) for a symbol.
        Expects JSON: {"symbol": "RELIANCE"}
        Returns PendingSignal dict with legs, strikes, deltas, LTPs.
        """
        state = get_system_state()

        if state.kill_switch_active:
            return jsonify({"error": "Kill switch is active"}), 403

        if not state.broker or not state.broker.is_connected:
            return jsonify({"error": "Broker not connected"}), 503

        if state.scanner is None:
            state.scanner = AutoScanner(broker=state.broker, system_state=state)

        data = request.get_json(silent=True) or {}
        symbol = data.get("symbol", "").upper().strip()

        if not symbol or symbol not in NIFTY50_SYMBOLS:
            return jsonify({"error": f"Invalid symbol: {symbol}"}), 400

        try:
            from config import UNDERLYING_INSTRUMENTS, SYMBOL_SECTOR

            inst_key = UNDERLYING_INSTRUMENTS.get(symbol)
            if not inst_key:
                return jsonify({"error": f"No instrument mapping for {symbol}"}), 400

            # 1. Spot price
            ltp = state.broker.get_ltp([inst_key])
            spot = ltp.get(inst_key, {}).get("last_price", 0)
            if spot <= 0:
                return jsonify({"error": f"Could not get spot price for {symbol}"}), 400

            # 2. Expiry + option chain
            expiry = state.scanner._get_target_expiry(symbol)
            if not expiry:
                return jsonify({"error": f"No valid expiry found for {symbol}"}), 400

            options = state.broker.find_options(symbol, expiry)
            if not options:
                return jsonify({"error": f"No options chain for {symbol}"}), 400

            # 3. IV surface
            surface = state.scanner._build_iv_surface(symbol, spot, expiry)
            if not surface:
                return jsonify({"error": f"Could not build IV surface for {symbol}"}), 400

            # 4. Re-validate VRP gate (real-time check)
            vrp_result = state.scanner.vrp_validator.validate(surface)
            if not vrp_result.passed:
                return jsonify({
                    "error": "VRP gate no longer passes for this symbol",
                    "failures": vrp_result.failures,
                }), 400

            # 5. Strategy selection
            vix_ltp = state.broker.get_ltp(["NSE:INDIA VIX"])
            india_vix = vix_ltp.get("NSE:INDIA VIX", {}).get("last_price", 15.0)
            strategy, short_delta, wing_delta = state.scanner.strategy_selector.select(
                surface, india_vix
            )

            # 6. Only strategies with _find_strikes support
            supported = {StrategyType.IRON_CONDOR, StrategyType.STRANGLE, StrategyType.PUT_SPREAD}
            if strategy not in supported:
                return jsonify({
                    "error": f"Strategy '{strategy.value}' does not have automated strike selection yet",
                    "strategy": strategy.value,
                }), 400

            # 7. Find strikes (includes LTP fetch)
            legs = state.scanner._find_strikes(
                symbol, spot, expiry, short_delta, wing_delta, strategy, options
            )
            if not legs:
                return jsonify({"error": "Could not find suitable strikes"}), 400

            # Detect iron condor downgrade: selected IC but got only short legs
            long_legs = [l for l in legs if l.is_long]
            if strategy == StrategyType.IRON_CONDOR and not long_legs:
                strategy = StrategyType.STRANGLE

            # 8. Credit / max loss / sizing (mirrors scanner._scan_symbol)
            lot_size = LOT_SIZES.get(symbol, 1)
            short_legs = [l for l in legs if not l.is_long]
            estimated_credit = sum(l.ltp for l in short_legs) - sum(l.ltp for l in long_legs)

            if strategy == StrategyType.IRON_CONDOR and long_legs:
                call_width = put_width = 0
                for sl in short_legs:
                    for ll in long_legs:
                        if sl.option_type == ll.option_type == "CE":
                            call_width = abs(ll.strike - sl.strike)
                        elif sl.option_type == ll.option_type == "PE":
                            put_width = abs(sl.strike - ll.strike)
                max_width = max(call_width, put_width)
                max_loss_per_lot = (max_width - estimated_credit) * lot_size
                # If credit exceeds wing width, use wing width as floor
                if max_loss_per_lot <= 0:
                    max_loss_per_lot = max_width * lot_size
            elif strategy == StrategyType.PUT_SPREAD and long_legs:
                width = abs(short_legs[0].strike - long_legs[0].strike)
                max_loss_per_lot = (width - estimated_credit) * lot_size
                if max_loss_per_lot <= 0:
                    max_loss_per_lot = width * lot_size
            else:
                max_loss_per_lot = estimated_credit * lot_size * 5

            lots = state.scanner.sizing.compute_lots(
                aum=state.aum,
                max_loss_inr=max_loss_per_lot if max_loss_per_lot > 0 else lot_size * spot * 0.05,
                lot_size=lot_size,
            )

            # 9. Create PendingSignal and store for execution
            signal = PendingSignal(
                signal_id=str(uuid.uuid4())[:12],
                symbol=symbol,
                strategy=strategy,
                short_delta=short_delta,
                wing_delta=wing_delta,
                vrp_gate={
                    "iv_rv_spread": round(vrp_result.iv_rv_spread, 2),
                    "ivp": round(vrp_result.ivp, 1),
                    "iv_rv_ratio": round(vrp_result.iv_rv_ratio, 2),
                },
                surface_data={
                    "iv_30d": round(surface.iv_30d, 1),
                    "rv_30d": round(surface.rv_30d, 1),
                    "put_skew": round(surface.put_skew_ratio, 2),
                    "spot": round(spot, 2),
                },
                legs=legs,
                lots=lots,
                lot_size=lot_size,
                estimated_credit=round(estimated_credit * lot_size * lots, 2),
                max_loss=round(max_loss_per_lot * lots, 2),
                sector=SYMBOL_SECTOR.get(symbol, ""),
            )
            state.scanner._signals[signal.signal_id] = signal

            log.info("n100.trade_details_built",
                     signal_id=signal.signal_id, symbol=symbol,
                     strategy=strategy.value, lots=lots)

            return jsonify(signal.to_dict()), 200

        except Exception as e:
            log.error("n100.trade_details_failed", symbol=symbol, error=str(e))
            return jsonify({"error": str(e)}), 500

    return app


# ─────────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────────

app = create_app()

if __name__ == "__main__":
    log.info(
        "vrp_system.starting",
        host=FLASK_HOST,
        port=FLASK_PORT,
        debug=DEBUG,
        broker=BROKER,
    )
    app.run(
        host=FLASK_HOST,
        port=FLASK_PORT,
        debug=DEBUG,
        threaded=True,
    )
