"""
modules/backtester.py — VRP Strategy Backtest Engine
=====================================================
Simulates the VRP options selling strategy over historical data.
Uses daily OHLC for any symbol's spot price and India VIX as IV proxy.
Prices options using the Black-Scholes model from engine.py.
Supports single-symbol and batch (multi-symbol) backtesting.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import structlog

from config import (
    RISK_CFG, GREEKS_CFG, LIFECYCLE_CFG, SKEW_CFG, STRIKE_STEPS, LOT_SIZES,
    AUTO_SCAN_SYMBOLS, UNDERLYING_INSTRUMENTS, SYMBOL_SECTOR,
    BACKTEST_STOCK_SYMBOLS,
)
from engine import (
    BlackScholesEngine, VRPGateValidator, StrategySelector,
    SizingEngine, TransactionCostEngine, OptionType, StrategyType,
)
from modules.data_engine import IVSurface

log = structlog.get_logger(__name__)

NIFTY_STRIKE_STEP = 50
TRADING_DAYS_PER_YEAR = 252


# ─────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    symbol: str = "NIFTY"
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    initial_aum: float = 10_000_000.0
    lot_size: Optional[int] = None         # None = auto-resolve from LOT_SIZES
    risk_free_rate: float = 0.06
    rv_lookback: int = 30
    ivp_lookback: int = 252
    max_concurrent: int = 25
    max_loss_pct: float = 0.02
    entry_dte: int = 45
    profit_target_pct: float = 0.40
    gamma_exit_dte: int = 14
    delta_trigger: float = 0.30
    put_skew_default: float = 1.1
    vix_skew_threshold: float = 20.0
    slippage_pct: float = 0.005
    symbols: List[str] = field(default_factory=list)  # Batch mode: list of symbols


@dataclass
class SimulatedLeg:
    strike: float
    option_type: OptionType
    is_long: bool
    entry_price: float
    current_price: float = 0.0
    delta_at_entry: float = 0.0


@dataclass
class SimulatedTrade:
    trade_id: str
    entry_date: date
    symbol: str = "NIFTY"
    sector: str = ""
    exit_date: Optional[date] = None
    strategy: StrategyType = StrategyType.IRON_CONDOR
    entry_spot: float = 0.0
    exit_spot: float = 0.0
    entry_vix: float = 0.0
    exit_vix: float = 0.0
    legs: List[SimulatedLeg] = field(default_factory=list)
    entry_credit: float = 0.0
    max_profit: float = 0.0
    max_loss: float = 0.0
    lots: int = 1
    pnl_points: float = 0.0
    pnl_inr: float = 0.0
    exit_reason: str = ""
    holding_days: int = 0
    expiry_date: date = field(default_factory=date.today)
    # Roll tracking for adjustment simulation
    challenged_side_rolled: bool = False
    roll_count: int = 0
    cumulative_roll_debit: float = 0.0


@dataclass
class BacktestResult:
    total_return_pct: float = 0.0
    cagr: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    avg_trade_pnl: float = 0.0
    avg_holding_days: float = 0.0
    total_trades: int = 0
    profit_factor: float = 0.0

    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
    monthly_returns: List[Dict[str, Any]] = field(default_factory=list)
    trades: List[Dict[str, Any]] = field(default_factory=list)
    signal_history: List[Dict[str, Any]] = field(default_factory=list)

    config: Dict[str, Any] = field(default_factory=dict)
    run_time_sec: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": {
                "total_return_pct": round(self.total_return_pct, 2),
                "cagr": round(self.cagr, 2),
                "sharpe_ratio": round(self.sharpe_ratio, 2),
                "sortino_ratio": round(self.sortino_ratio, 2),
                "max_drawdown_pct": round(self.max_drawdown_pct, 2),
                "win_rate": round(self.win_rate, 1),
                "avg_trade_pnl": round(self.avg_trade_pnl, 0),
                "avg_holding_days": round(self.avg_holding_days, 1),
                "total_trades": self.total_trades,
                "profit_factor": round(self.profit_factor, 2),
            },
            "equity_curve": self.equity_curve,
            "monthly_returns": self.monthly_returns,
            "trades": self.trades,
            "signal_history": self.signal_history,
            "config": self.config,
            "run_time_sec": round(self.run_time_sec, 2),
        }


# ─────────────────────────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Runs a historical simulation of the VRP options strategy.
    Uses Kite historical data API for any symbol's spot price and India VIX
    as IV proxy, then simulates option pricing and position management via BS model.
    """

    def __init__(self, broker, config: Optional[BacktestConfig] = None):
        self.broker = broker
        self.cfg = config or BacktestConfig()
        # Auto-resolve lot size from config if not explicitly set
        if self.cfg.lot_size is None:
            self.cfg.lot_size = LOT_SIZES.get(self.cfg.symbol, 25)
        self.bs = BlackScholesEngine(risk_free_rate=self.cfg.risk_free_rate)
        self.vrp_validator = VRPGateValidator()
        self.strategy_selector = StrategySelector()
        self.sizing = SizingEngine()
        self.tx_cost = TransactionCostEngine()
        self._strike_step = STRIKE_STEPS.get(self.cfg.symbol, NIFTY_STRIKE_STEP)

        self._open_trades: List[SimulatedTrade] = []
        self._closed_trades: List[SimulatedTrade] = []
        self._equity_curve: List[Dict[str, Any]] = []
        self._signal_history: List[Dict[str, Any]] = []
        self._current_nav: float = self.cfg.initial_aum
        self._peak_nav: float = self.cfg.initial_aum

    # ── Main Entry Point ──────────────────────────────────────────

    def run(self) -> BacktestResult:
        start_time = time.time()

        end = self.cfg.end_date or date.today()
        start = self.cfg.start_date or (end - timedelta(days=365))

        spot_data, vix_data = self._fetch_historical_data(start, end)
        daily_metrics = self._compute_daily_metrics(spot_data, vix_data, start)

        if not daily_metrics:
            log.warning("backtest.no_data", msg="No daily metrics computed")
            result = BacktestResult()
            result.run_time_sec = time.time() - start_time
            return result

        # Circuit breaker state
        cb_active = False
        cb_month: Optional[Tuple[int, int]] = None
        monthly_pnl_start_nav = self.cfg.initial_aum

        prev_spot: Optional[float] = None

        for day_metrics in daily_metrics:
            current_date = day_metrics["date"]
            spot = day_metrics["close"]
            vix = day_metrics["vix"]

            # Circuit breaker: reset at new month
            current_month = (current_date.year, current_date.month)
            if cb_month != current_month:
                cb_month = current_month
                cb_active = False
                monthly_pnl_start_nav = self._current_nav

            # Mark-to-market open positions
            for trade in self._open_trades:
                self._mark_to_market(trade, spot, vix, current_date)

            # Check exits on all open positions
            trades_to_close = []
            trades_to_roll_challenged = []
            trades_to_roll_out = []
            trades_to_roll_untested = []
            for trade in self._open_trades:
                exit_reason = self._check_exit_conditions(
                    trade, current_date, spot, vix, prev_spot
                )
                if exit_reason == "delta_roll_challenged":
                    trades_to_roll_challenged.append(trade)
                elif exit_reason == "itm_breach_roll_out":
                    trades_to_roll_out.append(trade)
                elif exit_reason == "untested_side_roll":
                    trades_to_roll_untested.append(trade)
                elif exit_reason:
                    trades_to_close.append((trade, exit_reason))

            for trade, reason in trades_to_close:
                self._close_trade(trade, spot, vix, current_date, reason)

            # Simulate rolls: challenged leg → new 16D strike, roll-out → same strike new expiry
            for trade in trades_to_roll_challenged:
                self._simulate_roll_challenged(trade, spot, vix, current_date)
            for trade in trades_to_roll_out:
                self._simulate_roll_out(trade, spot, vix, current_date)
            for trade in trades_to_roll_untested:
                self._simulate_roll_untested(trade, spot, vix, current_date)

            # Circuit breaker check: 4% monthly drawdown
            if not cb_active and monthly_pnl_start_nav > 0:
                monthly_return = (self._current_nav - monthly_pnl_start_nav) / monthly_pnl_start_nav
                if monthly_return <= -RISK_CFG.monthly_drawdown_trigger_pct:
                    cb_active = True
                    log.info("backtest.circuit_breaker_triggered",
                             date=current_date.isoformat(),
                             monthly_return_pct=round(monthly_return * 100, 2))

            # Effective max concurrent (halved during circuit breaker)
            effective_max = self.cfg.max_concurrent // 2 if cb_active else self.cfg.max_concurrent

            # Check VRP gate for new entry
            gate_passed = False
            if day_metrics["rv"] is not None and day_metrics["ivp"] is not None:
                # Low-vol regime: skip new entries when VIX < 12
                low_vol_block = vix < LIFECYCLE_CFG.low_vol_vix_threshold

                gate_result = self._check_vrp_gate(day_metrics)
                gate_passed = gate_result.passed and not low_vol_block

                self._signal_history.append({
                    "date": current_date.isoformat(),
                    "gate_passed": gate_passed,
                    "iv": round(vix, 2),
                    "rv": round(day_metrics["rv"], 2),
                    "ivp": round(day_metrics["ivp"], 1),
                    "iv_rv_spread": round(day_metrics["iv_rv_spread"], 2),
                    "iv_rv_ratio": round(day_metrics["iv_rv_ratio"], 2),
                    "open_positions": len(self._open_trades),
                    "failures": gate_result.failures if not gate_passed else [],
                    "circuit_breaker": cb_active,
                    "low_vol_block": low_vol_block,
                })

                if gate_passed and len(self._open_trades) < effective_max:
                    trade = self._build_simulated_position(day_metrics)
                    if trade:
                        self._open_trades.append(trade)

            # Update equity curve
            open_pnl = sum(
                self._compute_open_pnl(t) for t in self._open_trades
            )
            realized_pnl = sum(t.pnl_inr for t in self._closed_trades)
            self._current_nav = self.cfg.initial_aum + realized_pnl + open_pnl
            self._peak_nav = max(self._peak_nav, self._current_nav)
            drawdown = (
                (self._current_nav - self._peak_nav) / self._peak_nav * 100
                if self._peak_nav > 0 else 0.0
            )

            self._equity_curve.append({
                "date": current_date.isoformat(),
                "nav": round(self._current_nav, 0),
                "drawdown": round(drawdown, 2),
            })

            prev_spot = spot

        # Force-close remaining open trades
        if self._open_trades:
            last = daily_metrics[-1]
            for trade in list(self._open_trades):
                self._close_trade(
                    trade, last["close"], last["vix"],
                    last["date"], "backtest_end",
                )

        result = self._compute_results()
        result.run_time_sec = time.time() - start_time

        log.info(
            "backtest.completed",
            trades=result.total_trades,
            return_pct=round(result.total_return_pct, 2),
            sharpe=round(result.sharpe_ratio, 2),
            run_time=round(result.run_time_sec, 2),
        )
        return result

    # ── Data Fetching ─────────────────────────────────────────────

    def _fetch_historical_data(
        self, start: date, end: date,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        lookback_days = self.cfg.ivp_lookback + 60
        fetch_from = start - timedelta(days=lookback_days + 60)

        # Resolve spot instrument from UNDERLYING_INSTRUMENTS config
        instrument_key = UNDERLYING_INSTRUMENTS.get(self.cfg.symbol)
        if not instrument_key:
            raise ValueError(
                f"Symbol '{self.cfg.symbol}' not found in UNDERLYING_INSTRUMENTS config."
            )
        exchange, tradingsymbol = instrument_key.split(":", 1)
        spot_token = self.broker.get_instrument_token(tradingsymbol, exchange)
        vix_token = self.broker.get_instrument_token("INDIA VIX", "NSE")

        if spot_token is None or vix_token is None:
            raise ValueError(
                f"Cannot resolve instrument tokens for {self.cfg.symbol} "
                f"('{tradingsymbol}' on {exchange}) or INDIA VIX. "
                "Ensure Kite session is active and instruments are cached."
            )

        log.info(
            "backtest.fetching_data",
            symbol=self.cfg.symbol,
            spot_token=spot_token,
            vix_token=vix_token,
            from_date=fetch_from.isoformat(),
            to_date=end.isoformat(),
        )

        spot_data = self.broker.get_historical_data(
            instrument_token=spot_token,
            from_date=fetch_from,
            to_date=end,
            interval="day",
        )

        vix_data = self.broker.get_historical_data(
            instrument_token=vix_token,
            from_date=fetch_from,
            to_date=end,
            interval="day",
        )

        log.info(
            "backtest.data_fetched",
            spot_candles=len(spot_data),
            vix_candles=len(vix_data),
        )
        return spot_data, vix_data

    # ── Daily Metrics Computation ─────────────────────────────────

    def _compute_daily_metrics(
        self,
        spot_data: List[Dict[str, Any]],
        vix_data: List[Dict[str, Any]],
        backtest_start: date,
    ) -> List[Dict[str, Any]]:
        vix_by_date: Dict[date, float] = {}
        for v in vix_data:
            d = v["date"].date() if hasattr(v["date"], "date") else v["date"]
            vix_by_date[d] = v["close"]

        spot_records = []
        for s in spot_data:
            d = s["date"].date() if hasattr(s["date"], "date") else s["date"]
            vix_close = vix_by_date.get(d)
            if vix_close is not None and vix_close > 0:
                spot_records.append({
                    "date": d,
                    "close": s["close"],
                    "high": s["high"],
                    "low": s["low"],
                    "vix": vix_close,
                })

        daily_metrics = []
        closes = [r["close"] for r in spot_records]
        vix_values = [r["vix"] for r in spot_records]

        for i, record in enumerate(spot_records):
            if record["date"] < backtest_start:
                continue

            rv = None
            ivp = None
            iv_rv_spread = None
            iv_rv_ratio = None

            # 30-day realized volatility
            if i >= self.cfg.rv_lookback:
                window_closes = closes[i - self.cfg.rv_lookback: i + 1]
                log_returns = [
                    math.log(window_closes[j] / window_closes[j - 1])
                    for j in range(1, len(window_closes))
                ]
                rv = float(np.std(log_returns)) * math.sqrt(
                    TRADING_DAYS_PER_YEAR
                ) * 100

            # IV percentile from VIX rank over trailing 252 days
            if i >= self.cfg.ivp_lookback:
                trailing_vix = vix_values[i - self.cfg.ivp_lookback: i]
                current_vix = record["vix"]
                count_below = sum(1 for v in trailing_vix if v < current_vix)
                ivp = count_below / len(trailing_vix) * 100

            if rv is not None and rv > 0:
                iv_rv_spread = record["vix"] - rv
                iv_rv_ratio = record["vix"] / rv

            daily_metrics.append({
                "date": record["date"],
                "close": record["close"],
                "high": record["high"],
                "low": record["low"],
                "vix": record["vix"],
                "rv": rv,
                "ivp": ivp,
                "iv_rv_spread": iv_rv_spread,
                "iv_rv_ratio": iv_rv_ratio,
            })

        return daily_metrics

    # ── VRP Gate Check ────────────────────────────────────────────

    def _check_vrp_gate(self, metrics: Dict[str, Any]):
        put_skew = self.cfg.put_skew_default
        if metrics["vix"] > self.cfg.vix_skew_threshold:
            put_skew = 1.2 + (metrics["vix"] - self.cfg.vix_skew_threshold) * 0.02
            put_skew = min(put_skew, 1.5)

        surface = IVSurface(
            symbol=self.cfg.symbol,
            spot=metrics["close"],
            iv_30d=metrics["vix"],
            rv_30d=metrics["rv"],
            iv_rank=metrics["ivp"],
            iv_percentile=metrics["ivp"],
            put_skew_ratio=put_skew,
            iv_rv_ratio=metrics["iv_rv_ratio"],
            iv_rv_spread=metrics["iv_rv_spread"],
        )
        return self.vrp_validator.validate(surface)

    # ── Position Building ─────────────────────────────────────────

    def _build_simulated_position(
        self, metrics: Dict[str, Any],
    ) -> Optional[SimulatedTrade]:
        spot = metrics["close"]
        vix = metrics["vix"]
        sigma = vix / 100.0
        entry_date = metrics["date"]
        expiry_date = entry_date + timedelta(days=self.cfg.entry_dte)
        T = self.cfg.entry_dte / 365.0

        put_skew = self.cfg.put_skew_default
        if vix > self.cfg.vix_skew_threshold:
            put_skew = 1.2 + (vix - self.cfg.vix_skew_threshold) * 0.02

        surface = IVSurface(
            symbol=self.cfg.symbol, spot=spot,
            iv_30d=vix, rv_30d=metrics["rv"],
            iv_rank=metrics["ivp"], iv_percentile=metrics["ivp"],
            put_skew_ratio=put_skew,
            iv_rv_ratio=metrics["iv_rv_ratio"],
            iv_rv_spread=metrics["iv_rv_spread"],
        )

        strategy, short_delta, wing_delta = self.strategy_selector.select(
            surface, vix
        )

        atm = round(spot / self._strike_step) * self._strike_step
        strikes_above = [atm + i * self._strike_step for i in range(1, 30)]
        strikes_below = [atm - i * self._strike_step for i in range(1, 30)]

        legs: List[SimulatedLeg] = []

        if strategy in (StrategyType.IRON_CONDOR, StrategyType.STRANGLE):
            sc_strike = self._find_closest_delta_strike(
                strikes_above, spot, T, sigma, OptionType.CALL, short_delta,
            )
            sp_strike = self._find_closest_delta_strike(
                strikes_below, spot, T, sigma, OptionType.PUT, short_delta,
            )
            if sc_strike is None or sp_strike is None:
                return None

            sc_price = self.bs.price(spot, sc_strike, T, sigma, OptionType.CALL)
            sp_price = self.bs.price(spot, sp_strike, T, sigma, OptionType.PUT)
            sc_greeks = self.bs.greeks(spot, sc_strike, T, sigma, OptionType.CALL)
            sp_greeks = self.bs.greeks(spot, sp_strike, T, sigma, OptionType.PUT)

            legs.append(SimulatedLeg(
                strike=sc_strike, option_type=OptionType.CALL,
                is_long=False, entry_price=sc_price,
                current_price=sc_price, delta_at_entry=sc_greeks.delta,
            ))
            legs.append(SimulatedLeg(
                strike=sp_strike, option_type=OptionType.PUT,
                is_long=False, entry_price=sp_price,
                current_price=sp_price, delta_at_entry=sp_greeks.delta,
            ))

            if strategy == StrategyType.IRON_CONDOR:
                lc_candidates = [s for s in strikes_above if s > sc_strike]
                lc_strike = self._find_closest_delta_strike(
                    lc_candidates, spot, T, sigma, OptionType.CALL, wing_delta,
                )
                lp_candidates = [s for s in strikes_below if s < sp_strike]
                lp_strike = self._find_closest_delta_strike(
                    lp_candidates, spot, T, sigma, OptionType.PUT, wing_delta,
                )
                if lc_strike and lp_strike:
                    lc_price = self.bs.price(
                        spot, lc_strike, T, sigma, OptionType.CALL,
                    )
                    lp_price = self.bs.price(
                        spot, lp_strike, T, sigma, OptionType.PUT,
                    )
                    legs.append(SimulatedLeg(
                        strike=lc_strike, option_type=OptionType.CALL,
                        is_long=True, entry_price=lc_price,
                        current_price=lc_price,
                        delta_at_entry=self.bs.greeks(
                            spot, lc_strike, T, sigma, OptionType.CALL,
                        ).delta,
                    ))
                    legs.append(SimulatedLeg(
                        strike=lp_strike, option_type=OptionType.PUT,
                        is_long=True, entry_price=lp_price,
                        current_price=lp_price,
                        delta_at_entry=self.bs.greeks(
                            spot, lp_strike, T, sigma, OptionType.PUT,
                        ).delta,
                    ))

        elif strategy == StrategyType.PUT_SPREAD:
            sp_strike = self._find_closest_delta_strike(
                strikes_below, spot, T, sigma, OptionType.PUT, 0.30,
            )
            if sp_strike is None:
                return None
            lp_candidates = [s for s in strikes_below if s < sp_strike]
            lp_strike = self._find_closest_delta_strike(
                lp_candidates, spot, T, sigma, OptionType.PUT, short_delta,
            )
            if lp_strike is None:
                return None

            sp_price = self.bs.price(spot, sp_strike, T, sigma, OptionType.PUT)
            lp_price = self.bs.price(spot, lp_strike, T, sigma, OptionType.PUT)

            legs.append(SimulatedLeg(
                strike=sp_strike, option_type=OptionType.PUT,
                is_long=False, entry_price=sp_price, current_price=sp_price,
                delta_at_entry=self.bs.greeks(
                    spot, sp_strike, T, sigma, OptionType.PUT,
                ).delta,
            ))
            legs.append(SimulatedLeg(
                strike=lp_strike, option_type=OptionType.PUT,
                is_long=True, entry_price=lp_price, current_price=lp_price,
                delta_at_entry=self.bs.greeks(
                    spot, lp_strike, T, sigma, OptionType.PUT,
                ).delta,
            ))

        if not legs:
            return None

        # Net credit calculation
        short_premium = sum(l.entry_price for l in legs if not l.is_long)
        long_premium = sum(l.entry_price for l in legs if l.is_long)
        net_credit = short_premium - long_premium
        net_credit *= (1.0 - self.cfg.slippage_pct)

        if net_credit <= 0:
            return None

        # Max loss from wing widths
        long_legs = [l for l in legs if l.is_long]
        short_legs = [l for l in legs if not l.is_long]
        if long_legs:
            max_width = 0.0
            for sl in short_legs:
                for ll in long_legs:
                    if sl.option_type == ll.option_type:
                        max_width = max(max_width, abs(sl.strike - ll.strike))
            max_loss_per_lot = (max_width - net_credit) * self.cfg.lot_size
        else:
            max_loss_per_lot = net_credit * self.cfg.lot_size * 5

        lots = self.sizing.compute_lots(
            aum=self._current_nav,
            max_loss_inr=max_loss_per_lot if max_loss_per_lot > 0 else 1,
            lot_size=self.cfg.lot_size,
        )

        return SimulatedTrade(
            trade_id=str(uuid.uuid4())[:8],
            entry_date=entry_date,
            symbol=self.cfg.symbol,
            sector=SYMBOL_SECTOR.get(self.cfg.symbol, ""),
            strategy=strategy,
            entry_spot=spot,
            entry_vix=vix,
            legs=legs,
            entry_credit=net_credit,
            max_profit=net_credit,
            max_loss=max_loss_per_lot * lots,
            lots=lots,
            expiry_date=expiry_date,
        )

    # ── Strike Selection ──────────────────────────────────────────

    def _find_closest_delta_strike(
        self,
        strike_list: List[float],
        spot: float,
        T: float,
        sigma: float,
        option_type: OptionType,
        target_delta: float,
    ) -> Optional[float]:
        best_strike = None
        best_diff = float("inf")
        for strike in strike_list[:20]:
            g = self.bs.greeks(spot, strike, T, sigma, option_type)
            diff = abs(abs(g.delta) - target_delta)
            if diff < best_diff:
                best_diff = diff
                best_strike = strike
        return best_strike

    # ── Mark-to-Market ────────────────────────────────────────────

    def _mark_to_market(
        self,
        trade: SimulatedTrade,
        spot: float,
        vix: float,
        current_date: date,
    ) -> None:
        sigma = vix / 100.0
        remaining_days = (trade.expiry_date - current_date).days
        T = max(remaining_days, 0) / 365.0

        for leg in trade.legs:
            if T <= 0:
                if leg.option_type == OptionType.CALL:
                    leg.current_price = max(spot - leg.strike, 0.0)
                else:
                    leg.current_price = max(leg.strike - spot, 0.0)
            else:
                leg.current_price = self.bs.price(
                    spot, leg.strike, T, sigma, leg.option_type,
                )

    # ── Exit Conditions ───────────────────────────────────────────

    def _check_exit_conditions(
        self,
        trade: SimulatedTrade,
        current_date: date,
        spot: float,
        vix: float,
        prev_spot: Optional[float] = None,
    ) -> Optional[str]:
        """
        Priority-ordered exit checks matching DynamicHedgingEngine.evaluate_position().
        Returns exit reason string or None.
        """
        remaining_days = (trade.expiry_date - current_date).days

        # ── 0. Expiry-Day Exit (DTE <= 1) — SEBI margin surcharge
        if remaining_days <= 1:
            return "expiry_day_exit"

        # ── 1. Gamma Exit (configurable DTE threshold)
        if remaining_days <= self.cfg.gamma_exit_dte:
            return "gamma_exit"

        # ── 2. Profit Target
        current_pnl = self._compute_open_pnl_points(trade)
        if (trade.max_profit > 0
                and current_pnl >= trade.max_profit * self.cfg.profit_target_pct):
            return "profit_target"

        # ── 3. Vanna Close — IV collapsed >= 20% from entry AND spot flat
        if trade.entry_vix > 0:
            iv_drop = (trade.entry_vix - vix) / trade.entry_vix
            spot_drift = abs(spot - trade.entry_spot) / trade.entry_spot if trade.entry_spot > 0 else 1.0
            if (iv_drop >= LIFECYCLE_CFG.vanna_iv_drop_pct
                    and spot_drift <= LIFECYCLE_CFG.vanna_price_band_pct):
                return "vanna_close"

        # ── 4. Low-Vol Regime — VIX < 12
        # In-position: skip (live engine reduces notional / widens, not force-exit).
        # New entries are blocked in the run() loop via low_vol_block.

        # ── 5. Delta Trigger with impulsive vs drift classification
        sigma = vix / 100.0
        T = remaining_days / 365.0
        for leg in trade.legs:
            if leg.is_long:
                continue
            g = self.bs.greeks(spot, leg.strike, T, sigma, leg.option_type)
            if abs(g.delta) > self.cfg.delta_trigger:
                # Classify: impulsive (>1.5% daily move) vs gradual drift
                if prev_spot and prev_spot > 0:
                    daily_move_pct = abs(spot - prev_spot) / prev_spot
                    if daily_move_pct > 0.015:
                        return "delta_impulsive_close"
                # Gradual drift — roll challenged leg instead of closing
                return "delta_roll_challenged"

            # ── 6. Untested Side Roll (strict two-condition guard)
            # Only after challenged side was already rolled AND IV recovered
            if trade.challenged_side_rolled:
                iv_change = abs(vix - trade.entry_vix) / trade.entry_vix if trade.entry_vix > 0 else 1.0
                if iv_change <= GREEKS_CFG.untested_iv_return_threshold:
                    # This leg is the untested side (not breached) — check if it can be narrowed
                    if abs(g.delta) < 0.10:  # Far OTM, worth rolling inward
                        return "untested_side_roll"

            # ── 7. ITM breach + theta neutral check
            if leg.option_type == OptionType.CALL:
                breach_pct = (spot - leg.strike) / leg.strike if leg.strike > 0 else 0
            else:
                breach_pct = (leg.strike - spot) / leg.strike if leg.strike > 0 else 0
            if breach_pct > GREEKS_CFG.itm_breach_pct:
                # Theta neutralized proxy: option price has tripled from entry
                # (deep ITM, intrinsic dominates, theta exhausted)
                theta_neutralized = (
                    leg.entry_price > 0
                    and leg.current_price > leg.entry_price * 3.0
                )
                if theta_neutralized:
                    return "itm_breach_roll_out"

        return None

    # ── Roll Simulation ────────────────────────────────────────────

    def _simulate_roll_challenged(
        self,
        trade: SimulatedTrade,
        spot: float,
        vix: float,
        current_date: date,
    ) -> None:
        """
        Simulate rolling the challenged (delta-breached) short leg.
        Close the breached leg at current price, open a new leg at 16D in next expiry.
        """
        sigma = vix / 100.0
        remaining_days = (trade.expiry_date - current_date).days
        T = max(remaining_days, 1) / 365.0

        for i, leg in enumerate(trade.legs):
            if leg.is_long:
                continue
            g = self.bs.greeks(spot, leg.strike, T, sigma, leg.option_type)
            if abs(g.delta) > self.cfg.delta_trigger:
                # Cost to close current leg (buy back at current price)
                close_cost = leg.current_price

                # Find new strike at ~16D in next expiry cycle (+30 days)
                new_expiry_days = remaining_days + 30
                new_T = new_expiry_days / 365.0
                target_delta = GREEKS_CFG.roll_to_delta

                # Generate strike candidates
                strike_range = [
                    spot + (j * self._strike_step)
                    if leg.option_type == OptionType.CALL
                    else spot - (j * self._strike_step)
                    for j in range(1, 30)
                ]
                new_strike = self._find_closest_delta_strike(
                    strike_range, spot, new_T, sigma, leg.option_type, target_delta,
                )

                if new_strike:
                    new_price = self.bs.price(spot, new_strike, new_T, sigma, leg.option_type)
                    roll_debit = close_cost - new_price  # Positive = net debit

                    # Replace the leg
                    trade.legs[i] = SimulatedLeg(
                        strike=new_strike,
                        option_type=leg.option_type,
                        is_long=False,
                        entry_price=new_price,
                        current_price=new_price,
                        delta_at_entry=target_delta,
                    )
                    trade.expiry_date = current_date + timedelta(days=new_expiry_days)
                    trade.challenged_side_rolled = True
                    trade.roll_count += 1
                    trade.cumulative_roll_debit += roll_debit

                    log.debug(
                        "backtest.roll_challenged",
                        trade_id=trade.trade_id,
                        old_strike=leg.strike,
                        new_strike=new_strike,
                        roll_debit=round(roll_debit, 2),
                    )
                break  # Only roll the first breached leg

    # ── Roll-Out Simulation (ITM breach + theta neutral) ──────────

    def _simulate_roll_out(
        self,
        trade: SimulatedTrade,
        spot: float,
        vix: float,
        current_date: date,
    ) -> None:
        """
        Simulate rolling out an ITM-breached leg to the next expiry.
        Unlike roll_challenged (which finds a new 16D strike), roll-out
        keeps the same strike but extends to the next expiry cycle (+30 days)
        to collect time value on the same position.
        """
        sigma = vix / 100.0
        remaining_days = (trade.expiry_date - current_date).days
        T = max(remaining_days, 1) / 365.0

        for i, leg in enumerate(trade.legs):
            if leg.is_long:
                continue

            # Check ITM breach condition
            if leg.option_type == OptionType.CALL:
                breach_pct = (spot - leg.strike) / leg.strike if leg.strike > 0 else 0
            else:
                breach_pct = (leg.strike - spot) / leg.strike if leg.strike > 0 else 0

            if breach_pct <= GREEKS_CFG.itm_breach_pct:
                continue
            # Theta neutralized proxy: option price has tripled from entry
            if not (leg.entry_price > 0 and leg.current_price > leg.entry_price * 3.0):
                continue

            # Cost to close current leg (buy back at current price)
            close_cost = leg.current_price

            # Roll to next expiry cycle (+30 days) at SAME strike
            new_expiry_days = remaining_days + 30
            new_T = new_expiry_days / 365.0

            new_price = self.bs.price(spot, leg.strike, new_T, sigma, leg.option_type)
            roll_debit = close_cost - new_price  # Positive = net debit

            # Replace the leg with same strike, new expiry
            new_greeks = self.bs.greeks(spot, leg.strike, new_T, sigma, leg.option_type)
            trade.legs[i] = SimulatedLeg(
                strike=leg.strike,
                option_type=leg.option_type,
                is_long=False,
                entry_price=new_price,
                current_price=new_price,
                delta_at_entry=abs(new_greeks.delta),
            )
            trade.expiry_date = current_date + timedelta(days=new_expiry_days)
            trade.roll_count += 1
            trade.cumulative_roll_debit += roll_debit

            log.debug(
                "backtest.roll_out",
                trade_id=trade.trade_id,
                strike=leg.strike,
                old_expiry_days=remaining_days,
                new_expiry_days=new_expiry_days,
                roll_debit=round(roll_debit, 2),
            )
            break  # Only roll the first breached leg

    # ── Untested Side Roll Simulation ──────────────────────────────

    def _simulate_roll_untested(
        self,
        trade: SimulatedTrade,
        spot: float,
        vix: float,
        current_date: date,
    ) -> None:
        """
        After the challenged side has been rolled and IV has recovered,
        roll the untested (far OTM) side inward to ~20D to collect additional credit.
        """
        sigma = vix / 100.0
        remaining_days = (trade.expiry_date - current_date).days
        T = max(remaining_days, 1) / 365.0
        target_delta = GREEKS_CFG.untested_roll_delta  # 0.20 (20D)

        for i, leg in enumerate(trade.legs):
            if leg.is_long:
                continue
            g = self.bs.greeks(spot, leg.strike, T, sigma, leg.option_type)
            # Untested side is far OTM (delta < 10)
            if abs(g.delta) >= 0.10:
                continue

            # Close current far-OTM leg
            close_cost = leg.current_price

            # Find new strike at ~20D in current expiry
            strike_range = [
                spot + (j * self._strike_step)
                if leg.option_type == OptionType.CALL
                else spot - (j * self._strike_step)
                for j in range(1, 30)
            ]
            new_strike = self._find_closest_delta_strike(
                strike_range, spot, T, sigma, leg.option_type, target_delta,
            )

            if new_strike:
                new_price = self.bs.price(spot, new_strike, T, sigma, leg.option_type)
                # Rolling inward should generate a credit (new strike closer to ATM)
                roll_credit = new_price - close_cost  # Positive = net credit
                roll_debit = -roll_credit  # For consistency with cumulative_roll_debit

                new_greeks = self.bs.greeks(spot, new_strike, T, sigma, leg.option_type)
                trade.legs[i] = SimulatedLeg(
                    strike=new_strike,
                    option_type=leg.option_type,
                    is_long=False,
                    entry_price=new_price,
                    current_price=new_price,
                    delta_at_entry=abs(new_greeks.delta),
                )
                trade.roll_count += 1
                trade.cumulative_roll_debit += roll_debit

                log.debug(
                    "backtest.roll_untested",
                    trade_id=trade.trade_id,
                    old_strike=leg.strike,
                    new_strike=new_strike,
                    roll_credit=round(roll_credit, 2),
                )
            break  # Only roll one untested leg

    # ── P&L Computation ───────────────────────────────────────────

    def _compute_open_pnl_points(self, trade: SimulatedTrade) -> float:
        pnl = 0.0
        for leg in trade.legs:
            sign = -1.0 if leg.is_long else 1.0
            pnl += sign * (leg.entry_price - leg.current_price)
        return pnl

    def _compute_open_pnl(self, trade: SimulatedTrade) -> float:
        return (
            self._compute_open_pnl_points(trade)
            * trade.lots
            * self.cfg.lot_size
        )

    # ── Trade Closing ─────────────────────────────────────────────

    def _close_trade(
        self,
        trade: SimulatedTrade,
        spot: float,
        vix: float,
        current_date: date,
        reason: str,
    ) -> None:
        self._mark_to_market(trade, spot, vix, current_date)

        pnl_points = self._compute_open_pnl_points(trade)
        pnl_points *= (1.0 - self.cfg.slippage_pct)

        trade.exit_date = current_date
        trade.exit_spot = spot
        trade.exit_vix = vix
        trade.pnl_points = pnl_points

        # Deduct round-trip transaction costs (STT, exchange, SEBI, GST, stamp, brokerage)
        tx_legs = [
            {"price": l.entry_price, "is_long": l.is_long}
            for l in trade.legs
        ]
        tx_cost = self.tx_cost.compute_round_trip_cost(
            tx_legs, self.cfg.lot_size, trade.lots
        )
        trade.pnl_inr = (pnl_points * trade.lots * self.cfg.lot_size) - tx_cost
        # Deduct cumulative roll debits (cost of rolling legs during the trade)
        trade.pnl_inr -= trade.cumulative_roll_debit * trade.lots * self.cfg.lot_size

        trade.exit_reason = reason
        trade.holding_days = (current_date - trade.entry_date).days

        self._open_trades.remove(trade)
        self._closed_trades.append(trade)

    # ── Results Computation ───────────────────────────────────────

    def _compute_results(self) -> BacktestResult:
        result = BacktestResult()
        result.config = {
            "symbol": self.cfg.symbol,
            "initial_aum": self.cfg.initial_aum,
            "start_date": (
                self.cfg.start_date.isoformat()
                if isinstance(self.cfg.start_date, date)
                else "auto"
            ),
            "end_date": (
                self.cfg.end_date.isoformat()
                if isinstance(self.cfg.end_date, date)
                else "auto"
            ),
            "lot_size": self.cfg.lot_size,
            "entry_dte": self.cfg.entry_dte,
        }

        trades = self._closed_trades
        result.total_trades = len(trades)

        if not trades:
            result.equity_curve = self._equity_curve
            result.signal_history = self._signal_history
            return result

        pnl_list = [t.pnl_inr for t in trades]
        winners = [p for p in pnl_list if p > 0]
        losers = [p for p in pnl_list if p <= 0]

        result.win_rate = len(winners) / len(pnl_list) * 100
        result.avg_trade_pnl = sum(pnl_list) / len(pnl_list)
        result.avg_holding_days = sum(t.holding_days for t in trades) / len(trades)

        # Total return
        final_nav = (
            self._equity_curve[-1]["nav"]
            if self._equity_curve
            else self.cfg.initial_aum
        )
        result.total_return_pct = (
            (final_nav - self.cfg.initial_aum) / self.cfg.initial_aum * 100
        )

        # CAGR
        if self._equity_curve and len(self._equity_curve) > 1:
            days = (
                date.fromisoformat(self._equity_curve[-1]["date"])
                - date.fromisoformat(self._equity_curve[0]["date"])
            ).days
            years = max(days / 365.0, 0.01)
            ratio = final_nav / self.cfg.initial_aum
            if ratio > 0:
                result.cagr = (ratio ** (1 / years) - 1) * 100

        # Max drawdown
        drawdowns = [e["drawdown"] for e in self._equity_curve]
        result.max_drawdown_pct = min(drawdowns) if drawdowns else 0.0

        # Sharpe and Sortino (daily returns)
        if len(self._equity_curve) > 1:
            navs = [e["nav"] for e in self._equity_curve]
            daily_returns = [
                (navs[i] - navs[i - 1]) / navs[i - 1]
                for i in range(1, len(navs))
                if navs[i - 1] > 0
            ]
            if daily_returns:
                avg_ret = float(np.mean(daily_returns))
                std_ret = float(np.std(daily_returns))
                if std_ret > 0:
                    result.sharpe_ratio = (
                        avg_ret / std_ret
                    ) * math.sqrt(TRADING_DAYS_PER_YEAR)

                negative_returns = [r for r in daily_returns if r < 0]
                if negative_returns:
                    downside_std = float(np.std(negative_returns))
                    if downside_std > 0:
                        result.sortino_ratio = (
                            avg_ret / downside_std
                        ) * math.sqrt(TRADING_DAYS_PER_YEAR)

        # Profit factor
        gross_profit = sum(winners) if winners else 0
        gross_loss = abs(sum(losers)) if losers else 0
        result.profit_factor = (
            gross_profit / gross_loss if gross_loss > 0 else float("inf")
        )

        # Monthly returns
        monthly: Dict[Tuple[int, int], float] = {}
        for trade in trades:
            if trade.exit_date:
                key = (trade.exit_date.year, trade.exit_date.month)
                monthly.setdefault(key, 0.0)
                monthly[key] += trade.pnl_inr
        result.monthly_returns = [
            {
                "year": year,
                "month": month,
                "return_pct": round(pnl / self.cfg.initial_aum * 100, 2),
                "return_inr": round(pnl, 0),
            }
            for (year, month), pnl in sorted(monthly.items())
        ]

        # Trade log
        result.trades = [
            {
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "entry_date": t.entry_date.isoformat(),
                "exit_date": (
                    t.exit_date.isoformat() if t.exit_date else None
                ),
                "strategy": t.strategy.value,
                "entry_spot": round(t.entry_spot, 0),
                "exit_spot": round(t.exit_spot, 0),
                "entry_vix": round(t.entry_vix, 1),
                "exit_vix": round(t.exit_vix, 1),
                "legs": [
                    {
                        "strike": l.strike,
                        "type": l.option_type.value,
                        "side": "long" if l.is_long else "short",
                        "entry_price": round(l.entry_price, 2),
                        "exit_price": round(l.current_price, 2),
                    }
                    for l in t.legs
                ],
                "lots": t.lots,
                "credit": round(t.entry_credit, 2),
                "pnl_points": round(t.pnl_points, 2),
                "pnl_inr": round(t.pnl_inr, 0),
                "holding_days": t.holding_days,
                "exit_reason": t.exit_reason,
                "roll_count": t.roll_count,
                "roll_debit": round(t.cumulative_roll_debit, 2),
            }
            for t in trades
        ]

        result.equity_curve = self._equity_curve
        result.signal_history = self._signal_history

        return result


# ─────────────────────────────────────────────────────────────────
# BATCH BACKTEST ENGINE (multi-symbol)
# ─────────────────────────────────────────────────────────────────

@dataclass
class BacktestBatchResult:
    """Aggregated results from a multi-symbol VRP backtest."""
    portfolio_summary: Dict[str, Any] = field(default_factory=dict)
    per_symbol_summary: List[Dict[str, Any]] = field(default_factory=list)
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
    monthly_returns: List[Dict[str, Any]] = field(default_factory=list)
    trades: List[Dict[str, Any]] = field(default_factory=list)
    signal_history: List[Dict[str, Any]] = field(default_factory=list)
    config: Dict[str, Any] = field(default_factory=dict)
    run_time_sec: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": "batch",
            "summary": self.portfolio_summary,
            "per_symbol_summary": self.per_symbol_summary,
            "equity_curve": self.equity_curve,
            "monthly_returns": self.monthly_returns,
            "trades": self.trades,
            "signal_history": self.signal_history,
            "config": self.config,
            "run_time_sec": round(self.run_time_sec, 2),
        }


