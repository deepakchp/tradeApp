"""
modules/data_stream.py — Real-time Market Data Streamer (WebSocket)
===================================================================
Connects to Zerodha KiteTicker WebSocket to receive real-time tick data.
Pushes tick data into Redis so that background Celery tasks and Flask
can access instantaneous L1 order book updates without hitting HTTP APIs.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Set

import redis
import structlog
from kiteconnect import KiteTicker

from config import REDIS_URL

log = structlog.get_logger(__name__)


class MarketDataStreamer:
    """
    Maintains a persistent WebSocket connection to Kite via KiteTicker.
    Receives real-time ticks and pushes them to a local Redis replica.
    """

    def __init__(self, ticker: KiteTicker) -> None:
        self.ticker = ticker
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.is_running = False
        self._thread: threading.Thread | None = None

        # Track currently subscribed instrument tokens
        self.subscribed_tokens: Set[int] = set()
        
        # Redis Key Prefixes
        self.REDIS_LTP_KEY = "vrp:market_data:ltp"
        self.REDIS_VOL_KEY = "vrp:market_data:vol"
        self.REDIS_OHLC_KEY = "vrp:market_data:ohlc"
        self.REDIS_DEPTH_KEY = "vrp:market_data:depth"

        # Bind callbacks
        self.ticker.on_ticks = self._on_ticks
        self.ticker.on_connect = self._on_connect
        self.ticker.on_close = self._on_close
        self.ticker.on_error = self._on_error
        self.ticker.on_reconnect = self._on_reconnect
        self.ticker.on_noreconnect = self._on_noreconnect

    def start(self) -> None:
        """Start the WebSocket streamer in a background thread."""
        if self.is_running:
            log.warning("streamer.already_running")
            return

        self.is_running = True
        log.info("streamer.starting")
        self._thread = threading.Thread(
            target=self.ticker.connect,
            kwargs={"threaded": False}, # Run blocking in this background thread
            daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the WebSocket streamer and close connection."""
        if not self.is_running:
            return

        log.info("streamer.stopping")
        self.is_running = False
        self.ticker.close()
        if self._thread:
            self._thread.join(timeout=5)

    def subscribe(self, instrument_tokens: List[int]) -> None:
        """Subscribe to an array of instrument tokens (fetch Full mode)."""
        new_tokens = [t for t in instrument_tokens if t not in self.subscribed_tokens]
        if not new_tokens:
            return

        log.info("streamer.subscribing", tokens=new_tokens)
        if self.ticker.is_connected():
            self.ticker.subscribe(new_tokens)
            self.ticker.set_mode(self.ticker.MODE_FULL, new_tokens)
        
        for t in new_tokens:
            self.subscribed_tokens.add(t)

    def unsubscribe(self, instrument_tokens: List[int]) -> None:
        """Unsubscribe from instrument tokens."""
        tokens_to_remove = [t for t in instrument_tokens if t in self.subscribed_tokens]
        if not tokens_to_remove:
            return

        log.info("streamer.unsubscribing", tokens=tokens_to_remove)
        if self.ticker.is_connected():
            self.ticker.unsubscribe(tokens_to_remove)
            
        for t in tokens_to_remove:
            self.subscribed_tokens.remove(t)

    # ── KiteTicker Callbacks ──────────────────────────────────────

    def _calculate_vwap(self, depth_levels: List[Dict[str, Any]]) -> float:
        """Calculate VWAP from an array of depth dictionaries (price, quantity)."""
        tot_vol = sum(level.get("quantity", 0) for level in depth_levels)
        if tot_vol == 0:
            return 0.0
        weighted_sum = sum(level.get("price", 0.0) * level.get("quantity", 0) for level in depth_levels)
        return weighted_sum / tot_vol

    def _on_ticks(self, ws, ticks: List[Dict[str, Any]]) -> None:
        """
        Triggered when market data ticks are received.
        We push these updates immediately to the Redis replica.
        """
        pipeline = self.redis_client.pipeline()
        
        # We need to buffer the payload constructs so we can publish VWAPs correctly
        publish_payloads = []

        for tick in ticks:
            token = tick.get("instrument_token")
            if not token:
                continue

            token_str = str(token)
            last_price = tick.get("last_price")
            payload = {"token": token, "last_price": last_price}
            
            # LTP
            if last_price is not None:
                pipeline.hset(self.REDIS_LTP_KEY, token_str, last_price)
            
            # Volume
            if "volume_traded" in tick:
                pipeline.hset(self.REDIS_VOL_KEY, token_str, tick["volume_traded"])

            # OHLC
            if "ohlc" in tick:
                pipeline.hset(self.REDIS_OHLC_KEY, token_str, json.dumps(tick["ohlc"]))
                
            # Depth / Quotes (Full Mode)
            if "depth" in tick:
                buy_depth = tick["depth"].get("buy", [])
                sell_depth = tick["depth"].get("sell", [])
                
                depth_data = {
                    "buy": buy_depth,
                    "sell": sell_depth
                }
                pipeline.hset(self.REDIS_DEPTH_KEY, token_str, json.dumps(depth_data))
                
                # Append VWAP to standard tick payload if we have depth structure
                bid_vwap = self._calculate_vwap(buy_depth)
                ask_vwap = self._calculate_vwap(sell_depth)
                if bid_vwap > 0: payload["bid_vwap"] = round(bid_vwap, 2)
                if ask_vwap > 0: payload["ask_vwap"] = round(ask_vwap, 2)
                
            publish_payloads.append(payload)

        try:
            pipeline.execute()
            # Publish a quick event so the async event loop can wake up
            for payload in publish_payloads:
                if payload.get("last_price") is not None:
                    self.redis_client.publish(
                        "vrp:market_data:ticks",
                        json.dumps(payload)
                    )
        except Exception as exc:
            log.error("streamer.redis_push_failed", error=str(exc))

    def _on_connect(self, ws, response) -> None:
        """Triggered upon successful connection to Kite WebSocket."""
        log.info("streamer.connected", response=response)
        # Resubscribe to existing tokens on reconnect
        if self.subscribed_tokens:
            tokens_list = list(self.subscribed_tokens)
            self.ticker.subscribe(tokens_list)
            self.ticker.set_mode(self.ticker.MODE_FULL, tokens_list)
            log.info("streamer.resubscribed", count=len(tokens_list))

    def _on_close(self, ws, code, reason) -> None:
        """Triggered when connection is closed."""
        log.info("streamer.closed", code=code, reason=reason)

    def _on_error(self, ws, code, reason) -> None:
        """Triggered on WS error."""
        log.error("streamer.error", code=code, reason=reason)

    def _on_reconnect(self, ws, attempts_count) -> None:
        """Triggered during auto-reconnection attempts."""
        log.warning("streamer.reconnecting", attempts=attempts_count)

    def _on_noreconnect(self, ws) -> None:
        """Triggered when max auto-reconnect limit is hit."""
        log.error("streamer.reconnect_failed_max_retries")
