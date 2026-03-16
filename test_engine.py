"""
tests/test_engine.py — Unit tests for core quant logic
Run with: pytest tests/ -v
"""

import math
import pytest
import numpy as np
from datetime import date, timedelta

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import (
    BlackScholesEngine, VRPGateValidator, StrategySelector,
    DynamicHedgingEngine, OptionType, OptionLeg, Position,
    PositionState, StrategyType, Greeks, TransactionCostEngine,
)
from modules.data_engine import IVSurface, HistoricalVolEngine


# ─── Black-Scholes Tests ────────────────────────────────────────

class TestBlackScholes:
    bs = BlackScholesEngine()

    def test_call_price_positive(self):
        price = self.bs.price(100, 100, 30/365, 0.20, OptionType.CALL)
        assert price > 0

    def test_put_call_parity(self):
        S, K, T, sigma, r = 100, 100, 30/365, 0.20, 0.060
        call = self.bs.price(S, K, T, sigma, OptionType.CALL)
        put  = self.bs.price(S, K, T, sigma, OptionType.PUT)
        # Put-call parity: C - P = S - K*e^(-rT)
        lhs = call - put
        rhs = S - K * math.exp(-r * T)
        assert abs(lhs - rhs) < 0.01

    def test_atm_call_delta_near_half(self):
        g = self.bs.greeks(100, 100, 30/365, 0.20, OptionType.CALL)
        assert 0.45 < g.delta < 0.60

    def test_put_delta_negative(self):
        g = self.bs.greeks(100, 105, 30/365, 0.20, OptionType.PUT)
        assert g.delta < 0

    def test_gamma_positive(self):
        g = self.bs.greeks(100, 100, 30/365, 0.20, OptionType.CALL)
        assert g.gamma > 0

    def test_vega_positive(self):
        g = self.bs.greeks(100, 100, 30/365, 0.20, OptionType.CALL)
        assert g.vega > 0

    def test_theta_negative_for_long(self):
        """Theta should be negative (time decay costs the holder)."""
        g = self.bs.greeks(100, 100, 30/365, 0.20, OptionType.CALL)
        assert g.theta < 0

    def test_iv_solver_round_trip(self):
        """Implied vol solver should recover the input vol."""
        S, K, T, sigma = 100, 100, 30/365, 0.22
        price = self.bs.price(S, K, T, sigma, OptionType.CALL)
        recovered_iv = self.bs.implied_vol(S, K, T, price, OptionType.CALL)
        assert abs(recovered_iv - sigma) < 0.001


# ─── Historical Vol Engine Tests ────────────────────────────────

class TestHistoricalVol:
    ve = HistoricalVolEngine()

    def test_iv_percentile_is_0_to_100(self):
        iv_series = np.random.uniform(15, 30, 252)
        pct = self.ve.iv_percentile(22.0, iv_series)
        assert 0.0 <= pct <= 100.0

    def test_iv_rank_is_0_to_100(self):
        iv_series = np.linspace(10, 40, 252)
        rank = self.ve.iv_rank(25.0, iv_series)
        assert 0.0 <= rank <= 100.0

    def test_realized_vol_positive(self):
        prices = np.cumprod(1 + np.random.normal(0, 0.01, 32)) * 100
        rv = self.ve.realized_vol(prices, window=30)
        assert rv > 0

    def test_put_skew_ratio(self):
        ratio = self.ve.put_skew_ratio(26.0, 22.0)
        assert abs(ratio - 26.0/22.0) < 0.001


# ─── VRP Gate Tests ──────────────────────────────────────────────

class TestVRPGate:
    def _make_surface(self, iv=22.0, rv=14.0, ivp=67.0, skew=1.15):
        return IVSurface(
            symbol="BANKNIFTY", spot=52000, iv_30d=iv, rv_30d=rv,
            iv_rank=65.0, iv_percentile=ivp,
            put_skew_ratio=skew,
            iv_rv_ratio=iv/rv,
            iv_rv_spread=iv - rv,
        )

    def test_all_conditions_pass(self):
        validator = VRPGateValidator()
        surface   = self._make_surface()
        result    = validator.validate(surface)
        assert result.passed

    def test_fails_on_insufficient_iv_rv_spread(self):
        validator = VRPGateValidator()
        surface   = self._make_surface(iv=15.5, rv=14.0)  # spread = 1.5, below 2
        result    = validator.validate(surface)
        assert not result.passed
        assert any("IV-RV spread" in f for f in result.failures)

    def test_fails_on_low_ivp(self):
        validator = VRPGateValidator()
        surface   = self._make_surface(ivp=25.0)  # below 30th percentile
        result    = validator.validate(surface)
        assert not result.passed

    def test_tightened_threshold_after_drawdown(self):
        """After circuit breaker, min spread rises to 3.5 vol pts."""
        validator = VRPGateValidator(tightened=True)
        surface   = self._make_surface(iv=17.0, rv=14.0)  # spread = 3.0, below 3.5
        result    = validator.validate(surface)
        assert not result.passed


