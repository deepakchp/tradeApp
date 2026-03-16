"""
engine.py — Quant Core & Dynamic Hedging Engine
================================================
Institutional Volatility Desk "VRP" Framework v2
(NSE/BSE Variance Risk Premium — NIFTY + Top-10 High-Beta Stocks)

Phase 1 — VRP Scan & Skew Analysis:
  1.  VRPGateValidator   — 3-condition simultaneous gate (IV-RV spread, IVP,
                           IV/RV ratio)
  2.  StrategySelector   — Skew-aware structure selection:
                             Put Skew > 1.3 + term rich → PUT_CALENDAR
                             Put Skew > 1.3             → PUT_SPREAD
                             Call Skew > 1.2 (symmetric) → RATIO_CALL_SPREAD
                             Symmetric                  → IRON_CONDOR (16D/10D)
                             VIX < 12                   → STRANGLE (12D, low-vol protocol)

Phase 2 — Portfolio Greeks, Stress Testing & Beta-Weighting:
  3.  BlackScholesEngine      — Full BSM Greeks (Δ, Θ, ν, Γ, Vanna) via scipy
  4.  PortfolioGreeksEngine   — Beta-weighted delta (±10 units/₹1Cr)
                                Vega Layer-1: ≤ 0.25% AUM per VIX point
                                Vega Layer-2: VIX-doubling stress ≤ 8% AUM
                                Pairwise correlation (20-day rolling)
                                Enforcement: position limit, sector cap, corr constraint

Phase 3 — Execution & Gamma-Flip Protocol:
  5.  DynamicHedgingEngine    — Priority-ordered adjustment decisions:
                                  Gamma exit (21 DTE) → Profit exit (50%)
                                  Vanna close → Cooldown guard → Low-vol adjust
                                  Delta trigger (30D, hysteresis at 20D)
                                  Untested side roll (strict two-condition guard)
                                  ITM breach roll-out
  6.  SlippageController      — Limit-chase algorithm (mid → walk, max 1% budget)

Phase 4 — Position Sizing & Portfolio Construction:
  7.  SizingEngine            — Worst-case 2% AUM / name; 1% premium budget
  8.  CircuitBreakerManager   — Monthly drawdown monitor: > 4% AUM triggers
                                  50% position-count reduction + raised IV-RV gate (7 pts)
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.stats import norm

import structlog

from config import (
    GREEKS_CFG, LIFECYCLE_CFG, RISK_CFG, VRP_GATE, SKEW_CFG,
    SLIPPAGE_CFG, WHIPSAW_CFG, RISK_FREE_RATE, NIFTY_DIV_YIELD,
    TXCOST_CFG, VRP_INDEX_SYMBOLS,
)
from modules.data_engine import IVSurface

log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────
# ENUMS & DATA MODELS
# ─────────────────────────────────────────────────────────────────

class OptionType(str, Enum):
    CALL = "CE"
    PUT  = "PE"

class StrategyType(str, Enum):
    IRON_CONDOR       = "iron_condor"
    STRANGLE          = "strangle"
    PUT_SPREAD        = "put_spread"
    PUT_CALENDAR      = "put_calendar"
    RATIO_CALL_SPREAD = "ratio_call_spread"   # Low put-skew + elevated call IV
    BWB               = "broken_wing_butterfly"

class PositionState(str, Enum):
    PENDING    = "pending"
    ACTIVE     = "active"
    ADJUSTING  = "adjusting"
    CLOSING    = "closing"
    CLOSED     = "closed"


@dataclass
class Greeks:
    delta:  float = 0.0
    theta:  float = 0.0
    vega:   float = 0.0
    gamma:  float = 0.0
    vanna:  float = 0.0    # dΔ/dσ — used in Vanna adjustment logic


@dataclass
class OptionLeg:
    symbol:      str
    strike:      float
    expiry:      date
    option_type: OptionType
    is_long:     bool           # True = long, False = short
    lots:        int
    lot_size:    int   = 1     # NSE/BSE contract lot size (NIFTY=25, BANKNIFTY=30, SENSEX=10)
    exchange:    str   = "NFO" # "NFO" for NSE F&O, "BFO" for BSE F&O (SENSEX)
    entry_price: float = 0.0
    current_price: float = 0.0
    greeks:      Greeks = field(default_factory=Greeks)

    @property
    def signed_delta(self) -> float:
        """Delta from portfolio perspective (long = positive, short = negative)."""
        sign = 1.0 if self.is_long else -1.0
        put_sign = -1.0 if self.option_type == OptionType.PUT else 1.0
        return sign * put_sign * abs(self.greeks.delta) * self.lots

    @property
    def dte(self) -> int:
        return max(0, (self.expiry - date.today()).days)

    @property
    def pnl(self) -> float:
        sign = 1.0 if self.is_long else -1.0
        return sign * (self.current_price - self.entry_price) * self.lots * self.lot_size


@dataclass
class Position:
    position_id: str
    symbol:      str                    # Underlying (e.g. "BANKNIFTY")
    strategy:    StrategyType
    legs:        List[OptionLeg] = field(default_factory=list)
    state:       PositionState   = PositionState.PENDING
    entry_time:  datetime = field(default_factory=datetime.utcnow)
    entry_spot:  float = 0.0
    entry_iv:    float = 0.0            # IV at entry (for Vanna check)
    max_profit:  float = 0.0            # Max theoretical credit
    beta:        float = 1.0            # Beta vs NIFTY 50
    last_adjustment_ts: float = 0.0     # Unix timestamp of last adjustment
    challenged_side_recentered: bool = False  # True after challenged leg has been rolled
    # Used for untested-side roll guard (v2 spec: BOTH conditions must be true)

    @property
    def net_pnl(self) -> float:
        return sum(leg.pnl for leg in self.legs)

    @property
    def profit_pct(self) -> float:
        return self.net_pnl / self.max_profit if self.max_profit != 0 else 0.0

    @property
    def portfolio_delta(self) -> float:
        return sum(leg.signed_delta for leg in self.legs)

    @property
    def portfolio_vega(self) -> float:
        total = 0.0
        for leg in self.legs:
            sign = 1.0 if leg.is_long else -1.0
            total += sign * leg.greeks.vega * leg.lots
        return total

    @property
    def min_dte(self) -> int:
        return min(leg.dte for leg in self.legs) if self.legs else 0


# ─────────────────────────────────────────────────────────────────
# BLACK-SCHOLES ENGINE
# ─────────────────────────────────────────────────────────────────

class BlackScholesEngine:
    """
    Fast BSM Greeks calculator using Cython.
    Supports continuous dividend yield for index options (Merton model).
    Risk-free rate defaults to RBI repo rate proxy (~6.0% in 2026).
    """

    def __init__(self, risk_free_rate: float = None, dividend_yield: float = 0.0):
        self.r = risk_free_rate if risk_free_rate is not None else RISK_FREE_RATE
        self.q = dividend_yield  # Continuous dividend yield (NIFTY ~1.3%)

    def price(self, S, K, T, sigma, option_type: OptionType) -> float:
        from modules import fast_greeks
        # Using fast_greeks
        return fast_greeks.price(S, K, T, sigma, option_type.value, self.r, self.q)

    def greeks(self, S, K, T, sigma, option_type: OptionType) -> Greeks:
        from modules import fast_greeks
        res_dict = fast_greeks.greeks(S, K, T, sigma, option_type.value, self.r, self.q)
        return Greeks(
            delta=res_dict["delta"],
            gamma=res_dict["gamma"],
            theta=res_dict["theta"],
            vega=res_dict["vega"],
            vanna=res_dict["vanna"]
        )

    def implied_vol(
        self,
        S: float, K: float, T: float,
        market_price: float,
        option_type: OptionType,
        tol: float = 1e-6,
        max_iter: int = 200,
    ) -> float:
        """Newton-Raphson fast IV solver via Cython."""
        from modules import fast_greeks
        return fast_greeks.implied_vol(S, K, T, market_price, option_type.value, self.r, self.q, tol, max_iter)


# ─────────────────────────────────────────────────────────────────
# VRP GATE VALIDATOR
# ─────────────────────────────────────────────────────────────────

@dataclass
class VRPGateResult:
    passed:       bool
    iv_rv_spread: float
    ivp:          float
    iv_rv_ratio:  float
    failures:     List[str] = field(default_factory=list)


class VRPGateValidator:
    """
    Validates all three entry conditions for VRP trades.
    All conditions must pass simultaneously — any failure rejects the trade.
    """

    def __init__(self, tightened: bool = False):
        self.cfg = VRP_GATE
        self.tightened = tightened  # True after circuit-breaker month

    def validate(self, surface: IVSurface) -> VRPGateResult:
        failures = []
        is_index = surface.symbol in VRP_INDEX_SYMBOLS

        # Select tier thresholds: Tier 1 (Index) vs Tier 2 (Stock)
        if is_index:
            min_spread   = self.cfg.min_iv_rv_spread_pts
            min_ivp      = self.cfg.min_ivp_percentile
            min_ratio    = self.cfg.min_iv_rv_ratio
        else:
            min_spread   = self.cfg.stock_min_iv_rv_spread_pts
            min_ivp      = self.cfg.stock_min_ivp_percentile
            min_ratio    = self.cfg.stock_min_iv_rv_ratio

        # Override spread threshold if circuit-breaker is active
        if self.tightened:
            min_spread = self.cfg.tightened_iv_rv_spread

        # Condition 1: Absolute IV-RV spread
        if surface.iv_rv_spread <= min_spread:
            failures.append(
                f"IV-RV spread {surface.iv_rv_spread:.2f} ≤ {min_spread:.1f} vol pts"
            )

        # Condition 2: IV Percentile (dimensionally correct — pure rank)
        if surface.iv_percentile <= min_ivp:
            failures.append(
                f"IVP {surface.iv_percentile:.1f} ≤ {min_ivp:.0f}th percentile"
            )

        # Condition 3: IV/RV ratio (relative richness)
        if surface.iv_rv_ratio <= min_ratio:
            failures.append(
                f"IV/RV ratio {surface.iv_rv_ratio:.2f} ≤ {min_ratio:.2f}"
            )

        result = VRPGateResult(
            passed       = len(failures) == 0,
            iv_rv_spread = surface.iv_rv_spread,
            ivp          = surface.iv_percentile,
            iv_rv_ratio  = surface.iv_rv_ratio,
            failures     = failures,
        )

        if result.passed:
            log.info("vrp_gate.passed", symbol=surface.symbol,
                     spread=f"{surface.iv_rv_spread:.2f}", ivp=f"{surface.iv_percentile:.0f}")
        else:
            log.warning("vrp_gate.failed", symbol=surface.symbol, reasons=failures)

        return result


# ─────────────────────────────────────────────────────────────────
# STRATEGY SELECTOR
# ─────────────────────────────────────────────────────────────────

class StrategySelector:
    """
    Selects the correct structure based on skew and VIX regime.
    Implements the corrected skew logic from VRP Framework v2.

    Decision tree (evaluated top-to-bottom):
      1. VIX < 12             → Strangle at 12D (wider strikes, low-vol protocol)
      2. Put Skew > 1.3       → PUT_CALENDAR  if term structure is also rich
                                 PUT_SPREAD    otherwise
                                 (Never ratio put spreads in high put-skew — adds
                                 uncapped downside when tail risk is already priced)
      3. Call Skew > 1.2      → RATIO_CALL_SPREAD (sell elevated upside fear premium
                                 on the call side; put skew is symmetric, so no
                                 dangerous asymmetry on the downside)
      4. Symmetric surface    → IRON_CONDOR at 16D short / 10D wing
    """

    def select(
        self,
        surface: IVSurface,
        india_vix: float,
    ) -> Tuple[StrategyType, float, float]:
        """
        Returns: (strategy_type, short_delta, wing_delta)
        """
        # ── 1. Low-vol regime override ──────────────────────────────
        if india_vix < LIFECYCLE_CFG.low_vol_vix_threshold:
            short_delta = LIFECYCLE_CFG.low_vol_short_delta   # 0.12
            wing_delta  = GREEKS_CFG.long_wing_delta           # 0.10
            log.info("strategy.low_vol_regime",
                     vix=india_vix, short_delta=short_delta)
            # Wider strikes; NOT BWB (BWB is yield management only, not tail protection)
            return StrategyType.STRANGLE, short_delta, wing_delta

        # ── 2. High put skew: sell fear premium with defined downside ──
        if surface.put_skew_ratio > SKEW_CFG.put_skew_ratio_threshold:
            if surface.term_structure_rich:
                # Term structure richness adds a second dimension of edge — exploit both
                log.info("strategy.high_skew_put_calendar",
                         skew=surface.put_skew_ratio,
                         threshold=SKEW_CFG.put_skew_ratio_threshold)
                return StrategyType.PUT_CALENDAR, 0.30, GREEKS_CFG.short_delta_target
            # Pure put-skew play: defined-risk vertical
            log.info("strategy.high_skew_put_spread",
                     skew=surface.put_skew_ratio,
                     threshold=SKEW_CFG.put_skew_ratio_threshold)
            return StrategyType.PUT_SPREAD, 0.30, GREEKS_CFG.short_delta_target

        # ── 3. Low put-skew + elevated call IV: ratio call spread ──────
        # Reserve ratio structures for the CALL side only; put skew is symmetric
        # so there is no dangerous downside asymmetry to the trade.
        if surface.call_skew_ratio > SKEW_CFG.call_skew_ratio_threshold:
            log.info("strategy.elevated_call_iv_ratio_spread",
                     call_skew=surface.call_skew_ratio,
                     threshold=SKEW_CFG.call_skew_ratio_threshold)
            return (
                StrategyType.RATIO_CALL_SPREAD,
                GREEKS_CFG.short_delta_target,
                GREEKS_CFG.long_wing_delta,
            )

        # ── 4. Symmetric surface: Ironed Strangle with defined wings ───
        short_delta = GREEKS_CFG.short_delta_target   # 0.16
        wing_delta  = GREEKS_CFG.long_wing_delta       # 0.10 minimum
        log.info("strategy.iron_condor_selected",
                 short_delta=short_delta, wing_delta=wing_delta)
        return StrategyType.IRON_CONDOR, short_delta, wing_delta


# ─────────────────────────────────────────────────────────────────
# PORTFOLIO GREEKS AGGREGATOR
# ─────────────────────────────────────────────────────────────────

class PortfolioGreeksEngine:
    """
    Aggregates Greeks across all positions with NIFTY beta-weighting.
    """

    def beta_weighted_delta(
        self,
        positions: List[Position],
        nifty_spot: float,
        aum: float,
    ) -> float:
        """
        Beta-weighted delta in Nifty-equivalent units per ₹1Cr AUM.
        Δ_β = Σ(leg_delta × lots × lot_size × beta)
        signed_delta already includes lots; multiply by lot_size and beta.
        """
        total_bwd = 0.0
        for pos in positions:
            if pos.state != PositionState.ACTIVE:
                continue
            for leg in pos.legs:
                lot_multiplier = float(leg.lot_size) if leg.lot_size > 0 else 1.0
                notional_delta = leg.signed_delta * lot_multiplier
                beta_adj_delta = notional_delta * pos.beta
                total_bwd += beta_adj_delta

        per_cr = total_bwd / (aum / 1e7)   # Normalize to per ₹1Cr
        log.debug("portfolio.beta_weighted_delta",
                  raw=round(total_bwd, 2), per_cr=round(per_cr, 2))
        return per_cr

    def total_vega(self, positions: List[Position]) -> float:
        return sum(pos.portfolio_vega for pos in positions if pos.state == PositionState.ACTIVE)

    def vix_stress_pnl(
        self,
        positions: List[Position],
        current_vix: float,
        stressed_vix: Optional[float] = None,
    ) -> float:
        """
        Layer-2 stress: estimate P&L if VIX doubles.
        Includes second-order volga correction (vega convexity) for more
        realistic loss estimation on large VIX moves.
        """
        if stressed_vix is None:
            stressed_vix = current_vix * 2.0
        vol_shock = (stressed_vix - current_vix) / 100.0
        total_vega = self.total_vega(positions)
        linear_loss = total_vega * vol_shock * 100   # vega per 1% vol move
        # Volga correction: short vega portfolios lose more than linear estimate
        # on large VIX moves due to vega convexity (~15% additional per VIX doubling)
        volga_multiplier = 1.0 + 0.15 * (vol_shock * 100 / current_vix) if current_vix > 0 else 1.0
        estimated_loss = linear_loss * volga_multiplier
        log.info("portfolio.vix_stress",
                 current_vix=current_vix, stressed_vix=stressed_vix,
                 vol_shock_pct=vol_shock * 100,
                 estimated_loss=round(estimated_loss, 0))
        return estimated_loss

    def pairwise_correlation(
        self,
        returns_matrix: np.ndarray,   # shape: (days, n_positions)
    ) -> float:
        """Return average pairwise correlation across all positions."""
        if returns_matrix.shape[1] < 2:
            return 0.0
        corr_matrix = np.corrcoef(returns_matrix.T)
        n = corr_matrix.shape[0]
        upper_tri = corr_matrix[np.triu_indices(n, k=1)]
        return float(np.mean(upper_tri))

    # ── Enforcement Methods ───────────────────────────────────────

    def check_position_limit(
        self,
        active_count: int,
        max_positions: int,
    ) -> Tuple[bool, int]:
        """
        Phase 4: Max 8 concurrent positions (reduced to 4 post circuit-breaker).
        Returns (can_add_new_position, max_allowed).
        """
        can_add = active_count < max_positions
        if not can_add:
            log.warning("portfolio.position_limit_reached",
                        active=active_count, max=max_positions)
        return can_add, max_positions

    def check_sector_concentration(
        self,
        sector_counts: Dict[str, int],
    ) -> List[str]:
        """
        Phase 2 hard cap: no more than 3 positions in the same GICS sector.
        Returns a list of sectors that are at or above the hard cap.
        Caller must block new entries in breached sectors.
        """
        breached = [
            sector for sector, count in sector_counts.items()
            if count >= RISK_CFG.max_positions_per_sector
        ]
        if breached:
            log.warning("portfolio.sector_cap_breached",
                        sectors=breached, limit=RISK_CFG.max_positions_per_sector)
        return breached

    def check_vega_layer1(
        self,
        positions: List[Position],
        aum: float,
    ) -> Tuple[bool, float]:
        """
        Layer-1 vega limit: a 1-vol-point rise in India VIX must cause ≤ 0.25% AUM drawdown.
        Portfolio is net short vega; loss = abs(total_vega) per VIX point.
        Returns (within_limit, loss_per_vix_pt_inr).
        """
        loss_per_vix_pt = abs(self.total_vega(positions))
        limit = aum * RISK_CFG.layer1_vix_pt_drawdown_pct
        within_limit = loss_per_vix_pt <= limit
        if not within_limit:
            log.warning("portfolio.vega_layer1_breach",
                        loss_per_vix_pt=round(loss_per_vix_pt, 0),
                        limit=round(limit, 0),
                        excess=round(loss_per_vix_pt - limit, 0))
        return within_limit, loss_per_vix_pt

    def check_vega_stress_limit(
        self,
        positions: List[Position],
        current_vix: float,
        aum: float,
    ) -> Tuple[bool, float]:
        """
        Layer-2 stress limit: VIX-doubling scenario loss must not exceed 8% AUM.
        Uses vix_stress_pnl() (linear vega approximation — actual loss will be higher
        due to volga; this is a conservative floor, not a ceiling).
        Returns (within_limit, estimated_stress_loss_inr).
        """
        stress_loss = abs(self.vix_stress_pnl(positions, current_vix))
        limit = aum * RISK_CFG.layer2_vix_double_max_loss_pct
        within_limit = stress_loss <= limit
        if not within_limit:
            log.warning("portfolio.vega_layer2_stress_breach",
                        stress_loss=round(stress_loss, 0),
                        limit=round(limit, 0),
                        excess_pct=round((stress_loss - limit) / aum * 100, 2))
        return within_limit, stress_loss

    def check_correlation_constraint(
        self,
        returns_matrix: np.ndarray,
    ) -> Tuple[bool, float, float]:
        """
        Phase 2: If average pairwise correlation > 0.65, reduce gross notional by 30%.
        Returns (within_limit, avg_correlation, notional_scalar).
        notional_scalar = 0.70 when limit is breached, 1.0 when within limit.
        """
        avg_corr = self.pairwise_correlation(returns_matrix)
        if avg_corr > RISK_CFG.max_avg_pairwise_corr:
            scalar = 1.0 - RISK_CFG.corr_notional_reduction
            log.warning("portfolio.correlation_limit_breached",
                        avg_corr=round(avg_corr, 3),
                        limit=RISK_CFG.max_avg_pairwise_corr,
                        notional_scalar=scalar)
            return False, avg_corr, scalar
        return True, avg_corr, 1.0


# ─────────────────────────────────────────────────────────────────
# DYNAMIC HEDGING ENGINE
# ─────────────────────────────────────────────────────────────────

class AdjustmentDecision(Enum):
    NONE               = "none"
    ROLL_CHALLENGED    = "roll_challenged_leg"
    ROLL_UNTESTED      = "roll_untested_leg"     # Only after re-center + IV recovery
    CLOSE_ALL          = "close_all"             # Impulsive move detected
    ROLL_OUT           = "roll_out"              # ITM breach + theta neutralized
    VANNA_CLOSE        = "vanna_close"           # IV collapsed, edge gone
    GAMMA_EXIT         = "gamma_exit"            # 21 DTE
    PROFIT_EXIT        = "profit_exit"           # 50% max profit
    LOW_VOL_ADJUST     = "low_vol_adjust"        # VIX < 12 protocol
    INTRADAY_REBALANCE = "intraday_rebalance"    # Nifty 2%+ move, book > ±15 units
    CORRELATION_REDUCE = "correlation_reduce"    # Avg pairwise corr > 0.65 → cut notional 30%


@dataclass
class HedgingDecision:
    action:      AdjustmentDecision
    reason:      str
    position_id: str
    leg_symbol:  Optional[str]  = None
    target_delta: Optional[float] = None
    details:     dict = field(default_factory=dict)


class DynamicHedgingEngine:
    """
    Monitors all active positions and generates hedging decisions.
    Implements hysteresis, EWMA smoothing, and cooldown to prevent whipsaw.
    """

    def __init__(self, redis_client=None):
        self.redis   = redis_client
        self.bs      = BlackScholesEngine()
        self._triggered: Dict[str, bool] = {}   # Hysteresis state per leg

    # ── Cooldown Check ───────────────────────────────────────────
    def _is_in_cooldown(self, position_id: str) -> bool:
        """
        Whipsaw prevention: returns True if an adjustment was made recently.
        Uses Redis for distributed locking; falls back to local timestamp.
        """
        key = f"{WHIPSAW_CFG.redis_cooldown_prefix}{position_id}"
        cooldown_secs = WHIPSAW_CFG.cooldown_minutes * 60

        if self.redis:
            try:
                return self.redis.exists(key) == 1
            except Exception:
                pass

        # Fallback: check last_adjustment_ts in-process
        # This is set by the caller after executing an adjustment
        return False

    def set_cooldown(self, position_id: str) -> None:
        """Set Redis cooldown key after executing an adjustment."""
        key = f"{WHIPSAW_CFG.redis_cooldown_prefix}{position_id}"
        cooldown_secs = WHIPSAW_CFG.cooldown_minutes * 60
        if self.redis:
            try:
                self.redis.setex(key, cooldown_secs, "1")
                log.info("hedging.cooldown_set",
                         position_id=position_id, minutes=WHIPSAW_CFG.cooldown_minutes)
            except Exception as e:
                log.warning("hedging.cooldown_redis_fail", error=str(e))

    # ── Delta Trigger with Hysteresis ────────────────────────────
    def _check_delta_trigger(
        self,
        leg_key:      str,
        smoothed_delta: float,
    ) -> bool:
        """
        Hysteresis gate — v2 spec: "If any short leg reaches 30 Delta"
          - Fires when |smoothed_delta| > 0.30  (30D trigger)
          - Resets only when |smoothed_delta| < 0.20  (20D reset)
        The gap between 0.20 and 0.30 prevents oscillation around the threshold
        during minor intraday drift (whipsaw prevention).
        """
        triggered = self._triggered.get(leg_key, False)

        if not triggered and abs(smoothed_delta) > GREEKS_CFG.delta_trigger:
            self._triggered[leg_key] = True
            log.warning("hedging.delta_trigger_fired",
                        leg=leg_key, delta=round(smoothed_delta, 3),
                        trigger=GREEKS_CFG.delta_trigger)
            return True

        if triggered and abs(smoothed_delta) < GREEKS_CFG.delta_hysteresis_reset:
            self._triggered[leg_key] = False
            log.info("hedging.delta_trigger_reset",
                     leg=leg_key, reset_at=GREEKS_CFG.delta_hysteresis_reset)

        return triggered and abs(smoothed_delta) > GREEKS_CFG.delta_trigger

    # ── Move Character Detection ──────────────────────────────────
    def _is_impulsive_move(
        self,
        spot_now: float,
        spot_5min_ago: float,
        volume_now: int,
        avg_volume: int,
    ) -> bool:
        """
        Classify a directional move as impulsive (close entire trade)
        vs. gradual drift (roll only the challenged leg).

        Impulsive: price moved >1.5% in 5 minutes with elevated volume.
        """
        price_move_pct = abs(spot_now - spot_5min_ago) / spot_5min_ago
        volume_spike   = volume_now > avg_volume * 1.5
        return price_move_pct > 0.015 and volume_spike

    # ── Untested Side Roll — Two-Condition Guard ─────────────────
    def _check_untested_side_roll(
        self,
        position:   Position,
        current_iv: float,
    ) -> bool:
        """
        v2 spec: Only roll the unchallenged side inward to 20D if BOTH:
          (a) challenged side has already been rolled and re-centered
          (b) IV has returned to within 10% of entry IV level

        Rolling the untested side before these conditions are met narrows
        the profit zone when the market has shown directional intent.
        """
        # Condition (a): challenged leg must have been rolled first
        if not position.challenged_side_recentered:
            return False

        # Condition (b): IV must have recovered to within 10% of entry
        if position.entry_iv <= 0:
            return False
        iv_deviation = abs(current_iv - position.entry_iv) / position.entry_iv
        iv_recovered = iv_deviation <= GREEKS_CFG.untested_iv_return_threshold

        if iv_recovered:
            log.info(
                "hedging.untested_roll_conditions_met",
                position_id=position.position_id,
                entry_iv=position.entry_iv,
                current_iv=current_iv,
                iv_deviation_pct=round(iv_deviation * 100, 1),
            )
        return iv_recovered
    def _check_itm_breach(
        self,
        leg:     OptionLeg,
        spot:    float,
        theta_neutralized: bool,
    ) -> bool:
        """
        Roll-Out trigger: short strike breached by >1% AND theta has neutralized.
        Theta neutralized = daily theta income < 20% of entry theta income.
        """
        if leg.is_long:
            return False
        breach_pct = (
            (spot - leg.strike) / leg.strike if leg.option_type == OptionType.CALL
            else (leg.strike - spot) / leg.strike
        )
        return breach_pct > GREEKS_CFG.itm_breach_pct and theta_neutralized

    # ── Vanna Adjustment Check ───────────────────────────────────
    def _check_vanna_adjustment(
        self,
        position: Position,
        current_iv: float,
        current_spot: float,
    ) -> bool:
        """
        If IV has collapsed ≥20% from entry AND spot is within 0.5% of entry,
        P&L has been pulled forward by vanna. Close early — don't wait for theta.
        """
        iv_drop = (position.entry_iv - current_iv) / position.entry_iv
        spot_drift = abs(current_spot - position.entry_spot) / position.entry_spot
        return iv_drop >= LIFECYCLE_CFG.vanna_iv_drop_pct and spot_drift <= LIFECYCLE_CFG.vanna_price_band_pct

    # ── Main Evaluation Loop ─────────────────────────────────────
    def evaluate_position(
        self,
        position:      Position,
        current_spot:  float,
        current_iv:    float,
        india_vix:     float,
        spot_5min_ago: float,
        volume_now:    int,
        avg_volume:    int,
        cache=None,    # QuoteCache for EWMA smoothing
    ) -> HedgingDecision:
        """
        Evaluate a single position and return the appropriate HedgingDecision.
        Priority order matches the framework spec.
        """
        pid = position.position_id

        # ── 0. Expiry-Day Exit (T-1 DTE) — SEBI 2% additional margin surcharge
        if position.min_dte <= 1:
            return HedgingDecision(
                action=AdjustmentDecision.GAMMA_EXIT,
                reason=(
                    f"DTE={position.min_dte} ≤ 1 — expiry-day exit "
                    f"(SEBI {RISK_CFG.expiry_day_margin_surcharge_pct*100:.0f}% margin surcharge)"
                ),
                position_id=pid,
            )

        # ── 1. Gamma Exit (21 DTE) ───────────────────────────────
        if position.min_dte <= LIFECYCLE_CFG.gamma_exit_dte:
            return HedgingDecision(
                action=AdjustmentDecision.GAMMA_EXIT,
                reason=f"DTE={position.min_dte} ≤ {LIFECYCLE_CFG.gamma_exit_dte}",
                position_id=pid,
            )

        # ── 2. Profit Target (50%) ───────────────────────────────
        if position.profit_pct >= LIFECYCLE_CFG.profit_target_pct:
            return HedgingDecision(
                action=AdjustmentDecision.PROFIT_EXIT,
                reason=f"Profit {position.profit_pct*100:.1f}% ≥ 50% max",
                position_id=pid,
            )

        # ── 3. Vanna Adjustment ──────────────────────────────────
        if self._check_vanna_adjustment(position, current_iv, current_spot):
            return HedgingDecision(
                action=AdjustmentDecision.VANNA_CLOSE,
                reason=(
                    f"IV dropped {(position.entry_iv - current_iv)/position.entry_iv*100:.1f}% "
                    f"from entry; price within {LIFECYCLE_CFG.vanna_price_band_pct*100:.1f}% "
                    f"of entry. Vanna P&L pulled forward."
                ),
                position_id=pid,
            )

        # ── 4. Cooldown Check (whipsaw prevention) ───────────────
        if self._is_in_cooldown(pid):
            log.debug("hedging.in_cooldown", position_id=pid)
            return HedgingDecision(
                action=AdjustmentDecision.NONE,
                reason="Adjustment cooldown active",
                position_id=pid,
            )

        # ── 5. Low-Vol Regime ────────────────────────────────────
        if india_vix < LIFECYCLE_CFG.low_vol_vix_threshold:
            return HedgingDecision(
                action=AdjustmentDecision.LOW_VOL_ADJUST,
                reason=f"India VIX={india_vix:.1f} < {LIFECYCLE_CFG.low_vol_vix_threshold}",
                position_id=pid,
                details={"protocol": "reduce_notional_40pct_widen_to_12D_add_tail_hedge"},
            )

        # ── 6. Delta Trigger Check ───────────────────────────────
        for leg in position.legs:
            if leg.is_long:
                continue   # Only monitor short legs

            raw_delta = abs(leg.greeks.delta)
            # EWMA smoothing to prevent whipsaw from single bad tick
            if cache:
                smoothed = cache.smoothed_delta(leg.symbol, raw_delta)
            else:
                smoothed = raw_delta

            leg_key = f"{pid}:{leg.symbol}"

            if self._check_delta_trigger(leg_key, smoothed):
                # Classify move type
                impulsive = self._is_impulsive_move(
                    current_spot, spot_5min_ago, volume_now, avg_volume
                )

                if impulsive:
                    return HedgingDecision(
                        action=AdjustmentDecision.CLOSE_ALL,
                        reason=(
                            f"Leg {leg.symbol} Δ={smoothed:.2f} > "
                            f"{GREEKS_CFG.delta_trigger} (30D). Impulsive move — close all."
                        ),
                        position_id=pid,
                        leg_symbol=leg.symbol,
                        details={"spot_now": current_spot, "spot_5m": spot_5min_ago},
                    )
                else:
                    return HedgingDecision(
                        action=AdjustmentDecision.ROLL_CHALLENGED,
                        reason=(
                            f"Leg {leg.symbol} Δ={smoothed:.2f} > "
                            f"{GREEKS_CFG.delta_trigger} (30D). Gradual drift — "
                            f"roll challenged leg to {GREEKS_CFG.roll_to_delta} in next expiry."
                        ),
                        position_id=pid,
                        leg_symbol=leg.symbol,
                        target_delta=GREEKS_CFG.roll_to_delta,
                    )

            # ── 7. Untested Side Roll (strict two-condition check) ──
            if self._check_untested_side_roll(position, current_iv):
                return HedgingDecision(
                    action=AdjustmentDecision.ROLL_UNTESTED,
                    reason=(
                        f"Both conditions met: challenged side re-centered AND "
                        f"IV ({current_iv:.1f}%) within {GREEKS_CFG.untested_iv_return_threshold*100:.0f}% "
                        f"of entry IV ({position.entry_iv:.1f}%). "
                        f"Rolling untested side inward to {GREEKS_CFG.untested_roll_delta} (20D)."
                    ),
                    position_id=pid,
                    target_delta=GREEKS_CFG.untested_roll_delta,
                )

            # ITM breach check — relative theta threshold instead of absolute
            theta_ref = abs(leg.entry_price) / 50.0 if leg.entry_price > 0 else 0.01
            theta_neutralized = abs(leg.greeks.theta) < theta_ref * 0.20
            if self._check_itm_breach(leg, current_spot, theta_neutralized):
                return HedgingDecision(
                    action=AdjustmentDecision.ROLL_OUT,
                    reason=(
                        f"Strike {leg.strike} breached by >{GREEKS_CFG.itm_breach_pct*100:.0f}% "
                        f"and theta neutralized. Roll to next expiry."
                    ),
                    position_id=pid,
                    leg_symbol=leg.symbol,
                )

        return HedgingDecision(
            action=AdjustmentDecision.NONE,
            reason="All checks passed — no action required",
            position_id=pid,
        )


# ─────────────────────────────────────────────────────────────────
# CIRCUIT BREAKER
# ─────────────────────────────────────────────────────────────────

@dataclass
class CircuitBreakerState:
    """
    Tracks intra-month drawdown and whether tightened risk parameters are active.
    Activated when monthly P&L loss exceeds 4% of AUM.
    Resets at the start of each new calendar month.
    """
    active:               bool     = False
    triggered_at:         Optional[datetime] = None
    monthly_pnl:          float    = 0.0       # Cumulative month-to-date P&L (INR)
    reduced_max_positions: int     = 4         # 50% of 8 = 4


class CircuitBreakerManager:
    """
    Phase 4 spec: After any month where drawdown exceeds 4% of AUM:
      - Reduce max concurrent positions by 50% (8 → 4)
      - Require IV−RV > 7 vol points before re-entering (VRPGateValidator.tightened = True)

    Usage:
        cb = CircuitBreakerManager(aum=10_000_000)
        state = cb.record_pnl(realized_pnl_inr)
        gate  = VRPGateValidator(tightened=cb.is_vrp_gate_tightened())
        max_p = cb.max_positions()
    """

    def __init__(self, aum: Optional[float] = None):
        self.aum = aum or RISK_CFG.aum_inr
        reduced = max(
            1,
            int(RISK_CFG.max_concurrent_positions * (1.0 - RISK_CFG.cb_position_count_reduction)),
        )
        self.state = CircuitBreakerState(reduced_max_positions=reduced)

    def record_pnl(self, pnl_inr: float) -> CircuitBreakerState:
        """
        Accumulate realized P&L for the month and activate the circuit breaker
        if the drawdown threshold is breached.
        """
        self.state.monthly_pnl += pnl_inr
        drawdown_pct = -self.state.monthly_pnl / self.aum   # Positive when losing money

        if not self.state.active and drawdown_pct >= RISK_CFG.monthly_drawdown_trigger_pct:
            self.state.active = True
            self.state.triggered_at = datetime.utcnow()
            log.warning(
                "circuit_breaker.activated",
                monthly_pnl=round(self.state.monthly_pnl, 0),
                drawdown_pct=round(drawdown_pct * 100, 2),
                max_positions=self.state.reduced_max_positions,
                new_iv_rv_threshold=VRP_GATE.tightened_iv_rv_spread,
            )
        return self.state

    def reset_month(self) -> None:
        """Call at the start of each new calendar month to reset the tracker."""
        self.state.monthly_pnl = 0.0
        self.state.active = False
        self.state.triggered_at = None
        log.info("circuit_breaker.month_reset")

    def max_positions(self) -> int:
        """Return current maximum allowed concurrent positions."""
        return (
            self.state.reduced_max_positions
            if self.state.active
            else RISK_CFG.max_concurrent_positions
        )

    def is_vrp_gate_tightened(self) -> bool:
        """
        True when circuit breaker is active — signals VRPGateValidator to use
        the elevated IV−RV spread threshold (7 vol points instead of 5).
        """
        return self.state.active


# ─────────────────────────────────────────────────────────────────
# SLIPPAGE CONTROLLER
# ─────────────────────────────────────────────────────────────────

class SlippageController:
    """
    Limit-Chase algorithm for order placement.
    Places at theoretical mid, then walks the limit incrementally
    up to the configured max slippage budget.
    """

    def __init__(self):
        self.cfg = SLIPPAGE_CFG

    def compute_limit_price(
        self,
        theoretical_mid: float,
        side: str,          # "buy" | "sell"
        chase_step: int,    # 0 = initial, 1-4 = chase steps
        vwap_anchor: Optional[float] = None,  # VWAP from orderbook depth
    ) -> float:
        """
        For buys:  walk UP  (pay more to fill)
        For sells: walk DOWN (accept less to fill)
        
        When vwap_anchor is provided (from tick-level orderbook depth),
        use it as the price anchor instead of theoretical_mid for more
        accurate execution pricing.
        """
        anchor = vwap_anchor if vwap_anchor and vwap_anchor > 0 else theoretical_mid
        offset = self.cfg.chase_step_pct * chase_step
        if side == "buy":
            return round(anchor * (1.0 + offset), 2)
        return round(anchor * (1.0 - offset), 2)

    def is_within_budget(
        self,
        theoretical_mid: float,
        actual_fill:     float,
        side: str,
    ) -> bool:
        """Returns False if slippage exceeds budget — abandon the order."""
        if theoretical_mid <= 0:
            return True
        slippage = (
            (actual_fill - theoretical_mid) / theoretical_mid if side == "buy"
            else (theoretical_mid - actual_fill) / theoretical_mid
        )
        within = slippage <= self.cfg.max_slippage_budget_pct
        if not within:
            log.warning("slippage.budget_exceeded",
                        theoretical=theoretical_mid, actual=actual_fill,
                        slippage_pct=round(slippage * 100, 3))
        return within

    def check_min_edge(
        self,
        expected_credit: float,
        transaction_cost: float,
    ) -> bool:
        """Credit must be ≥ 2× transaction costs for the trade to have edge."""
        return expected_credit >= transaction_cost * self.cfg.min_edge_multiple


# ─────────────────────────────────────────────────────────────────
# SIZING ENGINE
# ─────────────────────────────────────────────────────────────────

class SizingEngine:
    """
    Calculates position size using volatility targeting and risk constraints.
    Includes SEBI margin awareness (ELM surcharge, expiry-day surcharge).
    """

    def compute_lots(
        self,
        aum:            float,
        max_loss_inr:   float,     # Worst-case loss per position in INR
        lot_size:       int,       # NSE/BSE lot size for the instrument
        margin_per_lot: float = 0.0,  # Approximate margin required per lot
    ) -> int:
        """
        Size so that worst-case loss (short struck breached, wings expire worthless)
        ≤ 2% of AUM per position. Also caps lots to available margin (80% of AUM).
        Hard-capped at 500 lots as a safety limit.
        """
        MAX_LOTS = 500  # Hard safety cap
        budget_inr = aum * RISK_CFG.max_loss_per_name_pct
        if max_loss_inr <= 0 or lot_size <= 0:
            return 1
        lots = int(budget_inr / max_loss_inr)
        # Margin constraint: use at most 80% of AUM for total margin
        if margin_per_lot and margin_per_lot > 0:
            margin_budget = aum * 0.80
            margin_lots = int(margin_budget / margin_per_lot)
            lots = min(lots, margin_lots)
        return max(1, min(lots, MAX_LOTS))

    def check_premium_budget(
        self,
        aum:            float,
        net_premium_at_risk: float,
    ) -> bool:
        """Net premium at risk must not exceed 1% of AUM per name."""
        return net_premium_at_risk <= aum * RISK_CFG.max_premium_risk_pct


# ─────────────────────────────────────────────────────────────────
# TRANSACTION COST ENGINE  (Indian F&O — SEBI 2024 rates)
# ─────────────────────────────────────────────────────────────────

class TransactionCostEngine:
    """
    Computes total transaction costs for Indian F&O trades per SEBI 2024 rates.
    Includes STT (doubled Oct 2024), exchange charges, SEBI fee, GST, stamp duty.
    """

    def __init__(self, cfg=None):
        self.cfg = cfg or TXCOST_CFG

    def compute_leg_cost(self, premium: float, quantity: int, side: str) -> float:
        """Compute total transaction cost for a single leg (one-way)."""
        turnover = abs(premium) * quantity
        stt = turnover * (self.cfg.stt_sell_pct if side == "sell" else self.cfg.stt_buy_pct)
        exchange_fee = turnover * self.cfg.exchange_txn_pct
        sebi_fee = turnover * self.cfg.sebi_turnover_pct
        stamp = turnover * self.cfg.stamp_duty_pct if side == "buy" else 0.0
        brokerage = self.cfg.brokerage_per_order
        gst = (brokerage + exchange_fee) * self.cfg.gst_pct
        return stt + exchange_fee + sebi_fee + stamp + brokerage + gst

    def compute_round_trip_cost(
        self,
        legs: List[dict],    # [{"price": float, "is_long": bool}, ...]
        lot_size: int,
        lots: int,
    ) -> float:
        """Compute total round-trip cost for all legs (entry + exit)."""
        total = 0.0
        qty = lots * lot_size
        for leg in legs:
            price = leg.get("price", 0)
            is_long = leg.get("is_long", True)
            # Entry
            entry_side = "buy" if is_long else "sell"
            total += self.compute_leg_cost(price, qty, entry_side)
            # Exit (reverse side)
            exit_side = "sell" if is_long else "buy"
            total += self.compute_leg_cost(price, qty, exit_side)
        return total
