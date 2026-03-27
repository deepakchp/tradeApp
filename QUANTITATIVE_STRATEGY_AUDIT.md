# Quantitative Strategy Audit: Missing P&L-Based Stop-Loss

**Date:** 2026-03-27
**Role:** Senior Quant / Risk Systems Architect
**Scope:** Rigorous analysis of the hedging engine's failure to protect against runaway losses

---

## Executive Summary

**CRITICAL GAP IDENTIFIED.** The hedging engine (`engine.py:719-869`) implements a 9-priority waterfall for position management but contains **zero P&L-based exit logic**. A short put position can accumulate unlimited losses (-600% of max profit observed on ADANIENT) while every adjustment check returns `NONE` because:

- Delta sits in the dead zone (0.20–0.30)
- IV expanded (vanna only fires on IV *collapse*)
- Strike is not breached by >1%
- DTE is far from gamma exit threshold

This is not a bug — it is a **structural blind spot** in the risk framework.

---

## Case Study: ADANIENT Short Put (-600% Loss)

| Metric | Value | Threshold | Result |
|--------|-------|-----------|--------|
| DTE | ~35 | ≤ 14 (gamma exit) | **SKIP** |
| Profit % | -600% | ≥ 50% (profit target) | **SKIP** |
| IV change | Expanded | ≥ 20% drop (vanna) | **SKIP** — wrong direction |
| Short put delta | 0.272 | > 0.30 (delta trigger) | **SKIP** — below threshold |
| Strike breach | < 1% | > 1% ITM (roll-out) | **SKIP** |
| VIX | ~13.5 | < 12.0 (low-vol) | **SKIP** |

**Every single check passes. The position bleeds indefinitely.**

The position entered a "delta dead zone" where the short put delta (0.272) hovers just below the 0.30 trigger with EWMA smoothing preventing spikes from crossing the threshold. Meanwhile the underlying drifted against the position gradually (not impulsively), so no volume spike triggered close-all logic.

---

## Hedging Engine Waterfall — Complete Walkthrough

```
Priority 0: Expiry-Day Exit (DTE ≤ 1)        → SKIP (DTE ~35)
Priority 1: Gamma Exit (DTE ≤ 14)             → SKIP (DTE ~35)
Priority 2: Profit Target (≥ 50%)             → SKIP (P&L is -600%)
Priority 3: Vanna Close (IV drop ≥ 20%)       → SKIP (IV expanded, not collapsed)
Priority 4: Cooldown Check                     → SKIP (no prior adjustment)
Priority 5: Low-Vol Regime (VIX < 12)          → SKIP (VIX ~13.5)
Priority 6: Delta Trigger (|Δ| > 0.30)        → SKIP (Δ = 0.272, EWMA smoothed)
Priority 7: Untested Side Roll                 → SKIP (challenged side not re-centered)
Priority 8: ITM Breach (>1% + θ neutralized)   → SKIP (strike not breached >1%)

Result: AdjustmentDecision.NONE — "All checks passed"
```

The system literally reports "All checks passed" while the position is at -600%.

---

## Five Hidden Failure Points

### 1. Delta Dead Zone (0.20 – 0.30)

The delta trigger fires at 0.30 with hysteresis reset at 0.20. A short put whose delta oscillates between 0.22–0.28 will **never** trigger an adjustment, regardless of how much money it loses. The EWMA smoothing (`cache.smoothed_delta()`) further dampens spikes that might briefly cross 0.30.

**Impact:** Positions can accumulate unlimited losses while delta stays in the dead zone during slow, grinding adverse moves.

### 2. One-Directional Vanna Check

`_check_vanna_adjustment()` (engine.py:704-716) only fires when IV **drops** ≥20% from entry:

```python
iv_drop = (position.entry_iv - current_iv) / position.entry_iv
return iv_drop >= LIFECYCLE_CFG.vanna_iv_drop_pct  # Only positive drops
```

When IV **expands** (the dangerous scenario for short options), vanna works *against* the position — inflating the option price and accelerating losses. But the check is blind to this direction entirely.

**Impact:** The most dangerous scenario for a short premium book (IV expansion) has zero detection logic.

### 3. `profit_pct` Asymmetry

`Position.profit_pct` (engine.py:149-150) is defined as:

```python
def profit_pct(self) -> float:
    return self.net_pnl / self.max_profit if self.max_profit != 0 else 0.0
```

For a short option receiving ₹5,000 premium, `max_profit = 5,000`. A -₹30,000 loss yields `profit_pct = -600%`. The system checks `profit_pct >= 0.50` for exits but **never checks the negative side**. The metric can go to -∞ with no floor check.

### 4. No Portfolio-Level Loss Awareness

The circuit breaker (`engine.py:872+`) monitors **monthly portfolio drawdown** at 4% of AUM, but individual position losses are never checked against any threshold. A single position could lose 2% of AUM (the `max_loss_per_name_pct` config value) without any mechanism detecting it.