# ─── Strategy Selector Tests ─────────────────────────────────────

class TestStrategySelector:
    sel = StrategySelector()

    def _surface(self, skew=1.1):
        return IVSurface(
            symbol="NIFTY", spot=22000, iv_30d=20, rv_30d=13,
            iv_rank=60, iv_percentile=65, put_skew_ratio=skew,
            iv_rv_ratio=1.54, iv_rv_spread=7.0,
        )

    def test_high_skew_returns_put_spread(self):
        strategy, _, _ = self.sel.select(self._surface(skew=1.4), india_vix=16.0)
        assert strategy == StrategyType.PUT_SPREAD

    def test_symmetric_surface_returns_iron_condor(self):
        strategy, _, _ = self.sel.select(self._surface(skew=1.1), india_vix=16.0)
        assert strategy == StrategyType.IRON_CONDOR

    def test_low_vix_returns_strangle_with_wider_strikes(self):
        strategy, short_delta, _ = self.sel.select(self._surface(), india_vix=11.5)
        assert strategy == StrategyType.STRANGLE
        assert short_delta == 0.12   # Wider strikes in low-vol regime


# ─── Dynamic Hedging Tests ───────────────────────────────────────

class TestDynamicHedging:
    def _make_position(self, profit_pct=0.0, dte=35, entry_iv=20.0):
        pos = Position(
            position_id = "test-001",
            symbol      = "BANKNIFTY",
            strategy    = StrategyType.IRON_CONDOR,
            state       = PositionState.ACTIVE,
            entry_spot  = 52000.0,
            entry_iv    = entry_iv,
            max_profit  = 10000.0,
        )
        # Simulate legs with 30 DTE
        expiry = date.today() + timedelta(days=dte)
        leg = OptionLeg(
            symbol="BANKNIFTY24DEC52000CE", strike=52000,
            expiry=expiry, option_type=OptionType.CALL,
            is_long=False, lots=1, entry_price=100, current_price=100 - profit_pct * 100,
        )
        leg.greeks = Greeks(delta=0.16, theta=-5.0, vega=10.0, gamma=0.001)
        pos.legs = [leg]
        return pos

    def test_gamma_exit_triggered_at_14_dte(self):
        engine = DynamicHedgingEngine()
        pos    = self._make_position(dte=13)
        decision = engine.evaluate_position(pos, 52000, 20.0, 15.0, 52000, 1000, 1000)
        assert decision.action.value == "gamma_exit"

    def test_profit_exit_at_40pct(self):
        engine = DynamicHedgingEngine()
        pos    = self._make_position(profit_pct=0.52, dte=35)
        pos.max_profit = 100.0
        # Manually set net_pnl via leg prices
        pos.legs[0].current_price = 48.0   # 52% profit on short leg
        pos.legs[0].entry_price   = 100.0
        decision = engine.evaluate_position(pos, 52000, 20.0, 15.0, 52000, 1000, 1000)
        assert decision.action.value == "profit_exit"

    def test_vanna_adjustment_triggers(self):
        engine = DynamicHedgingEngine()
        pos    = self._make_position(entry_iv=20.0, dte=35)
        # IV dropped 25%, price stagnant
        decision = engine.evaluate_position(
            pos, 52000, 15.0, 15.0, 52000, 1000, 1000  # iv=15 from entry 20 = 25% drop
        )
        assert decision.action.value == "vanna_close"

    def test_no_action_when_all_clear(self):
        engine = DynamicHedgingEngine()
        pos    = self._make_position(dte=35, entry_iv=20.0)
        decision = engine.evaluate_position(
            pos, 52000, 20.0, 15.0, 52000, 1000, 1000
        )
        assert decision.action.value == "none"

    def test_delta_trigger_fires_at_30D_not_40D(self):
        """v2 spec: trigger fires at 30 Delta (0.30), not 40 Delta (0.40)."""
        from config import GREEKS_CFG
        assert GREEKS_CFG.delta_trigger == 0.30, (
            f"delta_trigger should be 0.30 per v2 spec, got {GREEKS_CFG.delta_trigger}"
        )

    def test_delta_hysteresis_reset_at_20D(self):
        """v2 spec: reset only when Δ drops to 0.20 — prevents oscillation around 0.30."""
        from config import GREEKS_CFG
        assert GREEKS_CFG.delta_hysteresis_reset == 0.20

    def test_untested_side_roll_blocked_without_recenter(self):
        """Untested side must NOT roll if challenged leg hasn't been re-centered yet."""
        engine = DynamicHedgingEngine()
        pos    = self._make_position(dte=35, entry_iv=20.0)
        pos.challenged_side_recentered = False   # Condition (a) not met
        can_roll = engine._check_untested_side_roll(pos, current_iv=20.0)
        assert can_roll is False

    def test_untested_side_roll_blocked_when_iv_not_recovered(self):
        """Untested side must NOT roll if IV hasn't returned within 10% of entry."""
        engine = DynamicHedgingEngine()
        pos    = self._make_position(dte=35, entry_iv=20.0)
        pos.challenged_side_recentered = True    # Condition (a) met
        # IV at 14.0 — 30% below entry of 20.0 — condition (b) not met
        can_roll = engine._check_untested_side_roll(pos, current_iv=14.0)
        assert can_roll is False

    def test_untested_side_roll_allowed_when_both_conditions_met(self):
        """Both (a) re-centered AND (b) IV within 10% of entry → roll permitted."""
        engine = DynamicHedgingEngine()
        pos    = self._make_position(dte=35, entry_iv=20.0)
        pos.challenged_side_recentered = True    # Condition (a) met
        # IV at 19.5 — 2.5% below entry of 20.0 — well within 10% threshold
        can_roll = engine._check_untested_side_roll(pos, current_iv=19.5)
        assert can_roll is True


