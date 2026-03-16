"""
modules/broker.py — Zerodha Kite Connect Broker Adapter
========================================================
Wraps the kiteconnect SDK to provide:
  - OAuth login URL generation
  - Session (access_token) management
  - Quote, order, position, and instrument APIs
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

import structlog
from kiteconnect import KiteConnect

from config import KITE_API_KEY, KITE_API_SECRET

log = structlog.get_logger(__name__)


class KiteBroker:
    """Thin adapter around KiteConnect with session lifecycle management."""

    def __init__(self, api_key: str = KITE_API_KEY, api_secret: str = KITE_API_SECRET) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.kite = KiteConnect(api_key=self.api_key)
        self._access_token: Optional[str] = None
        self._user_profile: Optional[Dict[str, Any]] = None
        self._instruments_cache: Dict[str, List[Dict[str, Any]]] = {}

    # ── Login Flow ────────────────────────────────────────────────

    def get_login_url(self) -> str:
        """Return the Zerodha OAuth login URL the user must open in a browser."""
        return self.kite.login_url()

    def generate_session(self, request_token: str) -> Dict[str, Any]:
        """
        Exchange the request_token (from Kite redirect) for an access_token.
        Stores the token internally so subsequent API calls are authenticated.
        """
        data = self.kite.generate_session(request_token, api_secret=self.api_secret)
        self._access_token = data["access_token"]
        self.kite.set_access_token(self._access_token)
        self._user_profile = data
        log.info("broker.session_created",
                 user_id=data.get("user_id"),
                 user_name=data.get("user_name"))
        return data

    def set_access_token(self, access_token: str) -> None:
        """Manually set an access token (e.g. restored from storage)."""
        self._access_token = access_token
        self.kite.set_access_token(access_token)

    @property
    def is_connected(self) -> bool:
        return self._access_token is not None

    @property
    def user_profile(self) -> Optional[Dict[str, Any]]:
        return self._user_profile

    # ── Market Data ───────────────────────────────────────────────

    def get_quote(self, instruments: List[str]) -> Dict[str, Any]:
        """
        Fetch live quotes.
        instruments: list of exchange:tradingsymbol, e.g. ["NSE:NIFTY 50", "NFO:NIFTY24MAR22000CE"]
        """
        return self.kite.quote(instruments)

    def get_ltp(self, instruments: List[str]) -> Dict[str, Any]:
        """Fetch last traded prices only (lighter than full quote)."""
        return self.kite.ltp(instruments)

    def get_ohlc(self, instruments: List[str]) -> Dict[str, Any]:
        """Fetch OHLC + volume data."""
        return self.kite.ohlc(instruments)

    # ── Orders ────────────────────────────────────────────────────

    def place_order(
        self,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        order_type: str = "LIMIT",
        price: Optional[float] = None,
        product: str = "NRML",
        variety: str = "regular",
        trigger_price: Optional[float] = None,
    ) -> str:
        """
        Place an order and return the order_id.
        transaction_type: "BUY" or "SELL"
        order_type: "MARKET", "LIMIT", "SL", "SL-M"
        product: "NRML" (F&O overnight), "MIS" (intraday), "CNC" (delivery)
        """
        params: Dict[str, Any] = {
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "order_type": order_type,
            "product": product,
            "variety": variety,
        }
        if price is not None:
            params["price"] = price
        if trigger_price is not None:
            params["trigger_price"] = trigger_price

        order_id = self.kite.place_order(**params)
        log.info("broker.order_placed",
                 order_id=order_id,
                 tradingsymbol=tradingsymbol,
                 transaction_type=transaction_type,
                 quantity=quantity,
                 order_type=order_type)
        return order_id

    def cancel_order(self, order_id: str, variety: str = "regular") -> str:
        """Cancel an open order."""
        result = self.kite.cancel_order(variety=variety, order_id=order_id)
        log.info("broker.order_cancelled", order_id=order_id)
        return result

    def get_orders(self) -> List[Dict[str, Any]]:
        """Fetch all orders for the day."""
        return self.kite.orders()

    def get_order_history(self, order_id: str) -> List[Dict[str, Any]]:
        """Fetch status history for a specific order."""
        return self.kite.order_history(order_id)

    # ── Positions & Holdings ──────────────────────────────────────

    def get_positions(self) -> Dict[str, Any]:
        """Fetch net and day positions."""
        return self.kite.positions()

    def get_holdings(self) -> List[Dict[str, Any]]:
        """Fetch equity holdings."""
        return self.kite.holdings()

    # ── Instruments ───────────────────────────────────────────────

    def get_instruments(self, exchange: str = "NFO") -> List[Dict[str, Any]]:
        """Fetch the full instrument list for an exchange (NFO, NSE, BSE, etc.)."""
        return self.kite.instruments(exchange)

    # ── Account ───────────────────────────────────────────────────

    def get_profile(self) -> Dict[str, Any]:
        """Fetch the authenticated user's profile."""
        return self.kite.profile()

    def get_margins(self) -> Dict[str, Any]:
        """Fetch account margins (equity + commodity segments)."""
        return self.kite.margins()

    # ── Historical Data ──────────────────────────────────────────

    def get_historical_data(
        self,
        instrument_token: int,
        from_date: date,
        to_date: date,
        interval: str = "day",
    ) -> List[Dict[str, Any]]:
        """
        Fetch historical OHLCV candles from Kite.
        interval: "minute", "3minute", "5minute", "15minute", "30minute",
                  "60minute", "day", "week", "month"
        Returns list of dicts with: date, open, high, low, close, volume.
        """
        records = self.kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval=interval,
        )
        return records

    def get_instruments_cached(self, exchange: str = "NFO") -> List[Dict[str, Any]]:
        """Fetch instruments list, cached per exchange (refreshed once per session)."""
        if exchange not in self._instruments_cache:
            log.info("broker.fetching_instruments", exchange=exchange)
            self._instruments_cache[exchange] = self.kite.instruments(exchange)
            log.info("broker.instruments_cached",
                     exchange=exchange,
                     count=len(self._instruments_cache[exchange]))
        return self._instruments_cache[exchange]

    def get_instrument_token(
        self,
        tradingsymbol: str,
        exchange: str = "NSE",
    ) -> Optional[int]:
        """Look up the instrument_token for a given tradingsymbol + exchange."""
        instruments = self.get_instruments_cached(exchange)
        for inst in instruments:
            if inst.get("tradingsymbol") == tradingsymbol:
                return inst.get("instrument_token")
        return None

    def find_options(
        self,
        symbol: str,
        expiry: date,
        exchange: str = "NFO",
    ) -> List[Dict[str, Any]]:
        """
        Return all option instruments for a given underlying + expiry.
        Filters by name (e.g., "NIFTY") and expiry date.
        """
        instruments = self.get_instruments_cached(exchange)
        results = []
        for inst in instruments:
            if (inst.get("name") == symbol
                    and inst.get("expiry") == expiry
                    and inst.get("instrument_type") in ("CE", "PE")):
                results.append(inst)
        return results

    def clear_instruments_cache(self) -> None:
        """Force refresh of instruments cache on next call."""
        self._instruments_cache.clear()

    # ── Margin Pre-Check ────────────────────────────────────────

    def check_margin_available(self, required_margin: float) -> tuple:
        """
        Check if sufficient margin is available before placing an order.
        Returns (has_margin: bool, available_margin: float).
        """
        try:
            margins = self.get_margins()
            equity = margins.get("equity", {})
            available = equity.get("available", {}).get("live_balance", 0)
            collateral = equity.get("available", {}).get("collateral", 0)
            total_available = available + collateral
            return total_available >= required_margin, total_available
        except Exception as e:
            log.warning("broker.margin_check_failed", error=str(e))
            return True, 0  # Fail-open to not block paper trading

    # ── Basket Margin ──────────────────────────────────────────

    def get_basket_margins(self, orders: list, consider_positions: bool = True) -> Dict[str, Any]:
        """
        Calculate combined margin for a basket of orders using Kite's basket margin API.
        Each order dict: {tradingsymbol, exchange, transaction_type, quantity, order_type, product, price}
        Returns: {"initial": ..., "final": ..., "orders": [...]}
        """
        try:
            result = self.kite.basket_order_margins(
                orders, consider_positions=consider_positions, mode="compact"
            )
            return result
        except Exception as e:
            log.warning("broker.basket_margin_failed", error=str(e))
            return {"error": str(e)}

    # ── GTT Orders ──────────────────────────────────────────────

    def place_gtt_order(
        self,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        trigger_type: str,
        trigger_values: list,
        last_price: float,
        limit_prices: list,
    ) -> int:
        """Place a Good-Till-Triggered order for automatic profit/stop exits."""
        params = {
            "trigger_type": trigger_type,
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
            "trigger_values": trigger_values,
            "last_price": last_price,
            "orders": [{
                "transaction_type": transaction_type,
                "quantity": quantity,
                "order_type": "LIMIT",
                "product": "NRML",
                "price": lp,
            } for lp in limit_prices],
        }
        gtt_id = self.kite.place_gtt(params)
        log.info("broker.gtt_placed", gtt_id=gtt_id, tradingsymbol=tradingsymbol)
        return gtt_id
