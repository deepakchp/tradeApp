"""
modules/scanner.py — Auto-Entry Scanner
========================================
Background scanner that checks VRP entry conditions every 5 minutes
during market hours, generates alert signals for one-click execution.
"""

from __future__ import annotations

import math
import time
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pytz
import structlog

from config import (
    SCANNER_CFG, AUTO_SCAN_SYMBOLS, LOT_SIZES,
    UNDERLYING_INSTRUMENTS, RISK_CFG, LIFECYCLE_CFG,
    SYMBOL_EXCHANGE, STRIKE_STEPS, EXPIRY_CADENCE,
    NSE_HOLIDAYS_2026, DEFAULT_FNO_PRODUCT, NIFTY_DIV_YIELD,
    SYMBOL_SECTOR,
)
from engine import (
    BlackScholesEngine, VRPGateValidator, VRPGateResult,
    StrategySelector, SizingEngine, OptionType,
    StrategyType, Position, OptionLeg, PositionState,
)
from modules.data_engine import IVSurface

log = structlog.get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# Index symbols that need dividend yield adjustment in BS model
INDEX_SYMBOLS = frozenset(["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"])


# ─────────────────────────────────────────────────────────────────
# PENDING SIGNAL
# ─────────────────────────────────────────────────────────────────

@dataclass
class SignalLeg:
    """A proposed option leg within a signal."""
    tradingsymbol: str
    strike:        float
    expiry:        date
    option_type:   str        # "CE" or "PE"
    is_long:       bool
    delta:         float
    ltp:           float = 0.0


@dataclass
class PendingSignal:
    signal_id:      str
    symbol:         str
    strategy:       StrategyType
    short_delta:    float
    wing_delta:     float
    vrp_gate: Dict[str, Any] = field(default_factory=dict)
    surface_data: Dict[str, Any] = field(default_factory=dict)
    legs:           List[SignalLeg] = field(default_factory=list)
    lots:           int = 1
    lot_size:       int = 1
    estimated_credit: float = 0.0
    max_loss:       float = 0.0
    sector:         str = ""  # GICS sector from SYMBOL_SECTOR mapping
    created_at:     datetime = field(default_factory=lambda: datetime.now(IST))
    status:         str = "pending"   # "pending" | "executed" | "dismissed" | "expired"

    @property
    def is_expired(self) -> bool:
        age = datetime.now(IST) - self.created_at
        return age.total_seconds() > SCANNER_CFG.signal_ttl_minutes * 60

    def to_dict(self) -> Dict[str, Any]:
        ttl_remaining = max(
            0,
            SCANNER_CFG.signal_ttl_minutes * 60
            - (datetime.now(IST) - self.created_at).total_seconds()
        )
        return {
            "signal_id":        self.signal_id,
            "symbol":           self.symbol,
            "strategy":         self.strategy.value,
            "short_delta":      self.short_delta,
            "wing_delta":       self.wing_delta,
            "vrp_gate":         self.vrp_gate,
            "surface":          self.surface_data,
            "legs": [
                {
                    "tradingsymbol": l.tradingsymbol,
                    "strike":        l.strike,
                    "expiry":        l.expiry.isoformat(),
                    "option_type":   l.option_type,
                    "is_long":       l.is_long,
                    "delta":         round(l.delta, 4),
                    "ltp":           round(l.ltp, 2),
                }
                for l in self.legs
            ],
            "lots":             self.lots,
            "lot_size":         self.lot_size,
            "estimated_credit": round(self.estimated_credit, 2),
            "max_loss":         round(self.max_loss, 2),
            "created_at":       self.created_at.isoformat(),
            "status":           self.status,
            "sector":           self.sector,
            "ttl_remaining_sec": int(ttl_remaining),
        }


# ─────────────────────────────────────────────────────────────────
# AUTO SCANNER
# ─────────────────────────────────────────────────────────────────

