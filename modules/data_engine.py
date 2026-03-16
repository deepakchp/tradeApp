"""
modules/data_engine.py — Market Data Layer
==========================================
Provides IVSurface (IV/RV metrics for a single symbol) and DataEngine
(market data cache and feed interface).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import numpy as np


@dataclass
class IVSurface:
    """IV/RV surface metrics for a single symbol used by the VRP gate and strategy selector."""
    symbol:         str
    spot:           float
    iv_30d:         float
    rv_30d:         float
    iv_rank:        float
    iv_percentile:  float
    put_skew_ratio:     float
    iv_rv_ratio:        float
    iv_rv_spread:       float
    # Strategy selection signals
    call_skew_ratio:    float = 1.0    # 16D Call IV / 50D IV; elevated → ratio call spread
    term_structure_rich: bool = False  # True when front/back vol spread favours calendar


class DataEngine:
    """
    Market data cache and feed interface.
    In production, this feeds real-time data from broker WebSocket streams.
    Currently operates as an in-memory cache for paper trading.
    """

    def __init__(self) -> None:
        self.cache: Dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.cache.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.cache[key] = value


class HistoricalVolEngine:
    """
    Computation engine for historical volatility metrics.
    Used by the scanner, backtester, and unit tests to compute
    IV percentile, IV rank, realized volatility, and put skew ratio.
    """

    def iv_percentile(self, current_iv: float, iv_series: np.ndarray) -> float:
        """
        IV Percentile: % of past observations below current IV.
        Range: 0-100. Higher values indicate relatively elevated IV.
        """
        if len(iv_series) == 0:
            return 50.0
        return float(np.sum(iv_series < current_iv) / len(iv_series) * 100)

    def iv_rank(self, current_iv: float, iv_series: np.ndarray) -> float:
        """
        IV Rank: (current - min) / (max - min) * 100.
        Range: 0-100. Normalizes current IV within its historical range.
        """
        if len(iv_series) == 0:
            return 50.0
        min_iv = float(np.min(iv_series))
        max_iv = float(np.max(iv_series))
        if max_iv == min_iv:
            return 50.0
        return float((current_iv - min_iv) / (max_iv - min_iv) * 100)

    def realized_vol(self, prices: np.ndarray, window: int = 30) -> float:
        """
        Annualized realized volatility from close-to-close log returns.
        Uses the trailing `window` prices. Returns value in % (e.g. 18.5 = 18.5%).
        """
        if len(prices) < window + 1:
            return 0.0
        log_returns = np.diff(np.log(prices[-(window + 1):]))
        return float(np.std(log_returns) * math.sqrt(252) * 100)

    def put_skew_ratio(self, put_iv_16d: float, atm_iv: float) -> float:
        """
        Put skew ratio: 16-delta put IV divided by ATM IV.
        Values > 1.3 indicate elevated put skew (fear premium).
        """
        if atm_iv <= 0:
            return 1.0
        return put_iv_16d / atm_iv