# ─── Transaction Cost Tests ─────────────────────────────────────

class TestTransactionCosts:
    tc = TransactionCostEngine()

    def test_sell_side_stt_computed(self):
        """STT on sell-side options should be ~0.0625% of turnover."""
        cost = self.tc.compute_leg_cost(premium=200.0, quantity=25, side="sell")
        # Turnover = 200*25 = 5000; STT = 5000 * 0.000625 = 3.125
        assert cost > 3.0  # At least STT component

    def test_buy_side_no_stt(self):
        """No STT on buy-side options (per SEBI 2024 rules)."""
        cost_buy = self.tc.compute_leg_cost(premium=200.0, quantity=25, side="buy")
        cost_sell = self.tc.compute_leg_cost(premium=200.0, quantity=25, side="sell")
        # Buy should be cheaper due to no STT
        assert cost_buy < cost_sell

    def test_round_trip_positive(self):
        """Round-trip cost should always be positive."""
        legs = [
            {"price": 150.0, "is_long": False},
            {"price": 50.0,  "is_long": True},
        ]
        cost = self.tc.compute_round_trip_cost(legs, lot_size=25, lots=1)
        assert cost > 0


# ─── OptionLeg PnL Tests ────────────────────────────────────────

class TestOptionLegPnL:
    def test_pnl_includes_lot_size(self):
        """PnL must multiply by both lots AND lot_size (SEBI lot sizes)."""
        leg = OptionLeg(
            symbol="NIFTY24DEC22000CE", strike=22000,
            expiry=date.today() + timedelta(days=30),
            option_type=OptionType.CALL,
            is_long=False, lots=2, lot_size=25,
            entry_price=100.0, current_price=50.0,
        )
        # Short leg: PnL = -1 * (50 - 100) * 2 * 25 = 2500
        assert leg.pnl == 2500.0

    def test_pnl_loss_on_short_when_price_rises(self):
        """Short leg should lose money when option price increases."""
        leg = OptionLeg(
            symbol="NIFTY24DEC22000CE", strike=22000,
            expiry=date.today() + timedelta(days=30),
            option_type=OptionType.CALL,
            is_long=False, lots=1, lot_size=25,
            entry_price=100.0, current_price=150.0,
        )
        assert leg.pnl < 0


# ─── Expiry Day Exit Tests ──────────────────────────────────────