class AutoScanner:
    """
    Background scanner that checks VRP entry conditions for configured symbols.
    Generates PendingSignal alerts that the user can execute with one click.
    """

    def __init__(self, broker, system_state) -> None:
        self.broker = broker
        self.state = system_state
        self.bs = BlackScholesEngine()
        self.bs_index = BlackScholesEngine(dividend_yield=NIFTY_DIV_YIELD)
        self.vrp_validator = VRPGateValidator()
        self.strategy_selector = StrategySelector()
        self.sizing = SizingEngine()
        self._signals: Dict[str, PendingSignal] = {}
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def _get_bs(self, symbol: str) -> BlackScholesEngine:
        """Return BS engine with dividend yield for indices, without for stocks."""
        return self.bs_index if symbol in INDEX_SYMBOLS else self.bs

    # ── Start / Stop ────────────────────────────────────────────

    def start(self) -> None:
        if self.is_running:
            log.info("scanner.already_running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._thread.start()
        self._running = True
        log.info("scanner.started", interval_sec=SCANNER_CFG.scan_interval_sec)

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False
        log.info("scanner.stopped")

    # ── Market Hours Check ──────────────────────────────────────

    def _is_market_hours(self) -> bool:
        now = datetime.now(IST)
        if now.weekday() >= 5:   # Saturday=5, Sunday=6
            return False
        # Check NSE/BSE holidays
        today_str = now.strftime("%Y-%m-%d")
        if today_str in NSE_HOLIDAYS_2026:
            return False
        open_h, open_m = map(int, SCANNER_CFG.market_open_time.split(":"))
        close_h, close_m = map(int, SCANNER_CFG.market_close_time.split(":"))
        market_open = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
        market_close = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
        return market_open <= now <= market_close

    # ── Compute Realized Volatility ─────────────────────────────

    def _compute_rv(self, symbol: str) -> Optional[float]:
        """Compute annualized realized volatility from daily close prices."""
        try:
            inst_key = UNDERLYING_INSTRUMENTS.get(symbol)
            if not inst_key:
                return None

            # Get instrument token for the underlying
            exchange, tradingsymbol = inst_key.split(":", 1)
            token = self.broker.get_instrument_token(tradingsymbol, exchange)
            if token is None:
                log.warning("scanner.rv_no_token", symbol=symbol)
                return None

            to_date = date.today()
            from_date = to_date - timedelta(days=SCANNER_CFG.rv_lookback_days + 20)

            candles = self.broker.get_historical_data(
                instrument_token=token,
                from_date=from_date,
                to_date=to_date,
                interval="day",
            )
            if len(candles) < 10:
                log.warning("scanner.rv_insufficient_data",
                            symbol=symbol, candles=len(candles))
                return None

            closes = [c["close"] for c in candles[-SCANNER_CFG.rv_lookback_days:]]
            log_returns = [
                math.log(closes[i] / closes[i - 1])
                for i in range(1, len(closes))
            ]
            rv = float(np.std(log_returns, ddof=1)) * math.sqrt(252) * 100  # Annualized, %
            return round(rv, 2)

        except Exception as e:
            log.error("scanner.rv_failed", symbol=symbol, error=str(e))
            return None

    # ── Compute IV Surface ──────────────────────────────────────

    def _get_target_expiry(self, symbol: str) -> Optional[date]:
        """Find the expiry closest to target DTE (45 days).
        Respects SEBI expiry cadence: monthly-only symbols (BANKNIFTY, MIDCPNIFTY)
        filter to last-Thursday-of-month expiries only.
        """
        try:
            instruments = self.broker.find_options(symbol, expiry=None)
            # find_options with expiry=None won't work as designed;
            # we need to get all expiries from the cached instruments
            exchange = SYMBOL_EXCHANGE.get(symbol, "NFO")
            all_instruments = self.broker.get_instruments_cached(exchange)
            expiries = set()
            for inst in all_instruments:
                if inst.get("name") == symbol and inst.get("instrument_type") in ("CE", "PE"):
                    exp = inst.get("expiry")
                    if exp and isinstance(exp, date):
                        expiries.add(exp)

            if not expiries:
                return None

            # For monthly-only symbols, filter to monthly expiries (last Thursday of month)
            cadence = EXPIRY_CADENCE.get(symbol, "monthly")
            if cadence == "monthly":
                monthly_expiries = set()
                for exp in expiries:
                    # Last Thursday: find last day of month, walk back to Thursday
                    if exp.month == 12:
                        next_month = exp.replace(year=exp.year + 1, month=1, day=1)
                    else:
                        next_month = exp.replace(month=exp.month + 1, day=1)
                    last_day = next_month - timedelta(days=1)
                    # Walk back to Thursday (weekday 3)
                    days_to_thu = (last_day.weekday() - 3) % 7
                    last_thu = last_day - timedelta(days=days_to_thu)
                    # Allow ±1 day tolerance for exchange schedule variations
                    if abs((exp - last_thu).days) <= 1:
                        monthly_expiries.add(exp)
                expiries = monthly_expiries if monthly_expiries else expiries

            target = date.today() + timedelta(days=SCANNER_CFG.entry_dte_target)
            closest = min(expiries, key=lambda e: abs((e - target).days))

            # Only use if within the entry window
            dte = (closest - date.today()).days
            if dte < LIFECYCLE_CFG.entry_dte_min - 5 or dte > LIFECYCLE_CFG.entry_dte_max + 10:
                log.info("scanner.no_suitable_expiry",
                         symbol=symbol, closest_dte=dte)
                return None
            return closest

        except Exception as e:
            log.error("scanner.expiry_search_failed", symbol=symbol, error=str(e))
            return None

    def _build_iv_surface(self, symbol: str, spot: float, expiry: date) -> Optional[IVSurface]:
        """Build IV surface from live option chain data."""
        try:
            # Get ATM strike (closest to spot, per symbol-specific step)
            step = STRIKE_STEPS.get(symbol, 100)
            atm_strike = round(spot / step) * step

            T = max((expiry - date.today()).days, 1) / 365.0
            exchange = SYMBOL_EXCHANGE.get(symbol, "NFO")

            # Use more flexible symbol construction via instruments
            options = self.broker.find_options(symbol, expiry)
            if not options:
                log.warning("scanner.no_options_found", symbol=symbol, expiry=expiry)
                return None

            # Find ATM option instruments
            atm_ce = None
            atm_pe = None
            for opt in options:
                if opt["strike"] == atm_strike:
                    if opt["instrument_type"] == "CE":
                        atm_ce = opt
                    elif opt["instrument_type"] == "PE":
                        atm_pe = opt

            if not atm_ce or not atm_pe:
                # Try nearest strikes
                strikes = sorted(set(o["strike"] for o in options))
                atm_strike = min(strikes, key=lambda s: abs(s - spot))
                for opt in options:
                    if opt["strike"] == atm_strike:
                        if opt["instrument_type"] == "CE":
                            atm_ce = opt
                        elif opt["instrument_type"] == "PE":
                            atm_pe = opt

            if not atm_ce or not atm_pe:
                return None

            # Fetch live quotes for ATM options
            ce_key = f"{exchange}:{atm_ce['tradingsymbol']}"
            pe_key = f"{exchange}:{atm_pe['tradingsymbol']}"
            quotes = self.broker.get_quote([ce_key, pe_key])

            ce_price = quotes.get(ce_key, {}).get("last_price", 0)
            pe_price = quotes.get(pe_key, {}).get("last_price", 0)

            if ce_price <= 0 or pe_price <= 0:
                return None

            # Compute ATM IV from call and put, average them
            # Use dividend-yield-adjusted BS for index options
            bs = self._get_bs(symbol)
            iv_ce = bs.implied_vol(spot, atm_strike, T, ce_price, OptionType.CALL)
            iv_pe = bs.implied_vol(spot, atm_strike, T, pe_price, OptionType.PUT)
            iv_atm = (iv_ce + iv_pe) / 2 * 100  # Convert to percentage

            # Compute RV
            rv = self._compute_rv(symbol)
            if rv is None:
                return None

            # Compute IV percentile using symbol's own historical RV
            ivp = self._compute_iv_percentile(symbol, current_iv=iv_atm)

            # Compute put skew: 16D put IV / ATM IV
            put_skew = self._compute_put_skew(symbol, spot, expiry, T, options, iv_atm)

            # Compute call skew: 16D call IV / ATM IV
            call_skew = self._compute_call_skew(symbol, spot, expiry, T, options, iv_atm)

            iv_rv_spread = iv_atm - rv
            iv_rv_ratio = iv_atm / rv if rv > 0 else 0

            surface = IVSurface(
                symbol=symbol,
                spot=spot,
                iv_30d=iv_atm,
                rv_30d=rv,
                iv_rank=ivp,
                iv_percentile=ivp,
                put_skew_ratio=put_skew,
                iv_rv_ratio=iv_rv_ratio,
                iv_rv_spread=iv_rv_spread,
                call_skew_ratio=call_skew,
            )

            log.info("scanner.iv_surface_built",
                     symbol=symbol,
                     iv=round(iv_atm, 1), rv=round(rv, 1),
                     spread=round(iv_rv_spread, 1),
                     ivp=round(ivp, 0),
                     skew=round(put_skew, 2))
            return surface

        except Exception as e:
            log.error("scanner.iv_surface_failed", symbol=symbol, error=str(e))
            return None

    def _compute_iv_percentile(self, symbol: str, current_iv: float = 0.0) -> float:
        """Compute IV percentile: % of past 1-year IV observations below current IV.

        For indices (NIFTY, BANKNIFTY, etc.): Uses India VIX historical data as
        the IV series — VIX IS the market-implied volatility for NIFTY options.

        For stocks: Uses the stock's own 1-year rolling 30-day RV series as proxy
        since historical IV data is not stored. This is an approximation — the RV
        distribution underestimates IV levels, so stock IVP may read slightly high.
        """
        try:
            if current_iv <= 0:
                return 50.0

            # For index symbols: use India VIX historical data (gold standard)
            if symbol in INDEX_SYMBOLS:
                return self._compute_ivp_from_vix(current_iv)

            # For stocks: use stock's own historical RV as proxy
            return self._compute_ivp_from_rv(symbol, current_iv)

        except Exception as e:
            log.error("scanner.ivp_failed", symbol=symbol, error=str(e))
            return 50.0

    def _compute_ivp_from_vix(self, current_iv: float) -> float:
        """IV percentile for indices using India VIX historical data."""
        try:
            vix_token = self.broker.get_instrument_token("INDIA VIX", "NSE")
            if vix_token is None:
                return 50.0

            to_date = date.today()
            from_date = to_date - timedelta(days=SCANNER_CFG.ivp_lookback_days + 30)

            candles = self.broker.get_historical_data(
                instrument_token=vix_token,
                from_date=from_date,
                to_date=to_date,
                interval="day",
            )
            if len(candles) < 60:
                return 50.0

            # VIX close values are directly IV readings (annualized %)
            vix_series = np.array([c["close"] for c in candles], dtype=float)
            percentile = float(np.sum(vix_series < current_iv) / len(vix_series) * 100)
            return round(percentile, 1)

        except Exception as e:
            log.error("scanner.ivp_vix_failed", error=str(e))
            return 50.0

    def _compute_ivp_from_rv(self, symbol: str, current_iv: float) -> float:
        """IV percentile for stocks using historical RV as proxy."""
        try:
            inst_key = UNDERLYING_INSTRUMENTS.get(symbol)
            if not inst_key:
                return 50.0

            exchange, tradingsymbol = inst_key.split(":", 1)
            token = self.broker.get_instrument_token(tradingsymbol, exchange)
            if token is None:
                return 50.0

            to_date = date.today()
            from_date = to_date - timedelta(days=SCANNER_CFG.ivp_lookback_days + 30)

            candles = self.broker.get_historical_data(
                instrument_token=token,
                from_date=from_date,
                to_date=to_date,
                interval="day",
            )
            if len(candles) < 60:
                return 50.0

            closes = np.array([c["close"] for c in candles], dtype=float)
            log_returns = np.diff(np.log(closes))

            # Compute rolling 30-day annualized RV series (sample std dev)
            window = min(30, len(log_returns) - 1)
            rv_series = []
            for i in range(window, len(log_returns)):
                chunk = log_returns[i - window:i]
                rv = float(np.std(chunk, ddof=1)) * math.sqrt(252) * 100
                rv_series.append(rv)

            if len(rv_series) < 10:
                return 50.0

            rv_arr = np.array(rv_series)
            percentile = float(np.sum(rv_arr < current_iv) / len(rv_arr) * 100)
            return round(percentile, 1)

        except Exception as e:
            log.error("scanner.ivp_rv_failed", symbol=symbol, error=str(e))
            return 50.0

    def _compute_put_skew(
        self,
        symbol: str,
        spot: float,
        expiry: date,
        T: float,
        options: List[Dict],
        iv_atm: float,
    ) -> float:
        """Compute put skew ratio: 16D put IV / ATM IV."""
        try:
            # Find the ~16D put strike
            put_options = [o for o in options if o["instrument_type"] == "PE"]
            if not put_options:
                return 1.0

            bs = self._get_bs(symbol)

            # Sort puts by strike descending (OTM puts are below spot)
            otm_puts = sorted(
                [o for o in put_options if o["strike"] < spot],
                key=lambda o: o["strike"],
                reverse=True
            )

            # Try to find the 16D put by computing delta for each
            best_put = None
            best_delta_diff = float("inf")

            for put in otm_puts[:10]:  # Check top 10 OTM puts
                delta = abs(bs.greeks(
                    spot, put["strike"], T, iv_atm / 100, OptionType.PUT
                ).delta)
                diff = abs(delta - 0.16)
                if diff < best_delta_diff:
                    best_delta_diff = diff
                    best_put = put

            if not best_put or best_delta_diff > 0.05:
                return 1.0

            # Get quote for the 16D put
            exchange = SYMBOL_EXCHANGE.get(symbol, "NFO")
            put_key = f"{exchange}:{best_put['tradingsymbol']}"
            q = self.broker.get_quote([put_key])
            put_price = q.get(put_key, {}).get("last_price", 0)
            if put_price <= 0:
                return 1.0

            # Compute 16D put IV
            put_iv = bs.implied_vol(
                spot, best_put["strike"], T, put_price, OptionType.PUT
            ) * 100

            skew = put_iv / iv_atm if iv_atm > 0 else 1.0
            return round(skew, 3)

        except Exception as e:
            log.error("scanner.put_skew_failed", symbol=symbol, error=str(e))
            return 1.0

    def _compute_call_skew(
        self,
        symbol: str,
        spot: float,
        expiry: date,
        T: float,
        options: List[Dict],
        iv_atm: float,
    ) -> float:
        """Compute call skew ratio: 16D call IV / ATM IV."""
        try:
            call_options = [o for o in options if o["instrument_type"] == "CE"]
            if not call_options:
                return 1.0

            bs = self._get_bs(symbol)

            # OTM calls are above spot, sorted ascending by strike
            otm_calls = sorted(
                [o for o in call_options if o["strike"] > spot],
                key=lambda o: o["strike"],
            )

            best_call = None
            best_delta_diff = float("inf")

            for call in otm_calls[:10]:
                delta = abs(bs.greeks(
                    spot, call["strike"], T, iv_atm / 100, OptionType.CALL
                ).delta)
                diff = abs(delta - 0.16)
                if diff < best_delta_diff:
                    best_delta_diff = diff
                    best_call = call

            if not best_call or best_delta_diff > 0.05:
                return 1.0

            exchange = SYMBOL_EXCHANGE.get(symbol, "NFO")
            call_key = f"{exchange}:{best_call['tradingsymbol']}"
            q = self.broker.get_quote([call_key])
            call_price = q.get(call_key, {}).get("last_price", 0)
            if call_price <= 0:
                return 1.0

            call_iv = bs.implied_vol(
                spot, best_call["strike"], T, call_price, OptionType.CALL
            ) * 100

            skew = call_iv / iv_atm if iv_atm > 0 else 1.0
            return round(skew, 3)

        except Exception as e:
            log.error("scanner.call_skew_failed", symbol=symbol, error=str(e))
            return 1.0

    # ── Find Strikes ────────────────────────────────────────────

    def _find_strikes(
        self,
        symbol: str,
        spot: float,
        expiry: date,
        short_delta: float,
        wing_delta: float,
        strategy: StrategyType,
        options: List[Dict],
    ) -> Optional[List[SignalLeg]]:
        """Find option strikes matching target deltas for the chosen strategy."""
        try:
            T = max((expiry - date.today()).days, 1) / 365.0

            # Get IV for delta calculation (use rough ATM IV)
            step = STRIKE_STEPS.get(symbol, 100)
            strikes = sorted(set(o["strike"] for o in options))
            atm_strike = min(strikes, key=lambda s: abs(s - spot))

            # Approximate IV from previous surface computation
            sigma = 0.20  # Default; will be overridden

            # Get ATM quotes for IV estimate
            exchange = SYMBOL_EXCHANGE.get(symbol, "NFO")
            atm_opts = [o for o in options if o["strike"] == atm_strike]
            for opt in atm_opts:
                key = f"{exchange}:{opt['tradingsymbol']}"
                q = self.broker.get_quote([key])
                price = q.get(key, {}).get("last_price", 0)
                if price > 0:
                    otype = OptionType.CALL if opt["instrument_type"] == "CE" else OptionType.PUT
                    iv = self.bs.implied_vol(spot, atm_strike, T, price, otype)
                    if iv > 0:
                        sigma = iv
                        break

            legs = []

            if strategy in (StrategyType.IRON_CONDOR, StrategyType.STRANGLE):
                # Find short call (OTM above spot)
                call_strikes = sorted([s for s in strikes if s > spot])
                short_call_strike = self._closest_delta_strike(
                    call_strikes, spot, T, sigma, OptionType.CALL, short_delta
                )

                # Find short put (OTM below spot)
                put_strikes = sorted([s for s in strikes if s < spot], reverse=True)
                short_put_strike = self._closest_delta_strike(
                    put_strikes, spot, T, sigma, OptionType.PUT, short_delta
                )

                if not short_call_strike or not short_put_strike:
                    return None

                # Short legs
                short_call_sym = self._find_tradingsymbol(options, short_call_strike, "CE")
                short_put_sym = self._find_tradingsymbol(options, short_put_strike, "PE")

                if not short_call_sym or not short_put_sym:
                    return None

                sc_delta = self.bs.greeks(spot, short_call_strike, T, sigma, OptionType.CALL).delta
                sp_delta = self.bs.greeks(spot, short_put_strike, T, sigma, OptionType.PUT).delta

                legs.append(SignalLeg(
                    tradingsymbol=short_call_sym, strike=short_call_strike,
                    expiry=expiry, option_type="CE", is_long=False, delta=sc_delta,
                ))
                legs.append(SignalLeg(
                    tradingsymbol=short_put_sym, strike=short_put_strike,
                    expiry=expiry, option_type="PE", is_long=False, delta=sp_delta,
                ))

                if strategy == StrategyType.IRON_CONDOR:
                    # Find long wings
                    long_call_strike = self._closest_delta_strike(
                        [s for s in call_strikes if s > short_call_strike],
                        spot, T, sigma, OptionType.CALL, wing_delta
                    )
                    long_put_strike = self._closest_delta_strike(
                        [s for s in put_strikes if s < short_put_strike],
                        spot, T, sigma, OptionType.PUT, wing_delta
                    )

                    if long_call_strike and long_put_strike:
                        lc_sym = self._find_tradingsymbol(options, long_call_strike, "CE")
                        lp_sym = self._find_tradingsymbol(options, long_put_strike, "PE")
                        if lc_sym and lp_sym:
                            lc_delta = self.bs.greeks(spot, long_call_strike, T, sigma, OptionType.CALL).delta
                            lp_delta = self.bs.greeks(spot, long_put_strike, T, sigma, OptionType.PUT).delta
                            legs.append(SignalLeg(
                                tradingsymbol=lc_sym, strike=long_call_strike,
                                expiry=expiry, option_type="CE", is_long=True, delta=lc_delta,
                            ))
                            legs.append(SignalLeg(
                                tradingsymbol=lp_sym, strike=long_put_strike,
                                expiry=expiry, option_type="PE", is_long=True, delta=lp_delta,
                            ))
                        else:
                            # Wing tradingsymbols not found — downgrade to strangle
                            log.warning("scanner.iron_condor_downgrade",
                                        symbol=symbol, reason="wing tradingsymbols not found")
                    else:
                        # Wing strikes not found — downgrade to strangle
                        log.warning("scanner.iron_condor_downgrade",
                                    symbol=symbol, reason="wing strikes not found")

            elif strategy == StrategyType.PUT_SPREAD:
                # Short put at ~30D, long put at ~16D (defined risk)
                put_strikes_otm = sorted([s for s in strikes if s < spot], reverse=True)
                short_put_strike = self._closest_delta_strike(
                    put_strikes_otm, spot, T, sigma, OptionType.PUT, 0.30
                )
                long_put_strike = self._closest_delta_strike(
                    [s for s in put_strikes_otm if s < (short_put_strike or 0)],
                    spot, T, sigma, OptionType.PUT, short_delta
                )

                if not short_put_strike or not long_put_strike:
                    return None

                sp_sym = self._find_tradingsymbol(options, short_put_strike, "PE")
                lp_sym = self._find_tradingsymbol(options, long_put_strike, "PE")
                if not sp_sym or not lp_sym:
                    return None

                sp_delta = self.bs.greeks(spot, short_put_strike, T, sigma, OptionType.PUT).delta
                lp_delta = self.bs.greeks(spot, long_put_strike, T, sigma, OptionType.PUT).delta

                legs.append(SignalLeg(
                    tradingsymbol=sp_sym, strike=short_put_strike,
                    expiry=expiry, option_type="PE", is_long=False, delta=sp_delta,
                ))
                legs.append(SignalLeg(
                    tradingsymbol=lp_sym, strike=long_put_strike,
                    expiry=expiry, option_type="PE", is_long=True, delta=lp_delta,
                ))

            # Fetch LTPs for all legs
            if legs:
                exchange = SYMBOL_EXCHANGE.get(symbol, "NFO")
                leg_keys = [f"{exchange}:{l.tradingsymbol}" for l in legs]
                ltps = self.broker.get_ltp(leg_keys)
                for leg in legs:
                    key = f"{exchange}:{leg.tradingsymbol}"
                    leg.ltp = ltps.get(key, {}).get("last_price", 0)

                # Reject if any leg has zero LTP — cannot price the trade
                zero_legs = [l.tradingsymbol for l in legs if l.ltp <= 0]
                if zero_legs:
                    log.warning("scanner.zero_ltp_legs",
                                symbol=symbol, legs=zero_legs)
                    return None

            return legs if legs else None

        except Exception as e:
            log.error("scanner.find_strikes_failed", symbol=symbol, error=str(e))
            return None

    def _closest_delta_strike(
        self,
        strike_list: List[float],
        spot: float,
        T: float,
        sigma: float,
        option_type: OptionType,
        target_delta: float,
    ) -> Optional[float]:
        """Find the strike closest to the target delta."""
        best = None
        best_diff = float("inf")
        for strike in strike_list[:15]:  # Limit search range
            g = self.bs.greeks(spot, strike, T, sigma, option_type)
            diff = abs(abs(g.delta) - target_delta)
            if diff < best_diff:
                best_diff = diff
                best = strike
        return best

    def _find_tradingsymbol(
        self,
        options: List[Dict],
        strike: float,
        inst_type: str,
    ) -> Optional[str]:
        """Find the tradingsymbol for a given strike and type."""
        for opt in options:
            if opt["strike"] == strike and opt["instrument_type"] == inst_type:
                return opt["tradingsymbol"]
        return None

    # ── Scan a Single Symbol ────────────────────────────────────

    def _scan_symbol(self, symbol: str) -> Optional[PendingSignal]:
        """Full scan pipeline for one symbol."""
        try:
            log.info("scanner.scanning", symbol=symbol)

            # Check position limit
            active_count = sum(
                1 for p in self.state.positions.values()
                if p.state == PositionState.ACTIVE
            )
            if active_count >= RISK_CFG.max_concurrent_positions:
                log.info("scanner.max_positions_reached", count=active_count)
                return None

            # Get live spot price
            inst_key = UNDERLYING_INSTRUMENTS.get(symbol)
            if not inst_key:
                return None

            ltp = self.broker.get_ltp([inst_key])
            spot = ltp.get(inst_key, {}).get("last_price", 0)
            if spot <= 0:
                return None

            # Get target expiry
            expiry = self._get_target_expiry(symbol)
            if not expiry:
                return None

            # Get option chain for this expiry
            options = self.broker.find_options(symbol, expiry)
            if not options:
                log.warning("scanner.no_options", symbol=symbol, expiry=expiry)
                return None

            # Build IV surface
            surface = self._build_iv_surface(symbol, spot, expiry)
            if not surface:
                return None

            # Run VRP gate
            vrp_result = self.vrp_validator.validate(surface)
            if not vrp_result.passed:
                log.info("scanner.vrp_gate_failed",
                         symbol=symbol, failures=vrp_result.failures)
                return None

            # Get India VIX for strategy selection
            vix_ltp = self.broker.get_ltp(["NSE:INDIA VIX"])
            india_vix = vix_ltp.get("NSE:INDIA VIX", {}).get("last_price", 15.0)

            # Select strategy
            strategy, short_delta, wing_delta = self.strategy_selector.select(
                surface, india_vix
            )

            # Find strikes
            legs = self._find_strikes(
                symbol, spot, expiry, short_delta, wing_delta, strategy, options
            )
            if not legs:
                log.warning("scanner.no_suitable_strikes", symbol=symbol)
                return None

            # Detect iron condor downgrade: selected IC but got only short legs (no wings)
            long_legs_found = [l for l in legs if l.is_long]
            if strategy == StrategyType.IRON_CONDOR and not long_legs_found:
                strategy = StrategyType.STRANGLE
                log.info("scanner.strategy_downgraded", symbol=symbol,
                         original="iron_condor", actual="strangle")

            # Compute estimated credit and max loss
            lot_size = LOT_SIZES.get(symbol, 1)
            short_legs = [l for l in legs if not l.is_long]
            long_legs = [l for l in legs if l.is_long]

            estimated_credit = sum(l.ltp for l in short_legs) - sum(l.ltp for l in long_legs)

            # Max loss for iron condor: width of widest spread - credit
            if strategy == StrategyType.IRON_CONDOR and long_legs:
                call_width = 0
                put_width = 0
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
                width = abs(short_legs[0].strike - long_legs[0].strike) if short_legs and long_legs else 0
                max_loss_per_lot = (width - estimated_credit) * lot_size
                if max_loss_per_lot <= 0:
                    max_loss_per_lot = width * lot_size
            else:
                # Naked/undefined risk — use a large estimate
                max_loss_per_lot = estimated_credit * lot_size * 5

            # Size position
            lots = self.sizing.compute_lots(
                aum=self.state.aum,
                max_loss_inr=max_loss_per_lot if max_loss_per_lot > 0 else lot_size * spot * 0.05,
                lot_size=lot_size,
            )

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

            self._signals[signal.signal_id] = signal
            log.info("scanner.signal_generated",
                     signal_id=signal.signal_id,
                     symbol=symbol,
                     strategy=strategy.value,
                     credit=signal.estimated_credit,
                     lots=lots)

            return signal

        except Exception as e:
            log.error("scanner.scan_symbol_failed", symbol=symbol, error=str(e))
            return None

    # ── Main Scan Loop ──────────────────────────────────────────

    def _scan_loop(self) -> None:
        """Background loop: scan all symbols at configured interval."""
        while not self._stop_event.is_set():
            try:
                if self._is_market_hours():
                    # Clean expired signals
                    self._cleanup_expired()

                    # Check pending signal limit
                    pending = [s for s in self._signals.values() if s.status == "pending" and not s.is_expired]
                    if len(pending) < SCANNER_CFG.max_pending_signals:
                        for symbol in AUTO_SCAN_SYMBOLS:
                            # Skip if there's already a pending signal for this symbol
                            if any(s.symbol == symbol for s in pending):
                                continue
                            signal = self._scan_symbol(symbol)
                            
                            # Auto-execute the signal if one was successfully generated
                            if signal:
                                log.info("scanner.auto_executing_signal", signal_id=signal.signal_id, symbol=symbol)
                                self.execute_signal(signal.signal_id)
                    else:
                        log.debug("scanner.max_pending_reached",
                                  count=len(pending))
                else:
                    log.debug("scanner.outside_market_hours")

            except Exception as e:
                log.error("scanner.loop_error", error=str(e))

            # Wait for next scan interval or stop event
            self._stop_event.wait(SCANNER_CFG.scan_interval_sec)

    def _cleanup_expired(self) -> None:
        """Mark expired signals."""
        for signal in list(self._signals.values()):
            if signal.status == "pending" and signal.is_expired:
                signal.status = "expired"
                log.info("scanner.signal_expired", signal_id=signal.signal_id)

    # ── Public API ──────────────────────────────────────────────

    def get_pending_signals(self) -> List[PendingSignal]:
        """Return all active (non-expired, non-dismissed) signals."""
        self._cleanup_expired()
        return [
            s for s in self._signals.values()
            if s.status == "pending" and not s.is_expired
        ]

    def scan_metrics(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """Compute VRP metrics for a list of symbols without generating signals.

        Returns a list of dicts with per-symbol VRP metrics for the scanner
        dashboard table.
        """
        results: List[Dict[str, Any]] = []

        # Fetch India VIX once (shared across all symbols)
        india_vix = None
        try:
            vix_ltp = self.broker.get_ltp(["NSE:INDIA VIX"])
            india_vix = vix_ltp.get("NSE:INDIA VIX", {}).get("last_price")
        except Exception:
            pass

        for symbol in symbols:
            try:
                inst_key = UNDERLYING_INSTRUMENTS.get(symbol)
                if not inst_key:
                    continue

                ltp = self.broker.get_ltp([inst_key])
                spot = ltp.get(inst_key, {}).get("last_price", 0)
                if spot <= 0:
                    continue

                expiry = self._get_target_expiry(symbol)
                if not expiry:
                    continue

                options = self.broker.find_options(symbol, expiry)
                if not options:
                    continue

                # Build IV surface (now includes per-symbol IV percentile)
                surface = self._build_iv_surface(symbol, spot, expiry)
                if not surface:
                    continue

                # VRP gate check
                vrp_result = self.vrp_validator.validate(surface)

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
                    "vix": round(india_vix, 2) if india_vix is not None else None,
                    "vrp_gate_passed": vrp_result.passed,
                    "vrp_failures": vrp_result.failures,
                })

                log.info("scanner.metrics_computed", symbol=symbol,
                         iv_rv_spread=round(surface.iv_rv_spread, 2),
                         gate=vrp_result.passed)

                # Rate-limit to avoid Kite "Too many requests" (3 req/s limit)
                time.sleep(0.5)

            except Exception as e:
                log.error("scanner.metrics_symbol_failed",
                          symbol=symbol, error=str(e))
                continue

        return results

    def execute_signal(self, signal_id: str) -> Dict[str, Any]:
        """Execute a pending signal — place orders via broker."""
        signal = self._signals.get(signal_id)
        if not signal:
            return {"error": "Signal not found"}
        if signal.status != "pending":
            return {"error": f"Signal is {signal.status}, not pending"}
        if signal.is_expired:
            signal.status = "expired"
            return {"error": "Signal has expired"}

        # ── Guard 1: Duplicate symbol ────────────────────────────────
        # Block if there is already an ACTIVE position on the same underlying.
        existing = [
            p for p in self.state.positions.values()
            if p.symbol == signal.symbol and p.state == PositionState.ACTIVE
        ]
        if existing:
            log.warning("scanner.duplicate_symbol_blocked",
                        signal_id=signal_id, symbol=signal.symbol,
                        existing_position=existing[0].position_id)
            return {
                "error": (
                    f"Duplicate blocked: an active position for '{signal.symbol}' "
                    f"already exists ({existing[0].position_id})"
                )
            }

        # ── Guard 2: Max concurrent positions ───────────────────────
        active_count = sum(
            1 for p in self.state.positions.values()
            if p.state == PositionState.ACTIVE
        )
        if active_count >= RISK_CFG.max_concurrent_positions:
            log.warning("scanner.max_positions_blocked",
                        signal_id=signal_id, count=active_count,
                        limit=RISK_CFG.max_concurrent_positions)
            return {
                "error": (
                    f"Max concurrent positions ({RISK_CFG.max_concurrent_positions}) "
                    f"already reached — cannot execute signal for '{signal.symbol}'"
                )
            }

        from config import PAPER_TRADE

        # Guard: reject if any leg has zero LTP — cannot execute without price
        zero_legs = [l.tradingsymbol for l in signal.legs if l.ltp <= 0]
        if zero_legs:
            log.warning("scanner.execute_zero_ltp_blocked",
                        signal_id=signal_id, legs=zero_legs)
            return {
                "error": f"Cannot execute: legs with zero LTP: {zero_legs}. "
                         "No valid market price available."
            }

        order_ids = []
        exchange = SYMBOL_EXCHANGE.get(signal.symbol, "NFO")
        try:
            for leg in signal.legs:
                tx_type = "BUY" if leg.is_long else "SELL"
                quantity = signal.lots * signal.lot_size

                if not PAPER_TRADE and self.broker.is_connected:
                    order_id = self.broker.place_order(
                        tradingsymbol=leg.tradingsymbol,
                        exchange=exchange,
                        transaction_type=tx_type,
                        quantity=quantity,
                        order_type="LIMIT",
                        price=leg.ltp,
                        product=DEFAULT_FNO_PRODUCT,
                    )
                    order_ids.append(order_id)
                    log.info("scanner.order_placed",
                             signal_id=signal_id,
                             order_id=order_id,
                             tradingsymbol=leg.tradingsymbol,
                             tx_type=tx_type,
                             qty=quantity,
                             price=leg.ltp)
                else:
                    # Paper trade — log but don't place real order
                    paper_id = f"PAPER-{uuid.uuid4().hex[:8]}"
                    order_ids.append(paper_id)
                    log.info("scanner.paper_order",
                             signal_id=signal_id,
                             paper_id=paper_id,
                             tradingsymbol=leg.tradingsymbol,
                             tx_type=tx_type,
                             qty=quantity,
                             price=leg.ltp)

            # Create position in system state
            position_id = str(uuid.uuid4())[:12]
            position = Position(
                position_id=position_id,
                symbol=signal.symbol,
                strategy=signal.strategy,
                state=PositionState.ACTIVE,
                entry_spot=signal.surface_data.get("spot", 0),
                entry_iv=signal.surface_data.get("iv_30d", 0),
                max_profit=signal.estimated_credit,
            )

            # Add legs to position
            for leg in signal.legs:
                position.legs.append(OptionLeg(
                    symbol=leg.tradingsymbol,
                    strike=leg.strike,
                    expiry=leg.expiry,
                    option_type=OptionType.CALL if leg.option_type == "CE" else OptionType.PUT,
                    is_long=leg.is_long,
                    lots=signal.lots,
                    lot_size=signal.lot_size,
                    exchange=exchange,
                    entry_price=leg.ltp,
                    current_price=leg.ltp,
                ))

            self.state.positions[position_id] = position
            signal.status = "executed"

            # ── Persist to PostgreSQL ──────────────────────────────────
            try:
                from modules.db import persist_position, persist_order_event
                persist_position(position)
                persist_order_event(
                    position_id=position_id,
                    event_type="position.opened",
                    action=signal.strategy.value,
                    details={
                        "signal_id": signal_id,
                        "order_ids": order_ids,
                        "paper_trade": PAPER_TRADE,
                    },
                )
            except Exception as e:
                log.error("db.persist_on_scanner_execute_failed",
                          position_id=position_id, error=str(e))

            log.info("scanner.signal_executed",
                     signal_id=signal_id,
                     position_id=position_id,
                     orders=len(order_ids))

            return {
                "status": "executed",
                "signal_id": signal_id,
                "position_id": position_id,
                "order_ids": order_ids,
                "paper_trade": PAPER_TRADE,
            }

        except Exception as e:
            log.error("scanner.execute_failed",
                      signal_id=signal_id, error=str(e))
            return {"error": str(e)}

    def dismiss_signal(self, signal_id: str) -> Dict[str, Any]:
        """Dismiss a pending signal."""
        signal = self._signals.get(signal_id)
        if not signal:
            return {"error": "Signal not found"}
        signal.status = "dismissed"
        log.info("scanner.signal_dismissed", signal_id=signal_id)
        return {"status": "dismissed", "signal_id": signal_id}