class BacktestBatchEngine:
    """
    Runs the VRP backtest across multiple symbols with equal capital allocation.
    Aggregates per-symbol results into a portfolio-level view.
    """

    def __init__(self, broker, config: Optional[BacktestConfig] = None):
        self.broker = broker
        self.cfg = config or BacktestConfig()

    def run(self) -> BacktestBatchResult:
        start_time = time.time()
        symbols = self.cfg.symbols
        if not symbols:
            symbols = BACKTEST_STOCK_SYMBOLS

        per_symbol_aum = self.cfg.initial_aum / len(symbols)
        total_initial_aum = self.cfg.initial_aum

        end = self.cfg.end_date or date.today()
        start = self.cfg.start_date or (end - timedelta(days=365))

        per_symbol_results: List[Tuple[str, BacktestResult]] = []
        failed_symbols: List[str] = []

        for i, symbol in enumerate(symbols):
            log.info(
                "backtest.batch_progress",
                symbol=symbol,
                progress=f"{i + 1}/{len(symbols)}",
            )
            sym_config = BacktestConfig(
                symbol=symbol,
                start_date=start,
                end_date=end,
                initial_aum=per_symbol_aum,
                lot_size=None,  # auto-resolve
                risk_free_rate=self.cfg.risk_free_rate,
                rv_lookback=self.cfg.rv_lookback,
                ivp_lookback=self.cfg.ivp_lookback,
                max_concurrent=self.cfg.max_concurrent,
                max_loss_pct=self.cfg.max_loss_pct,
                entry_dte=self.cfg.entry_dte,
                profit_target_pct=self.cfg.profit_target_pct,
                gamma_exit_dte=self.cfg.gamma_exit_dte,
                delta_trigger=self.cfg.delta_trigger,
                put_skew_default=self.cfg.put_skew_default,
                vix_skew_threshold=self.cfg.vix_skew_threshold,
                slippage_pct=self.cfg.slippage_pct,
            )
            try:
                engine = BacktestEngine(broker=self.broker, config=sym_config)
                result = engine.run()
                per_symbol_results.append((symbol, result))
            except Exception as exc:
                log.warning(
                    "backtest.batch_symbol_failed",
                    symbol=symbol,
                    error=str(exc),
                )
                failed_symbols.append(symbol)

        return self._aggregate_results(
            per_symbol_results, failed_symbols,
            total_initial_aum, per_symbol_aum, start, end,
            time.time() - start_time,
        )

    def _aggregate_results(
        self,
        per_symbol_results: List[Tuple[str, BacktestResult]],
        failed_symbols: List[str],
        total_initial_aum: float,
        per_symbol_aum: float,
        start: date,
        end: date,
        elapsed: float,
    ) -> BacktestBatchResult:
        batch = BacktestBatchResult()
        batch.run_time_sec = elapsed
        batch.config = {
            "mode": "batch",
            "symbols_requested": len(self.cfg.symbols) or len(BACKTEST_STOCK_SYMBOLS),
            "symbols_succeeded": len(per_symbol_results),
            "symbols_failed": len(failed_symbols),
            "failed_symbols": failed_symbols,
            "initial_aum": total_initial_aum,
            "per_symbol_aum": round(per_symbol_aum, 0),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "entry_dte": self.cfg.entry_dte,
        }

        if not per_symbol_results:
            batch.portfolio_summary = self._empty_summary()
            return batch

        # ── Per-symbol summaries ────────────────────────────────────
        for symbol, result in per_symbol_results:
            summary = result.to_dict().get("summary", {})
            summary["symbol"] = symbol
            summary["sector"] = SYMBOL_SECTOR.get(symbol, "")
            batch.per_symbol_summary.append(summary)

        # Sort by total return descending
        batch.per_symbol_summary.sort(
            key=lambda s: s.get("total_return_pct", 0), reverse=True,
        )

        # ── Combine all trades ──────────────────────────────────────
        for _symbol, result in per_symbol_results:
            batch.trades.extend(result.trades)

        # Sort trades by entry date
        batch.trades.sort(key=lambda t: t.get("entry_date", ""))

        # ── Aggregate equity curve by date ──────────────────────────
        nav_by_date: Dict[str, float] = {}
        for _symbol, result in per_symbol_results:
            for point in result.equity_curve:
                d = point["date"]
                nav_by_date[d] = nav_by_date.get(d, 0.0) + point["nav"]

        sorted_dates = sorted(nav_by_date.keys())
        peak_nav = 0.0
        for d in sorted_dates:
            nav = nav_by_date[d]
            peak_nav = max(peak_nav, nav)
            dd = (nav - peak_nav) / peak_nav * 100 if peak_nav > 0 else 0.0
            batch.equity_curve.append({
                "date": d,
                "nav": round(nav, 0),
                "drawdown": round(dd, 2),
            })

        # ── Portfolio-level KPIs from combined equity curve ─────────
        final_nav = batch.equity_curve[-1]["nav"] if batch.equity_curve else total_initial_aum
        total_return_pct = (final_nav - total_initial_aum) / total_initial_aum * 100

        cagr = 0.0
        if len(batch.equity_curve) > 1:
            days = (
                date.fromisoformat(batch.equity_curve[-1]["date"])
                - date.fromisoformat(batch.equity_curve[0]["date"])
            ).days
            years = max(days / 365.0, 0.01)
            ratio = final_nav / total_initial_aum
            if ratio > 0:
                cagr = (ratio ** (1 / years) - 1) * 100

        max_drawdown = min(
            (e["drawdown"] for e in batch.equity_curve), default=0.0,
        )

        sharpe = 0.0
        sortino = 0.0
        if len(batch.equity_curve) > 1:
            navs = [e["nav"] for e in batch.equity_curve]
            daily_returns = [
                (navs[i] - navs[i - 1]) / navs[i - 1]
                for i in range(1, len(navs))
                if navs[i - 1] > 0
            ]
            if daily_returns:
                avg_ret = float(np.mean(daily_returns))
                std_ret = float(np.std(daily_returns))
                if std_ret > 0:
                    sharpe = avg_ret / std_ret * math.sqrt(TRADING_DAYS_PER_YEAR)
                neg_rets = [r for r in daily_returns if r < 0]
                if neg_rets:
                    downside_std = float(np.std(neg_rets))
                    if downside_std > 0:
                        sortino = avg_ret / downside_std * math.sqrt(TRADING_DAYS_PER_YEAR)

        all_pnls = [t.get("pnl_inr", 0) for t in batch.trades]
        total_trades = len(all_pnls)
        winners = [p for p in all_pnls if p > 0]
        losers = [p for p in all_pnls if p <= 0]
        win_rate = len(winners) / total_trades * 100 if total_trades else 0
        avg_pnl = sum(all_pnls) / total_trades if total_trades else 0
        avg_holding = (
            sum(t.get("holding_days", 0) for t in batch.trades) / total_trades
            if total_trades else 0
        )
        gross_profit = sum(winners) if winners else 0
        gross_loss = abs(sum(losers)) if losers else 0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        batch.portfolio_summary = {
            "total_return_pct": round(total_return_pct, 2),
            "cagr": round(cagr, 2),
            "sharpe_ratio": round(sharpe, 2),
            "sortino_ratio": round(sortino, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "win_rate": round(win_rate, 1),
            "avg_trade_pnl": round(avg_pnl, 0),
            "avg_holding_days": round(avg_holding, 1),
            "total_trades": total_trades,
            "profit_factor": round(profit_factor, 2),
        }

        # ── Monthly returns (aggregate from all trades) ─────────────
        monthly: Dict[Tuple[int, int], float] = {}
        for t in batch.trades:
            exit_dt = t.get("exit_date")
            if exit_dt:
                d = date.fromisoformat(exit_dt)
                key = (d.year, d.month)
                monthly.setdefault(key, 0.0)
                monthly[key] += t.get("pnl_inr", 0)
        batch.monthly_returns = [
            {
                "year": year,
                "month": month,
                "return_pct": round(pnl / total_initial_aum * 100, 2),
                "return_inr": round(pnl, 0),
            }
            for (year, month), pnl in sorted(monthly.items())
        ]

        log.info(
            "backtest.batch_completed",
            symbols=len(per_symbol_results),
            failed=len(failed_symbols),
            total_trades=total_trades,
            return_pct=round(total_return_pct, 2),
            run_time=round(elapsed, 2),
        )

        return batch

    @staticmethod
    def _empty_summary() -> Dict[str, Any]:
        return {
            "total_return_pct": 0.0,
            "cagr": 0.0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "max_drawdown_pct": 0.0,
            "win_rate": 0.0,
            "avg_trade_pnl": 0.0,
            "avg_holding_days": 0.0,
            "total_trades": 0,
            "profit_factor": 0.0,
        }
