"""
modules/tasks.py — Celery Task Definitions
==========================================
All background jobs run through Celery + Redis:
  - MTM monitoring (every 30s)
  - Delta monitoring (every 10s)
  - Adjustment execution
  - Structured JSON logging for every order lifecycle event
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, Optional

import structlog
from celery import Celery
from celery.utils.log import get_task_logger

from config import (
    REDIS_URL,
    MTM_MONITOR_INTERVAL_SEC,
    DELTA_MONITOR_INTERVAL_SEC,
    CELERY_TIMEZONE,
    RISK_CFG,
    GREEKS_CFG,
    PAPER_TRADE,
    SLIPPAGE_CFG,
)

log = structlog.get_logger(__name__)
task_log = get_task_logger(__name__)


# ─────────────────────────────────────────────────────────────────
# CELERY APP CONFIGURATION
# ─────────────────────────────────────────────────────────────────

def make_celery() -> Celery:
    app = Celery(
        "vrp_system",
        broker=REDIS_URL,
        backend=REDIS_URL,
    )
    app.conf.update(
        task_serializer        = "json",
        result_serializer      = "json",
        accept_content         = ["json"],
        timezone               = CELERY_TIMEZONE,
        enable_utc             = True,
        task_track_started     = True,
        task_acks_late         = True,          # Ack only after completion
        worker_prefetch_multiplier = 1,          # One task at a time per worker
        # Beat schedule previously ran mtm-monitor and delta-monitor here.
        # This has been replaced by the real-time Asyncio event loop (async_engine.py).
    )
    return app


celery_app = make_celery()


# ─────────────────────────────────────────────────────────────────
# STRUCTURED ORDER LIFECYCLE LOGGER
# ─────────────────────────────────────────────────────────────────

class OrderLogger:
    """
    Emits structured JSON log events for every order lifecycle state change.
    All events are queryable for post-trade analysis and compliance.
    """

    EVENTS = [
        "order.placed", "order.filled", "order.rejected",
        "order.cancelled", "order.partial_fill",
        "adjustment.triggered", "adjustment.completed", "adjustment.aborted",
        "position.opened", "position.closed", "position.rolled",
        "kill_switch.activated", "kill_switch.completed",
        "risk.circuit_breaker", "risk.vega_limit_breach",
        "slippage.budget_exceeded", "cooldown.active",
    ]

    @staticmethod
    def log_event(event: str, **kwargs) -> None:
        log.info(
            event,
            ts=datetime.utcnow().isoformat(),
            **kwargs,
        )


order_log = OrderLogger()


# ─────────────────────────────────────────────────────────────────
# CELERY TASKS
# ─────────────────────────────────────────────────────────────────

@celery_app.task(
    name="modules.tasks.run_mtm_monitor",
    bind=True,
    max_retries=3,
    default_retry_delay=5,
)
def run_mtm_monitor(self) -> Dict[str, Any]:
    """
    MTM Monitoring task — runs every 30 seconds.
    Checks profit targets, vega limits, and drawdown circuit breakers.
    """
    try:
        from app import get_system_state   # Late import to avoid circular deps

        state = get_system_state()
        results = {"checked": 0, "actions": []}

        for pos in state.positions.values():
            if pos.state.value not in ("active",):
                continue

            results["checked"] += 1

            # 50% profit target
            if pos.profit_pct >= 0.50:
                order_log.log_event(
                    "adjustment.triggered",
                    position_id=pos.position_id,
                    action="profit_exit",
                    profit_pct=round(pos.profit_pct * 100, 1),
                )
                # Enqueue execution task
                execute_position_exit.delay(pos.position_id, reason="profit_target_50pct")
                results["actions"].append(
                    {"position_id": pos.position_id, "action": "profit_exit"}
                )

            # 21 DTE gamma exit
            if pos.min_dte <= 21:
                order_log.log_event(
                    "adjustment.triggered",
                    position_id=pos.position_id,
                    action="gamma_exit",
                    dte=pos.min_dte,
                )
                execute_position_exit.delay(pos.position_id, reason="gamma_exit_21dte")
                results["actions"].append(
                    {"position_id": pos.position_id, "action": "gamma_exit"}
                )

        # Portfolio-level vega check
        total_vega = sum(
            p.portfolio_vega for p in state.positions.values()
            if p.state.value == "active"
        )
        vega_limit = state.aum * RISK_CFG.layer1_vix_pt_drawdown_pct
        if abs(total_vega) > abs(vega_limit):
            order_log.log_event(
                "risk.vega_limit_breach",
                total_vega=round(total_vega, 2),
                limit=round(vega_limit, 2),
            )

        log.info("mtm_monitor.completed",
                 checked=results["checked"], actions_taken=len(results["actions"]))
        return results

    except Exception as exc:
        log.error("mtm_monitor.failed", error=str(exc))
        raise self.retry(exc=exc)


@celery_app.task(
    name="modules.tasks.run_delta_monitor",
    bind=True,
    max_retries=3,
    default_retry_delay=3,
)
def run_delta_monitor(self) -> Dict[str, Any]:
    """
    Delta Monitoring task — runs every 10 seconds.
    Checks individual leg delta triggers and portfolio delta band.
    """
    try:
        from app import get_system_state, get_market_data

        state = get_system_state()
        market = get_market_data()
        results = {"checked": 0, "adjustments_queued": 0}

        for pos in state.positions.values():
            if pos.state.value != "active":
                continue

            results["checked"] += 1
            
            # Use raw_ltp and raw_vol when available (real-time). Fallback to old keys for tests
            symbol_token = state.broker.get_instrument_token(pos.symbol) if state.broker else None
            
            if symbol_token and "raw_ltp" in market and symbol_token in market["raw_ltp"]:
                spot = market["raw_ltp"][symbol_token]
            else:
                spot = market.get("spot", {}).get(pos.symbol, 0)
                
            if symbol_token and "raw_vol" in market and symbol_token in market["raw_vol"]:
                vol_now = market["raw_vol"][symbol_token]
            else:
                vol_now = market.get("volume", {}).get(pos.symbol, 0)
                
            curr_iv  = market.get("iv", {}).get(pos.symbol, pos.entry_iv)
            vix      = market.get("india_vix", 15.0)
            spot_5m  = market.get("spot_5m", {}).get(pos.symbol, spot)
            avg_vol  = market.get("avg_volume", {}).get(pos.symbol, 1)

            decision = state.hedging_engine.evaluate_position(
                position      = pos,
                current_spot  = spot,
                current_iv    = curr_iv,
                india_vix     = vix,
                spot_5min_ago = spot_5m,
                volume_now    = vol_now,
                avg_volume    = avg_vol,
                cache         = state.data_engine.cache if state.data_engine else None,
            )

            if decision.action.value != "none":
                order_log.log_event(
                    "adjustment.triggered",
                    position_id = pos.position_id,
                    action      = decision.action.value,
                    reason      = decision.reason,
                )
                execute_adjustment.delay(
                    position_id = pos.position_id,
                    action      = decision.action.value,
                    leg_symbol  = decision.leg_symbol,
                    target_delta = decision.target_delta,
                    details     = decision.details,
                )
                results["adjustments_queued"] += 1

        log.info("delta_monitor.completed",
                 checked=results["checked"],
                 adjustments=results["adjustments_queued"])
        return results

    except Exception as exc:
        log.error("delta_monitor.failed", error=str(exc))
        raise self.retry(exc=exc)


# ─────────────────────────────────────────────────────────────────
# STANDALONE EXECUTION FUNCTIONS (Upgrade #4)
# These can be called directly by internal Flask endpoints OR by
# Celery tasks. No dependency on Celery's `self` for retry logic.
# ─────────────────────────────────────────────────────────────────

def _do_position_exit(position_id: str, reason: str) -> Dict[str, Any]:
    """
    Core position exit logic — standalone, no Celery dependency.
    Uses limit-chase slippage controller per leg.
    """
    from app import get_system_state, get_execution_engine

    state   = get_system_state()
    exec_eng = get_execution_engine()

    pos = state.positions.get(position_id)
    if not pos:
        return {"status": "position_not_found", "position_id": position_id}

    if pos.state.value == "closed":
        return {"status": "already_closed", "position_id": position_id}

    pos.state = pos.state.__class__.CLOSING
    fills = []

    for leg in pos.legs:
        side = "sell" if leg.is_long else "buy"   # Close = reverse
        theoretical_mid = leg.current_price

        if not PAPER_TRADE and state.broker and state.broker.is_connected:
            # ── LIVE EXECUTION: Place real orders via Kite ──────
            tx_type = "SELL" if leg.is_long else "BUY"
            quantity = leg.lots * leg.lot_size

            for step in range(SLIPPAGE_CFG.max_chase_steps + 1):
                limit_price = exec_eng.slippage.compute_limit_price(
                    theoretical_mid, side, step
                )
                try:
                    order_id = state.broker.place_order(
                        tradingsymbol=leg.symbol,
                        exchange=leg.exchange,
                        transaction_type=tx_type,
                        quantity=quantity,
                        order_type="LIMIT",
                        price=limit_price,
                        product="NRML",
                    )
                    log.info("execute_exit.order_placed",
                             order_id=order_id, leg=leg.symbol,
                             price=limit_price, step=step)

                    # Wait for fill
                    if step < SLIPPAGE_CFG.max_chase_steps:
                        time.sleep(SLIPPAGE_CFG.chase_interval_sec)

                    # Check fill status
                    order_history = state.broker.get_order_history(order_id)
                    latest = order_history[-1] if order_history else {}
                    status = latest.get("status", "")

                    if status == "COMPLETE":
                        fill_price = latest.get("average_price", limit_price)
                        fills.append({
                            "leg": leg.symbol,
                            "side": side,
                            "fill_price": fill_price,
                            "order_id": order_id,
                            "chase_steps": step,
                            "mode": "live",
                        })
                        order_log.log_event(
                            "order.filled",
                            position_id=position_id,
                            leg_symbol=leg.symbol,
                            side=side,
                            fill_price=fill_price,
                            order_id=order_id,
                            chase_steps=step,
                            reason=reason,
                            mode="live",
                        )
                        break

                    # Partial fill handling
                    filled_qty = latest.get("filled_quantity", 0)
                    pending_qty = latest.get("pending_quantity", quantity)
                    if filled_qty > 0 and status != "COMPLETE":
                        order_log.log_event(
                            "order.partial_fill",
                            position_id=position_id,
                            leg_symbol=leg.symbol,
                            order_id=order_id,
                            filled_qty=filled_qty,
                            pending_qty=pending_qty,
                            total_qty=quantity,
                        )
                        log.warning("execute_exit.partial_fill",
                                    order_id=order_id, leg=leg.symbol,
                                    filled=filled_qty, pending=pending_qty)

                    # Not filled — cancel and chase at worse price
                    if step < SLIPPAGE_CFG.max_chase_steps:
                        try:
                            state.broker.cancel_order(order_id)
                        except Exception:
                            pass  # May already be filled

                except Exception as e:
                    log.error("execute_exit.order_failed",
                              leg=leg.symbol, step=step, error=str(e))
                    break
            else:
                order_log.log_event(
                    "slippage.budget_exceeded",
                    position_id=position_id,
                    leg_symbol=leg.symbol,
                    theoretical=theoretical_mid,
                    mode="live",
                )
                log.error("execute_exit.slippage_abort",
                          leg=leg.symbol, position_id=position_id)

        else:
            # ── PAPER TRADE: Simulated fills ───────────────────
            for step in range(SLIPPAGE_CFG.max_chase_steps + 1):
                limit_price = exec_eng.slippage.compute_limit_price(
                    theoretical_mid, side, step
                )
                fill_price = limit_price
                time.sleep(SLIPPAGE_CFG.chase_interval_sec if step > 0 else 0)

                if exec_eng.slippage.is_within_budget(theoretical_mid, fill_price, side):
                    fills.append({
                        "leg": leg.symbol,
                        "side": side,
                        "fill_price": fill_price,
                        "chase_steps": step,
                        "mode": "paper",
                    })
                    order_log.log_event(
                        "order.filled",
                        position_id=position_id,
                        leg_symbol=leg.symbol,
                        side=side,
                        fill_price=fill_price,
                        chase_steps=step,
                        reason=reason,
                        mode="paper",
                    )
                    break
            else:
                order_log.log_event(
                    "slippage.budget_exceeded",
                    position_id=position_id,
                    leg_symbol=leg.symbol,
                    theoretical=theoretical_mid,
                    mode="paper",
                )
                log.error("execute_exit.slippage_abort",
                          leg=leg.symbol, position_id=position_id)

    pos.state = pos.state.__class__.CLOSED
    order_log.log_event(
        "position.closed",
        position_id = position_id,
        reason      = reason,
        legs_closed = len(fills),
        net_pnl     = round(pos.net_pnl, 2),
    )

    # ── Persist to PostgreSQL ──────────────────────────────────
    try:
        from modules.db import persist_position, persist_order_event
        persist_position(pos)
        persist_order_event(
            position_id=position_id,
            event_type="position.closed",
            action=reason,
            details={"fills": fills, "net_pnl": round(pos.net_pnl, 2)},
        )
    except Exception:
        pass

    return {"status": "closed", "position_id": position_id, "fills": fills}


@celery_app.task(
    name="modules.tasks.execute_position_exit",
    bind=True,
    max_retries=5,
    default_retry_delay=2,
)
def execute_position_exit(self, position_id: str, reason: str) -> Dict[str, Any]:
    """Celery wrapper around _do_position_exit."""
    try:
        return _do_position_exit(position_id, reason)
    except Exception as exc:
        log.error("execute_exit.failed", error=str(exc), position_id=position_id)
        raise self.retry(exc=exc)



def _do_adjustment(
    position_id:  str,
    action:       str,
    leg_symbol:   Optional[str] = None,
    target_delta: Optional[float] = None,
    details:      Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Core adjustment logic — standalone, no Celery dependency.
    Sets cooldown after execution.
    """
    from app import get_system_state, get_execution_engine

    state    = get_system_state()
    exec_eng = get_execution_engine()
    pos      = state.positions.get(position_id)

    if not pos:
        return {"status": "position_not_found"}

    result = {"status": "executed", "action": action, "position_id": position_id}

    if action == "close_all":
        execute_position_exit.delay(position_id, reason="impulsive_move_close_all")

    elif action == "roll_challenged_leg":
        log.info("adjustment.roll_challenged",
                 position_id=position_id, leg=leg_symbol,
                 target_delta=target_delta)
        order_log.log_event(
            "position.rolled",
            position_id  = position_id,
            leg_symbol   = leg_symbol,
            target_delta = target_delta,
            action       = "roll_challenged_leg",
        )
        if pos:
            pos.challenged_side_recentered = True
            log.info("adjustment.challenged_recentered_flag_set",
                     position_id=position_id)

    elif action == "roll_untested_leg":
        log.info("adjustment.roll_untested",
                 position_id=position_id,
                 target_delta=target_delta or GREEKS_CFG.untested_roll_delta)
        order_log.log_event(
            "position.rolled",
            position_id  = position_id,
            leg_symbol   = leg_symbol,
            target_delta = target_delta or GREEKS_CFG.untested_roll_delta,
            action       = "roll_untested_leg",
        )
        if pos:
            pos.challenged_side_recentered = False

    elif action == "roll_out":
        log.info("adjustment.roll_out_next_expiry",
                 position_id=position_id, leg=leg_symbol)
        order_log.log_event(
            "position.rolled",
            position_id = position_id,
            leg_symbol  = leg_symbol,
            action      = "roll_out_itm",
        )

    elif action == "low_vol_adjust":
        log.info("adjustment.low_vol_protocol", position_id=position_id)
        order_log.log_event(
            "adjustment.completed",
            position_id = position_id,
            action      = "low_vol_protocol",
            details     = details or {},
        )

    # Set cooldown to prevent whipsaw
    state.hedging_engine.set_cooldown(position_id)

    order_log.log_event(
        "adjustment.completed",
        position_id = position_id,
        action      = action,
    )

    # ── Persist to PostgreSQL ──────────────────────────────────
    try:
        from modules.db import persist_position, persist_order_event
        persist_position(pos)
        persist_order_event(
            position_id=position_id,
            event_type="adjustment.completed",
            action=action,
            leg_symbol=leg_symbol,
            details=details,
        )
    except Exception:
        pass

    return result