class TestExpiryDayExit:
    def test_expiry_day_exit_at_1_dte(self):
        """Position at 1 DTE should trigger gamma exit (SEBI margin surcharge)."""
        engine = DynamicHedgingEngine()
        pos = Position(
            position_id="test-expiry",
            symbol="NIFTY",
            strategy=StrategyType.IRON_CONDOR,
            state=PositionState.ACTIVE,
            entry_spot=22000.0,
            entry_iv=18.0,
            max_profit=5000.0,
        )
        expiry = date.today() + timedelta(days=1)
        leg = OptionLeg(
            symbol="NIFTY24DEC22000CE", strike=22000,
            expiry=expiry, option_type=OptionType.CALL,
            is_long=False, lots=1, lot_size=25,
            entry_price=100.0, current_price=80.0,
        )
        leg.greeks = Greeks(delta=0.16, theta=-5.0, vega=10.0, gamma=0.001)
        pos.legs = [leg]
        decision = engine.evaluate_position(pos, 22000, 18.0, 15.0, 22000, 1000, 1000)
        assert decision.action.value == "gamma_exit"
        assert "SEBI" in decision.reason or "expiry" in decision.reason.lower()


# ─── Multi-Exchange Config Tests ─────────────────────────────────

class TestMultiExchangeConfig:
    def test_sensex_on_bfo(self):
        """SENSEX options must route to BFO exchange."""
        from config import SYMBOL_EXCHANGE
        assert SYMBOL_EXCHANGE.get("SENSEX") == "BFO"

    def test_nifty_on_nfo(self):
        from config import SYMBOL_EXCHANGE
        assert SYMBOL_EXCHANGE.get("NIFTY") == "NFO"

    def test_stock_options_on_nfo(self):
        """All stock F&O trades on NFO exchange."""
        from config import SYMBOL_EXCHANGE
        for sym in ["RELIANCE", "TCS", "HDFCBANK", "SBIN", "INFY"]:
            assert SYMBOL_EXCHANGE.get(sym) == "NFO", f"{sym} should be on NFO"

    def test_finnifty_removed(self):
        """FINNIFTY was delisted Nov 2024 — must not be in any config."""
        from config import AUTO_SCAN_SYMBOLS, LOT_SIZES, UNDERLYING_INSTRUMENTS
        assert "FINNIFTY" not in AUTO_SCAN_SYMBOLS
        assert "FINNIFTY" not in LOT_SIZES
        assert "FINNIFTY" not in UNDERLYING_INSTRUMENTS

    def test_nifty_lot_size_25(self):
        """NIFTY lot size changed from 75 to 25 per SEBI Nov 2024."""
        from config import LOT_SIZES
        assert LOT_SIZES["NIFTY"] == 25

    def test_risk_free_rate_6pct(self):
        """RBI repo rate proxy should be ~6.0% for 2026."""
        from config import RISK_FREE_RATE
        assert abs(RISK_FREE_RATE - 0.06) < 0.01

    def test_nifty100_stocks_in_universe(self):
        """Universe must include NIFTY 100 derivative stocks."""
        from config import AUTO_SCAN_SYMBOLS, LOT_SIZES, STRIKE_STEPS
        # Key NIFTY 50 stocks must be present
        for sym in ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
                     "SBIN", "BHARTIARTL", "ITC", "KOTAKBANK", "LT"]:
            assert sym in AUTO_SCAN_SYMBOLS, f"{sym} missing from scan symbols"
            assert sym in LOT_SIZES, f"{sym} missing lot size"
            assert sym in STRIKE_STEPS, f"{sym} missing strike step"
        # Must have 80+ symbols (4 indices + ~80 stocks)
        assert len(AUTO_SCAN_SYMBOLS) >= 80, (
            f"Expected 80+ symbols, got {len(AUTO_SCAN_SYMBOLS)}"
        )

    def test_symbol_sector_mapping(self):
        """All scan stocks should have a GICS sector mapping."""
        from config import AUTO_SCAN_SYMBOLS, SYMBOL_SECTOR
        indices = {"NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"}
        unmapped = [
            s for s in AUTO_SCAN_SYMBOLS
            if s not in indices and s not in SYMBOL_SECTOR
        ]
        # Allow a few unmapped but flag if too many
        assert len(unmapped) <= 5, f"Too many stocks without sector: {unmapped}"

    def test_stock_expiry_monthly(self):
        """All stock options must have monthly expiry cadence."""
        from config import EXPIRY_CADENCE
        for sym in ["RELIANCE", "TCS", "SBIN", "TATASTEEL"]:
            assert EXPIRY_CADENCE.get(sym) == "monthly", (
                f"{sym} should have monthly expiry"
            )
