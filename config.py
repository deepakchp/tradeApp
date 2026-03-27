"""
config.py — Central System Configuration
All thresholds, flags, and env bindings live here.
Never hardcode a value in a module — import from here.

Updated March 2026 for:
  - SEBI lot size changes (NIFTY 75→25, Nov 2024)
  - FINNIFTY delisted (Nov 28, 2024)
  - BANKNIFTY weekly expiry discontinued (Nov 2024)
  - SENSEX weekly on BSE + MIDCPNIFTY monthly added
  - STT doubled on options (Oct 2024)
  - SEBI ELM surcharge for short options (+2%)
  - Expiry-day additional margin (+2%)
  - RBI repo rate updated to 6.0%
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from dotenv import load_dotenv

load_dotenv()


# ─────────────────────────────────────────────────────────────────
# BROKER SELECTION
# ─────────────────────────────────────────────────────────────────
BROKER = os.getenv("BROKER", "zerodha")          # "zerodha" | "angelone"
PAPER_TRADE = os.getenv("PAPER_TRADE", "true").lower() == "true"


# ─────────────────────────────────────────────────────────────────
# SECURITY  (AES-256 key must be 32 bytes, base64-encoded in env)
# ─────────────────────────────────────────────────────────────────
ENCRYPTION_KEY_B64 = os.getenv("ENCRYPTION_KEY_B64", "")   # base64(32-byte key)
ENCRYPTED_API_KEY  = os.getenv("ENCRYPTED_API_KEY",  "")   # AES-256-GCM ciphertext
ENCRYPTED_API_SECRET = os.getenv("ENCRYPTED_API_SECRET", "")

# Kite Connect credentials (loaded from .env)
KITE_API_KEY    = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")


# ─────────────────────────────────────────────────────────────────
# INFRASTRUCTURE
# ─────────────────────────────────────────────────────────────────
REDIS_URL      = os.getenv("REDIS_URL",      "redis://localhost:6379/0")
DATABASE_URL   = os.getenv("DATABASE_URL",   "postgresql://vrp:vrp@localhost:5432/vrp_db")
FLASK_SECRET   = os.getenv("FLASK_SECRET",   "change-me-in-production")
FLASK_HOST     = os.getenv("FLASK_HOST",     "0.0.0.0")
FLASK_PORT     = int(os.getenv("FLASK_PORT", "5050"))
DEBUG          = os.getenv("DEBUG",          "false").lower() == "true"
SKIP_LOGIN     = os.getenv("SKIP_LOGIN",     "false").lower() == "true"


# ─────────────────────────────────────────────────────────────────
# MARKET PARAMETERS  (updated periodically — confirm on RBI/NSE circulars)
# ─────────────────────────────────────────────────────────────────
RISK_FREE_RATE: float  = float(os.getenv("RISK_FREE_RATE",  "0.060"))   # RBI repo rate proxy (~6.0% in 2026)
NIFTY_DIV_YIELD: float = float(os.getenv("NIFTY_DIV_YIELD", "0.013"))   # ~1.3% continuous dividend yield for NIFTY 50


# ─────────────────────────────────────────────────────────────────
# VRP ENTRY GATE  (Phase 1 — all four must pass)
# Tier 1: Index (NIFTY / BANKNIFTY) — tighter thresholds
# Tier 2: NIFTY50 stocks            — standard thresholds
# ─────────────────────────────────────────────────────────────────
@dataclass
class VRPGateConfig:
    # Tier 1: Index defaults
    min_iv_rv_spread_pts:    float = 2.5     # IV-RV spread > 2.5 pts
    min_ivp_percentile:      float = 60.0    # IVP > 60th percentile
    min_iv_rv_ratio:         float = 1.20    # IV/RV ratio > 1.20
    # Tier 2: Stock overrides
    stock_min_iv_rv_spread_pts:    float = 4.5     # IV-RV spread > 4.5 pts
    stock_min_ivp_percentile:      float = 65.0    # IVP > 65th percentile
    stock_min_iv_rv_ratio:         float = 1.30    # IV/RV ratio > 1.30
    # Elevated-drawdown tightened threshold
    tightened_iv_rv_spread: float = 3.5     # After circuit-breaker month (aggressive)

VRP_GATE = VRPGateConfig()

# Symbols treated as Tier 1 (Index) for VRP gate
VRP_INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY"}


# ─────────────────────────────────────────────────────────────────
# SKEW CONFIG
# ─────────────────────────────────────────────────────────────────
@dataclass
class SkewConfig:
    put_skew_ratio_threshold:  float = 1.3   # 16D Put IV / 50D IV; above → Put Spread / Calendar
    call_skew_ratio_threshold: float = 1.2   # 16D Call IV / 50D IV; above (low put-skew) → Ratio Call Spread
    # Below put_skew_ratio_threshold and below call_skew_ratio_threshold → Ironed Strangle

SKEW_CFG = SkewConfig()


# ─────────────────────────────────────────────────────────────────
# GREEKS & POSITION CONFIG
# ─────────────────────────────────────────────────────────────────
@dataclass
class GreeksConfig:
    # Entry deltas
    short_delta_target:    float = 0.16    # 16D short strike
    long_wing_delta:       float = 0.10    # 10D long wing (minimum — not 3D cosmetic)
    # Adjustment triggers  ← v2 spec: "If any short leg reaches 30 Delta"
    delta_trigger:         float = 0.30    # Fire adjustment if leg Δ exceeds 0.30 (30D)
    delta_hysteresis_reset: float = 0.20   # Reset only when Δ drops back to 0.20 (not 0.30)
    portfolio_delta_band:  float = 15.0    # ±15 Nifty-equivalent units (intraday rebalance)
    # Roll targets after adjustment
    roll_to_delta:         float = 0.16    # Roll challenged leg to 16D in next expiry
    untested_roll_delta:   float = 0.20    # Roll untested side inward to 20D (strict conditions)
    # Untested-side roll guard — BOTH must be true before rolling untested side:
    #   (a) challenged side already re-centred
    #   (b) IV has returned to within 10% of entry IV level
    untested_iv_return_threshold: float = 0.10   # IV must be within 10% of entry IV
    itm_breach_pct:        float = 0.01    # Roll-out if strike breached by >1%
    # EWMA smoothing for whipsaw prevention
    delta_ewma_span:       int   = 5       # Ticks for EWMA smoothing

GREEKS_CFG = GreeksConfig()


# ─────────────────────────────────────────────────────────────────
# TRADE LIFECYCLE
# ─────────────────────────────────────────────────────────────────
@dataclass
class LifecycleConfig:
    entry_dte_min:    int   = 35           # Entry window: 35-55 DTE (widened)
    entry_dte_max:    int   = 55
    gamma_exit_dte:   int   = 14           # Mechanical exit at 14 DTE (hold longer)
    profit_target_pct: float = 0.40        # Close at 40% max profit (faster recycling)
    # Vanna adjustment: IV drop ≥ 20% AND underlying within 0.5% of entry price
    vanna_iv_drop_pct:    float = 0.20     # IV dropped ≥ 20% from entry
    vanna_price_band_pct: float = 0.005    # Price within 0.5% of entry (0.5% = 0.005)
    # Intraday delta rebalance: trigger if Nifty moves ≥ 2% and book > ±15 units
    nifty_intraday_rebalance_pct: float = 0.02
    # Low-vol regime
    low_vol_vix_threshold: float = 12.0
    low_vol_notional_reduction: float = 0.40   # Reduce notional 40%
    low_vol_short_delta: float = 0.12           # Widen to 12D
    low_vol_tail_budget_pct: float = 0.075      # 7.5% of portfolio budget to tail hedges

LIFECYCLE_CFG = LifecycleConfig()


# ─────────────────────────────────────────────────────────────────
# RISK / SIZING
# ─────────────────────────────────────────────────────────────────
@dataclass
class RiskConfig:
    aum_inr:                float = 10_000_000.0   # ₹1 Cr default AUM
    max_premium_risk_pct:   float = 0.01            # 1% AUM per name in net premium
    max_loss_per_name_pct:  float = 0.02            # 2% AUM worst-case per position
    max_concurrent_positions: int = 999       # No practical limit on concurrent positions
    max_positions_per_sector: int = 5       # Hard cap — GICS sector concentration limit
    # Beta-weighting
    portfolio_delta_limit_per_cr: float = 10.0     # ±10 Nifty units per ₹1Cr
    intraday_rebalance_band: float = 15.0           # Rebalance if > ±15 units
    # Vega risk — Layer 1
    layer1_vix_pt_drawdown_pct: float = 0.0025     # 0.25% AUM per 1 VIX-pt rise
    # Vega risk — Layer 2
    layer2_vix_double_max_loss_pct: float = 0.08   # 8% AUM if VIX doubles
    # Correlation constraint
    max_avg_pairwise_corr: float = 0.65
    corr_notional_reduction: float = 0.30           # Reduce 30% if breach
    corr_lookback_days: int = 20
    # Circuit breaker
    monthly_drawdown_trigger_pct: float = 0.04      # 4% drawdown triggers CB
    cb_position_count_reduction: float = 0.50       # Reduce positions 50%
    # SEBI margin surcharges (Oct 2024 circular)
    sebi_elm_surcharge_pct: float = 0.02             # Additional 2% ELM for short options
    expiry_day_margin_surcharge_pct: float = 0.02    # Additional 2% margin on expiry day
    min_contract_value_inr: float = 15_00_000        # SEBI minimum contract value Rs 15 lakhs

RISK_CFG = RiskConfig()


# ─────────────────────────────────────────────────────────────────
# SLIPPAGE CONTROL
# ─────────────────────────────────────────────────────────────────
@dataclass
class SlippageConfig:
    initial_limit_offset_pct: float = 0.0          # Start at theoretical mid
    chase_step_pct:    float = 0.0025               # Walk 0.25% per tick
    chase_interval_sec: int  = 3                    # Wait 3s before chasing
    max_chase_steps:   int   = 4                    # Max 4 steps = 1% total
    max_slippage_budget_pct: float = 0.01           # Abandon if slippage > 1%
    min_edge_multiple:  float = 2.0                 # Credit must be 2× tx costs

SLIPPAGE_CFG = SlippageConfig()


# ─────────────────────────────────────────────────────────────────
# TRANSACTION COSTS  (Indian F&O — SEBI 2024 updated rates)
# ─────────────────────────────────────────────────────────────────
@dataclass
class TransactionCostConfig:
    """Indian F&O transaction costs per SEBI 2024 revised rates."""
    stt_sell_pct:       float = 0.000625    # 0.0625% STT on sell-side premium (doubled Oct 2024)
    stt_buy_pct:        float = 0.0         # No STT on buy-side options
    exchange_txn_pct:   float = 0.0005      # NSE/BSE exchange transaction charges
    sebi_turnover_pct:  float = 0.000001    # SEBI turnover fee
    gst_pct:            float = 0.18        # 18% GST on brokerage + exchange charges
    stamp_duty_pct:     float = 0.00003     # Stamp duty on buy side
    brokerage_per_order: float = 20.0       # Flat Rs 20 per executed order (discount broker)

TXCOST_CFG = TransactionCostConfig()


# ─────────────────────────────────────────────────────────────────
# WHIPSAW PREVENTION
# ─────────────────────────────────────────────────────────────────
@dataclass
class WhipsawConfig:
    cooldown_minutes:  int   = 15           # Lock adjustments for 15 min post-trade
    redis_cooldown_prefix: str = "cooldown:"

WHIPSAW_CFG = WhipsawConfig()


# ─────────────────────────────────────────────────────────────────
# CELERY / MONITORING
# ─────────────────────────────────────────────────────────────────
MTM_MONITOR_INTERVAL_SEC = 30       # MTM check every 30 seconds
DELTA_MONITOR_INTERVAL_SEC = 10     # Delta check every 10 seconds
CELERY_TIMEZONE = "Asia/Kolkata"


# ─────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────
LOG_LEVEL   = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE    = os.getenv("LOG_FILE",  "logs/vrp_system.jsonl")


# ─────────────────────────────────────────────────────────────────
# EMAIL NOTIFICATIONS (env-var based — never hardcode credentials)
# ─────────────────────────────────────────────────────────────────
# EMAIL_SENDER       — sender Gmail address
# EMAIL_APP_PASSWORD — Gmail App Password (2FA → App Passwords)
# EMAIL_RECIPIENT    — recipient email address
# EMAIL_ENABLED      — "true" / "false" (default: "true")
# EMAIL_SMTP_HOST    — SMTP host (default: smtp.gmail.com)
# EMAIL_SMTP_PORT    — SMTP port (default: 587)
EMAIL_NOTIFICATIONS_ENABLED = os.getenv("EMAIL_ENABLED", "true").lower() == "true"


# ─────────────────────────────────────────────────────────────────
# AUTO-ENTRY SCANNER
# ─────────────────────────────────────────────────────────────────
@dataclass
class ScannerConfig:
    scan_interval_sec:  int   = 300        # Scan every 5 minutes
    market_open_time:   str   = "09:20"    # Start scanning after open (IST)
    market_close_time:  str   = "15:10"    # Stop before close (IST)
    rv_lookback_days:   int   = 30         # 30-day realized vol
    ivp_lookback_days:  int   = 252        # 1-year IV percentile lookback
    signal_ttl_minutes: int   = 30         # Signal expires after 30 min
    max_pending_signals: int  = 5          # Max queued signals
    entry_dte_target:   int   = 45         # Target ~45 DTE expiry

SCANNER_CFG = ScannerConfig()

AUTO_SCAN_SYMBOLS: List[str] = [
    # ── Index Options ─────────────────────────────────────────────
    "NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY",
    # ── NIFTY 50 F&O Stocks ──────────────────────────────────────
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "AXISBANK", "BAJFINANCE", "ASIANPAINT", "MARUTI",
    "HCLTECH", "SUNPHARMA", "TITAN", "NTPC", "TATAMOTORS",
    "ULTRACEMCO", "M&M", "WIPRO", "ONGC", "POWERGRID",
    "TATASTEEL", "JSWSTEEL", "NESTLEIND", "DIVISLAB", "ADANIENT",
    "DRREDDY", "BAJAJFINSV", "TECHM", "INDUSINDBK", "HINDALCO",
    "GRASIM", "CIPLA", "APOLLOHOSP", "HEROMOTOCO", "BPCL",
    "EICHERMOT", "COALINDIA", "BRITANNIA", "TATACONSUM", "BAJAJ-AUTO",
    "SBILIFE", "ADANIPORTS",
]

# Nifty 50 stock symbols for batch backtesting (excludes index options)
_INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"}
BACKTEST_STOCK_SYMBOLS: List[str] = [
    sym for sym in AUTO_SCAN_SYMBOLS if sym not in _INDEX_SYMBOLS
]

# NIFTY 50 F&O stocks + key indices (for IV metrics screen)
NIFTY50_SYMBOLS: List[str] = [
    "NIFTY", "BANKNIFTY",
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "AXISBANK", "BAJFINANCE", "ASIANPAINT", "MARUTI",
    "HCLTECH", "SUNPHARMA", "TITAN", "NTPC", "TATAMOTORS",
    "ULTRACEMCO", "M&M", "WIPRO", "ONGC", "POWERGRID",
    "TATASTEEL", "JSWSTEEL", "NESTLEIND", "DIVISLAB", "ADANIENT",
    "DRREDDY", "BAJAJFINSV", "TECHM", "INDUSINDBK", "HINDALCO",
    "GRASIM", "CIPLA", "APOLLOHOSP", "HEROMOTOCO", "BPCL",
    "EICHERMOT", "COALINDIA", "BRITANNIA", "TATACONSUM", "BAJAJ-AUTO",
    "SBILIFE", "ADANIPORTS",
]

# NSE/BSE lot sizes (updated March 2026 — verify against latest NSE/BSE circular)
# Stock lot sizes per SEBI minimum contract value norms (₹15L for index, ₹5-7.5L for stocks)
LOT_SIZES: Dict[str, int] = {
    # ── Index Options ─────────────────────────────────────────────
    "NIFTY":      25,     # Changed from 75 → 25 (Nov 2024 SEBI circular)
    "BANKNIFTY":  30,
    "SENSEX":     10,     # BSE SENSEX options
    "MIDCPNIFTY": 50,     # NIFTY MID SELECT
    # ── NIFTY 50 Stocks ──────────────────────────────────────────
    "RELIANCE":    250,
    "TCS":         175,
    "HDFCBANK":    550,
    "INFY":        400,
    "ICICIBANK":   700,
    "HINDUNILVR":  300,
    "ITC":        1600,
    "SBIN":        750,
    "BHARTIARTL":  475,
    "KOTAKBANK":   400,
    "LT":          375,
    "AXISBANK":    625,
    "BAJFINANCE":  125,
    "ASIANPAINT":  300,
    "MARUTI":      100,
    "HCLTECH":     350,
    "SUNPHARMA":   350,
    "TITAN":       250,
    "NTPC":       2250,
    "TATAMOTORS": 1400,
    "ULTRACEMCO":  100,
    "M&M":         350,
    "WIPRO":      1500,
    "ONGC":       3850,
    "POWERGRID":  2700,
    "TATASTEEL":  5500,
    "JSWSTEEL":   1350,
    "NESTLEIND":    50,
    "DIVISLAB":    175,
    "ADANIENT":    500,
    "DRREDDY":     125,
    "BAJAJFINSV":  500,
    "TECHM":       700,
    "INDUSINDBK":  500,
    "HINDALCO":   1400,
    "GRASIM":      350,
    "CIPLA":       650,
    "APOLLOHOSP":  125,
    "HEROMOTOCO":  150,
    "BPCL":       1800,
    "EICHERMOT":   175,
    "COALINDIA":  2100,
    "BRITANNIA":   200,
    "TATACONSUM":  575,
    "BAJAJ-AUTO":  250,
    "SBILIFE":     500,
    "ADANIPORTS": 1250,
}

# Underlying instrument keys for Kite quote API
# For stocks: "NSE:SYMBOL" — Kite resolves to equity segment for spot price
# For indices: special names like "NSE:NIFTY 50"
_STOCK_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "AXISBANK", "BAJFINANCE", "ASIANPAINT", "MARUTI",
    "HCLTECH", "SUNPHARMA", "TITAN", "NTPC", "TATAMOTORS",
    "ULTRACEMCO", "M&M", "WIPRO", "ONGC", "POWERGRID",
    "TATASTEEL", "JSWSTEEL", "NESTLEIND", "DIVISLAB", "ADANIENT",
    "DRREDDY", "BAJAJFINSV", "TECHM", "INDUSINDBK", "HINDALCO",
    "GRASIM", "CIPLA", "APOLLOHOSP", "HEROMOTOCO", "BPCL",
    "EICHERMOT", "COALINDIA", "BRITANNIA", "TATACONSUM", "BAJAJ-AUTO",
    "SBILIFE", "ADANIPORTS",
]

UNDERLYING_INSTRUMENTS: Dict[str, str] = {
    # Index instruments
    "NIFTY":      "NSE:NIFTY 50",
    "BANKNIFTY":  "NSE:NIFTY BANK",
    "SENSEX":     "BSE:SENSEX",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    # Stock instruments — auto-generated from stock list
    **{sym: f"NSE:{sym}" for sym in _STOCK_SYMBOLS},
}

# Exchange segment per symbol (NFO = NSE F&O, BFO = BSE F&O)
# All stock F&O trades on NFO; only SENSEX on BFO
SYMBOL_EXCHANGE: Dict[str, str] = {
    "NIFTY":      "NFO",
    "BANKNIFTY":  "NFO",
    "MIDCPNIFTY": "NFO",
    "SENSEX":     "BFO",
    # All stock F&O on NFO — auto-generated
    **{sym: "NFO" for sym in _STOCK_SYMBOLS},
}

# Strike step sizes per symbol (used by scanner for strike grid)
# Index-specific steps + stock steps based on price bands
STRIKE_STEPS: Dict[str, int] = {
    # ── Index Options ─────────────────────────────────────────────
    "NIFTY":      50,
    "BANKNIFTY":  100,
    "SENSEX":     100,
    "MIDCPNIFTY": 25,
    # ── Stocks: price > ₹5000 → step 100 ────────────────────────
    "MARUTI":     100, "ULTRACEMCO": 100, "NESTLEIND": 250,
    "APOLLOHOSP": 100, "BAJAJ-AUTO": 100, "BAJFINANCE": 100, "DRREDDY": 100,
    # ── Stocks: price ₹2000-5000 → step 50 ──────────────────────
    "RELIANCE": 50, "TCS": 50, "TITAN": 50, "ASIANPAINT": 50,
    "LT": 50, "GRASIM": 50, "HEROMOTOCO": 50, "EICHERMOT": 50,
    "BRITANNIA": 50, "DIVISLAB": 50, "M&M": 50, "ADANIENT": 50,
    "BAJAJFINSV": 50, "HINDUNILVR": 50,
    # ── Stocks: price ₹1000-2000 → step 20 ──────────────────────
    "HDFCBANK": 20, "INFY": 25, "BHARTIARTL": 20, "KOTAKBANK": 25,
    "HCLTECH": 25, "SUNPHARMA": 25, "ICICIBANK": 20, "AXISBANK": 20,
    "TECHM": 20, "INDUSINDBK": 20, "CIPLA": 20, "SBILIFE": 20,
    "ADANIPORTS": 20,
    # ── Stocks: price ₹500-1000 → step 10 ────────────────────────
    "SBIN": 10, "TATACONSUM": 10,
    "TATAMOTORS": 10, "JSWSTEEL": 10,
    "WIPRO": 10, "BPCL": 10,
    "COALINDIA": 10, "HINDALCO": 10,
    # ── Stocks: price ₹100-500 → step 5 ──────────────────────────
    "ITC": 5, "NTPC": 5, "ONGC": 5, "POWERGRID": 5,
    # ── Stocks: price < ₹100 → step 2 ────────────────────────────
    "TATASTEEL": 2,
}

# Expiry cadence per symbol (SEBI Nov 2024: only 1 weekly benchmark per exchange)
# All stock options are monthly-only expiry
EXPIRY_CADENCE: Dict[str, str] = {
    "NIFTY":      "weekly",     # NSE weekly benchmark
    "BANKNIFTY":  "monthly",    # Weekly discontinued Nov 2024
    "SENSEX":     "weekly",     # BSE weekly benchmark
    "MIDCPNIFTY": "monthly",
    # All stock options → monthly only
    **{sym: "monthly" for sym in _STOCK_SYMBOLS},
}

# GICS Sector mapping for position concentration limits
SYMBOL_SECTOR: Dict[str, str] = {
    # ── Financials ────────────────────────────────────────────────
    "HDFCBANK": "Financials", "ICICIBANK": "Financials", "SBIN": "Financials",
    "KOTAKBANK": "Financials", "AXISBANK": "Financials", "BAJFINANCE": "Financials",
    "BAJAJFINSV": "Financials", "INDUSINDBK": "Financials", "SBILIFE": "Financials",
    # ── Information Technology ────────────────────────────────────
    "TCS": "InfoTech", "INFY": "InfoTech", "HCLTECH": "InfoTech",
    "WIPRO": "InfoTech", "TECHM": "InfoTech",
    # ── Energy ────────────────────────────────────────────────────
    "RELIANCE": "Energy", "ONGC": "Energy", "BPCL": "Energy",
    "COALINDIA": "Energy", "NTPC": "Energy", "POWERGRID": "Energy",
    "ADANIENT": "Energy",
    # ── Materials ─────────────────────────────────────────────────
    "TATASTEEL": "Materials", "JSWSTEEL": "Materials", "HINDALCO": "Materials",
    "ULTRACEMCO": "Materials", "GRASIM": "Materials",
    # ── Consumer Staples ──────────────────────────────────────────
    "HINDUNILVR": "ConsumerStaples", "ITC": "ConsumerStaples",
    "NESTLEIND": "ConsumerStaples", "BRITANNIA": "ConsumerStaples",
    "TATACONSUM": "ConsumerStaples",
    # ── Consumer Discretionary ────────────────────────────────────
    "MARUTI": "ConsumerDisc", "TATAMOTORS": "ConsumerDisc", "M&M": "ConsumerDisc",
    "BAJAJ-AUTO": "ConsumerDisc", "EICHERMOT": "ConsumerDisc", "TITAN": "ConsumerDisc",
    "ASIANPAINT": "ConsumerDisc",
    # ── Healthcare ────────────────────────────────────────────────
    "SUNPHARMA": "Healthcare", "DRREDDY": "Healthcare", "CIPLA": "Healthcare",
    "DIVISLAB": "Healthcare", "APOLLOHOSP": "Healthcare",
    # ── Industrials ───────────────────────────────────────────────
    "LT": "Industrials", "HEROMOTOCO": "Industrials", "ADANIPORTS": "Industrials",
    # ── Communication Services ────────────────────────────────────
    "BHARTIARTL": "CommServices",
}

# Order product type (NRML for overnight F&O — never MIS for VRP positions)
DEFAULT_FNO_PRODUCT: str = "NRML"

# NSE / BSE holidays for 2026 (update annually from exchange circular)
NSE_HOLIDAYS_2026: List[str] = [
    "2026-01-26",  # Republic Day
    "2026-03-10",  # Holi
    "2026-03-30",  # Id-Ul-Fitr (Ramzan Eid)
    "2026-04-02",  # Ram Navami
    "2026-04-03",  # Mahavir Jayanti
    "2026-04-14",  # Dr. Ambedkar Jayanti / Good Friday
    "2026-05-01",  # Maharashtra Day
    "2026-06-05",  # Eid ul-Adha (Bakri Id)
    "2026-08-15",  # Independence Day
    "2026-08-28",  # Ganesh Chaturthi
    "2026-10-02",  # Mahatma Gandhi Jayanti
    "2026-10-20",  # Diwali (Lakshmi Puja)
    "2026-10-21",  # Diwali Balipratipada
    "2026-11-04",  # Guru Nanak Jayanti
    "2026-12-25",  # Christmas
]