`max_loss_per_name_pct = 0.02` exists in `RiskConfig` but is only used for **sizing at entry** — never for ongoing monitoring.

### 5. Expiry-Week Gamma Acceleration

The gamma exit at DTE ≤ 14 protects against the final exponential gamma ramp, but the zone between DTE 14–21 is unmonitored. For weekly options (NIFTY/BANKNIFTY), gamma accelerates significantly from DTE 7, and SEBI's 2024 margin surcharge makes holding through expiry week extremely capital-inefficient.

---

## Proposed Fix: Multi-Tier P&L Guard

### Configuration (to add in `config.py`)

```python
@dataclass
class StopLossConfig:
    # Tier 1: Soft warning — log + alert, no action
    soft_warning_pct: float = -1.50       # -150% of max profit

    # Tier 2: Hard exit — close position immediately
    hard_exit_pct: float = -3.00          # -300% of max profit

    # Tier 3: IV expansion guard
    iv_expansion_trigger_pct: float = 0.25   # IV expanded 25%+ from entry
    iv_expansion_spot_move_pct: float = 0.02 # AND spot moved 2%+ against

STOPLOSS_CFG = StopLossConfig()
```

### Implementation (to insert at Priority 2.5 in the waterfall)

Insert between Profit Target (Priority 2) and Vanna (Priority 3):

```python
# ── 2a. Max Loss Exit (hard stop) ──────────────────────────
if position.profit_pct <= STOPLOSS_CFG.hard_exit_pct:
    return HedgingDecision(
        action=AdjustmentDecision.STOP_LOSS_EXIT,
        reason=(
            f"P&L {position.profit_pct*100:.1f}% breached hard stop "
            f"({STOPLOSS_CFG.hard_exit_pct*100:.0f}% of max profit)"
        ),
        position_id=pid,
    )

# ── 2b. Max Loss Warning (soft alert) ─────────────────────
if position.profit_pct <= STOPLOSS_CFG.soft_warning_pct:
    log.warning(
        "hedging.loss_warning",
        position_id=pid,
        profit_pct=round(position.profit_pct * 100, 1),
        threshold=round(STOPLOSS_CFG.soft_warning_pct * 100, 1),
    )
    # Don't return — continue evaluation, but alert is raised

# ── 2c. IV Expansion Guard ────────────────────────────────
iv_expansion = (current_iv - position.entry_iv) / position.entry_iv
spot_move = abs(current_spot - position.entry_spot) / position.entry_spot
if (iv_expansion >= STOPLOSS_CFG.iv_expansion_trigger_pct
        and spot_move >= STOPLOSS_CFG.iv_expansion_spot_move_pct):
    return HedgingDecision(
        action=AdjustmentDecision.IV_EXPANSION_EXIT,
        reason=(
            f"IV expanded {iv_expansion*100:.1f}% from entry with "
            f"{spot_move*100:.1f}% adverse spot move. "
            f"Vanna + delta working against position."
        ),
        position_id=pid,
    )
```

### Pros vs. Cons

| Aspect | Benefit | Risk |
|--------|---------|------|
| Hard stop at -300% | Caps max loss per position | May exit before mean reversion on wide spreads |
| Soft warning at -150% | Early alert without forced action | Requires monitoring infrastructure (alerts) |
| IV expansion guard | Catches the blind spot vanna misses | May trigger during VIX spikes that reverse quickly |
| AUM-based backstop | Prevents single name from exceeding risk budget | Needs AUM tracking to be accurate |

### Impact Analysis

With the ADANIENT case:
- **Soft warning** would have fired at -150% (~₹7,500 loss on ₹5,000 credit)
- **Hard exit** would have fired at -300% (~₹15,000 loss), saving ₹15,000+ of additional drawdown
- **IV expansion guard** would have likely fired even earlier, as IV expanded significantly with adverse spot movement

### New `AdjustmentDecision` Enum Values Required

```python
class AdjustmentDecision(Enum):
    # ... existing values ...
    STOP_LOSS_EXIT = "stop_loss_exit"
    IV_EXPANSION_EXIT = "iv_expansion_exit"
```

---

## Backtester Gap

The backtester (`modules/backtester.py`) must also implement these P&L checks. Currently it mirrors the live engine's waterfall — meaning backtested results also **understate realized losses** because positions that should have been stopped out were allowed to run.

Any backtest results generated before this fix should be considered **optimistic** on the loss side.

---

## Recommendation

**Implement immediately.** The current system has an unbounded loss tail on individual positions. The -300% hard stop is conservative (most institutional desks use -200% to -250% on premium-selling strategies), and the IV expansion guard addresses the specific blind spot that the one-directional vanna check creates.

Priority order:
1. Add `STOP_LOSS_EXIT` and `IV_EXPANSION_EXIT` to `AdjustmentDecision` enum
2. Add `StopLossConfig` to `config.py`
3. Insert P&L guard checks into `evaluate_position()` waterfall
4. Update backtester to apply same logic
5. Add execution handler for new decision types in `app.py`
