"""
async_engine.py — High-Frequency Event Loop
=============================================
Replaces the Celery interval polling (`run_delta_monitor`, `run_mtm_monitor`)
with an asyncio event loop that subscribes directly to Redis Pub/Sub tick updates.
"""
import asyncio
import json
from typing import Any, Dict

import aiohttp
import redis.asyncio as redis
import structlog

from config import REDIS_URL, MTM_MONITOR_INTERVAL_SEC
from engine import PositionState
# Initialize structlog similar to app.py
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.JSONRenderer()
    ],
    logger_factory=structlog.PrintLoggerFactory(),
)
log = structlog.get_logger(__name__)


class AsyncTradeEngine:
    def __init__(self):
        self.redis: redis.Redis | None = None
        self.pubsub: redis.client.PubSub | None = None
        self.CHANNEL = "vrp:market_data:ticks"
        
        # We need the synchronous state to check things
        # For simplicity, we can reuse the system state from app.py
        from app import get_system_state
        self.state = get_system_state()

    async def connect(self):
        self.redis = redis.from_url(REDIS_URL, decode_responses=True)
        self.pubsub = self.redis.pubsub()
        await self.pubsub.subscribe(self.CHANNEL)
        log.info("async_engine.connected", channel=self.CHANNEL)

    async def _dispatch_flask_async(self, endpoint: str, payload: dict):
        """Native execute a task without blocking the loop and without Celery queueing."""
        url = f"http://127.0.0.1:5000{endpoint}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    resp_data = await resp.json()
                    log.info("async_engine.dispatch_success", endpoint=endpoint, status=resp.status, response=resp_data)
        except Exception as e:
            log.error("async_engine.dispatch_failed", endpoint=endpoint, error=str(e))

    async def fetch_market_data(self) -> Dict[str, Any]:
        """Fetch current full market data natively via async Redis."""
        ltp_data = await self.redis.hgetall("vrp:market_data:ltp")
        vol_data = await self.redis.hgetall("vrp:market_data:vol")
        
        market = {
            "spot": {},
            "iv": {},
            "spot_5m": {},
            "volume": {},
            "avg_volume": {},
            "india_vix": 15.0, # Will need a stream for this or a fetcher
            "raw_ltp": {int(k): float(v) for k, v in ltp_data.items()},
            "raw_vol": {int(k): float(v) for k, v in vol_data.items()}
        }
        return market

    async def evaluate_delta(self, market: Dict[str, Any], token: int, last_price: float):
        """Evaluate positions related to a specific token tick."""
        results = {"checked": 0, "adjustments_queued": 0}

        for pos in self.state.positions.values():
            if pos.state.value != "active":
                continue

            results["checked"] += 1
            spot     = market["spot"].get(pos.symbol, 0) or last_price
            curr_iv  = market["iv"].get(pos.symbol, pos.entry_iv)
            vix      = market.get("india_vix", 15.0)
            spot_5m  = market["spot_5m"].get(pos.symbol, spot)
            vol_now  = market["volume"].get(pos.symbol, 0)
            avg_vol  = market["avg_volume"].get(pos.symbol, 1)

            # ── LIVE GREEKS RECOMPUTATION (Upgrade #2) ────────────
            # Recompute Greeks with fresh spot price on every tick.
            # The Cython engine runs in ~5μs per leg — negligible cost.
            for leg in pos.legs:
                try:
                    T = leg.dte / 365.0
                    if T > 0 and spot > 0:
                        fresh_greeks = self.state.bs_engine.greeks(
                            S=spot, K=leg.strike, T=T,
                            sigma=curr_iv / 100.0 if curr_iv > 1 else curr_iv,
                            option_type=leg.option_type,
                        )
                        leg.greeks = fresh_greeks
                except Exception:
                    pass  # Keep stale greeks if recompute fails

            # Evaluate (Sync call fast enough due to Cython, but could wrap in to_thread if heavy)
            decision = self.state.hedging_engine.evaluate_position(
                position      = pos,
                current_spot  = spot,
                current_iv    = curr_iv,
                india_vix     = vix,
                spot_5min_ago = spot_5m,
                volume_now    = vol_now,
                avg_volume    = avg_vol,
                cache         = self.state.data_engine.cache if self.state.data_engine else None,
            )

            if decision.action.value != "none":
                from modules.tasks import order_log
                
                order_log.log_event(
                    "adjustment.triggered",
                    position_id = pos.position_id,
                    action      = decision.action.value,
                    reason      = decision.reason,
                )
                
                # NATIVE EXECUTION: Bypass Celery task queues for maximum lowest-latency execution
                payload = {
                    "position_id": pos.position_id,
                    "action": decision.action.value,
                    "leg_symbol": decision.leg_symbol,
                    "target_delta": decision.target_delta,
                    "details": decision.details,
                }
                asyncio.create_task(self._dispatch_flask_async("/api/v1/internal/execute_adjustment", payload))
                
                results["adjustments_queued"] += 1

        if results["adjustments_queued"] > 0:
            log.info("async_engine.delta_evaluated", checked=results["checked"], adjustments=results["adjustments_queued"])

    async def tick_listener(self):
        """Listen infinitely to the Redis PubSub Channel for ticking market data."""
        log.info("async_engine.listener_started")
        async for message in self.pubsub.listen():
            if message["type"] == "message":
                try:
                    payload = json.loads(message["data"])
                    token = payload.get("token")
                    last_price = payload.get("last_price")
                    
                    if token is not None and last_price is not None:
                        market = await self.fetch_market_data()
                        await self.evaluate_delta(market, token, last_price)
                        
                except Exception as e:
                    log.error("async_engine.tick_process_error", error=str(e))

    async def mtm_monitor_loop(self):
        """Background loop to replace the run_mtm_monitor Celery beat task."""
        log.info("async_engine.mtm_loop_started", interval_sec=MTM_MONITOR_INTERVAL_SEC)
        while True:
            try:
                results = {"checked": 0, "actions": []}
                from modules.tasks import order_log
                from config import RISK_CFG
                
                for pos in self.state.positions.values():
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
                        asyncio.create_task(
                            self._dispatch_flask_async("/api/v1/internal/execute_position_exit", 
                                                       {"position_id": pos.position_id, "reason": "profit_target_50pct"})
                        )
                        results["actions"].append({"position_id": pos.position_id, "action": "profit_exit"})

                    # 21 DTE gamma exit
                    if pos.min_dte <= 21:
                        order_log.log_event(
                            "adjustment.triggered",
                            position_id=pos.position_id,
                            action="gamma_exit",
                            dte=pos.min_dte,
                        )
                        asyncio.create_task(
                            self._dispatch_flask_async("/api/v1/internal/execute_position_exit", 
                                                       {"position_id": pos.position_id, "reason": "gamma_exit_21dte"})
                        )
                        results["actions"].append({"position_id": pos.position_id, "action": "gamma_exit"})

                # Portfolio-level vega check
                total_vega = sum(p.portfolio_vega for p in self.state.positions.values() if p.state.value == "active")
                vega_limit = self.state.aum * RISK_CFG.layer1_vix_pt_drawdown_pct * 100
                if abs(total_vega) > abs(vega_limit):
                    order_log.log_event(
                        "risk.vega_limit_breach",
                        total_vega=round(total_vega, 2),
                        limit=round(vega_limit, 2),
                    )

                if results["actions"]:
                    log.info("async_engine.mtm_check", checked=results["checked"], actions_taken=len(results["actions"]))
            
            except Exception as e:
                log.error("async_engine.mtm_loop_error", error=str(e))
                
            await asyncio.sleep(MTM_MONITOR_INTERVAL_SEC)

    async def update_yield_curve_loop(self):
        """Background loop to fetch the daily Overnight India Yield Curve (ZCYC/MIBOR)."""
        log.info("async_engine.yield_loop_started")
        while True:
            try:
                # In production, hit the public API for an overnight rate. 
                # Doing a simulated fetch to demonstrate the architecture pattern.
                async with aiohttp.ClientSession() as session:
                    # e.g., await session.get("https://.../api/yield-curve")
                    await asyncio.sleep(0.5)
                    # Simulated mock response
                    dynamic_rate = 0.0665  # 6.65% live yield
                    
                    if hasattr(self.state, "bs_engine") and hasattr(self.state.bs_engine, "r"):
                        old_rate = self.state.bs_engine.r
                        self.state.bs_engine.r = dynamic_rate
                        if old_rate != dynamic_rate:
                            log.info("async_engine.yield_curve_updated", old_rate=old_rate, new_rate=dynamic_rate)
                            
            except Exception as e:
                log.error("async_engine.yield_loop_error", error=str(e))
                
            # Sleep 24 hours between yield curve scrapes
            await asyncio.sleep(86400)


async def main():
    engine = AsyncTradeEngine()
    await engine.connect()
    
    # Run the listener, periodic MTM check, and daily yield fetch concurrently
    await asyncio.gather(
        engine.tick_listener(),
        engine.mtm_monitor_loop(),
        engine.update_yield_curve_loop(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("async_engine.shutdown")