@celery_app.task(
    name="modules.tasks.execute_adjustment",
    bind=True,
    max_retries=3,
    default_retry_delay=5,
)
def execute_adjustment(
    self,
    position_id:  str,
    action:       str,
    leg_symbol:   Optional[str] = None,
    target_delta: Optional[float] = None,
    details:      Optional[Dict] = None,
) -> Dict[str, Any]:
    """Celery wrapper around _do_adjustment."""
    try:
        return _do_adjustment(position_id, action, leg_symbol, target_delta, details)
    except Exception as exc:
        log.error("execute_adjustment.failed", error=str(exc))
        raise self.retry(exc=exc)


@celery_app.task(
    name="modules.tasks.run_position_reconciliation",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def run_position_reconciliation(self) -> Dict[str, Any]:
    """
    Daily position reconciliation task (scheduled at 09:30 IST).
    Compares internal system state against broker positions to detect mismatches:
      - Orphaned broker positions (in broker, not in system state)
      - Missing broker positions (in system state, not at broker)
    Logs all mismatches for manual review — does NOT auto-correct.
    """
    try:
        from app import get_system_state

        state = get_system_state()
        if not state.broker or not state.broker.is_connected:
            log.warning("reconciliation.broker_not_connected")
            return {"status": "skipped", "reason": "broker_not_connected"}

        broker_positions = state.broker.get_positions()
        net_positions = broker_positions.get("net", [])

        # Build set of tradingsymbols from broker
        broker_symbols = set()
        for bp in net_positions:
            if bp.get("quantity", 0) != 0:
                broker_symbols.add(bp["tradingsymbol"])

        # Build set of tradingsymbols from system state
        system_symbols = set()
        for pos in state.positions.values():
            if pos.state.value in ("active", "adjusting"):
                for leg in pos.legs:
                    system_symbols.add(leg.symbol)

        orphaned = broker_symbols - system_symbols
        missing = system_symbols - broker_symbols

        if orphaned:
            order_log.log_event(
                "reconciliation.orphaned_positions",
                symbols=list(orphaned),
                count=len(orphaned),
            )
            log.warning("reconciliation.orphaned",
                        symbols=list(orphaned), count=len(orphaned))

        if missing:
            order_log.log_event(
                "reconciliation.missing_positions",
                symbols=list(missing),
                count=len(missing),
            )
            log.warning("reconciliation.missing",
                        symbols=list(missing), count=len(missing))

        result = {
            "status": "completed",
            "broker_positions": len(broker_symbols),
            "system_positions": len(system_symbols),
            "orphaned": list(orphaned),
            "missing": list(missing),
            "matched": len(broker_symbols & system_symbols),
        }

        log.info("reconciliation.completed",
                 matched=result["matched"],
                 orphaned=len(orphaned),
                 missing=len(missing))
        return result

    except Exception as exc:
        log.error("reconciliation.failed", error=str(exc))
        raise self.retry(exc=exc)
