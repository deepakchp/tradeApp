# VRP Framework v2 — Engine Flow Diagrams

## 1. Full Trade Pipeline (Signal → Execution)

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                    VRP FRAMEWORK v2 — TRADE PIPELINE                       ║
╚══════════════════════════════════════════════════════════════════════════════╝

  AUTO_SCAN_SYMBOLS (4 indices + 103 stocks)
          │
          ▼
  ┌───────────────────┐   every 5 min (SCANNER_CFG.scan_interval_sec)
  │  Background Scan  │   runs in daemon thread during market hours
  │    _scan_loop()   │   09:15 IST — 15:30 IST
  └────────┬──────────┘
           │
           ▼
  ┌────────────────────┐   max 25 concurrent (RISK_CFG.max_concurrent_positions)
  │  Position Limit?   ├── YES ──▶ SKIP (no more room)
  └────────┬───────────┘
           │ NO
           ▼
  ┌────────────────────┐
  │  Fetch Spot Price  │   broker.get_ltp("NSE:SYMBOL")
  └────────┬───────────┘
           │
           ▼
  ┌────────────────────┐   target ~45 DTE, range 35-55 DTE
  │  Get Target Expiry │   monthly-only for BANKNIFTY, MIDCPNIFTY
  └────────┬───────────┘
           │
           ▼
  ┌────────────────────┐
  │  Load Option Chain │   broker.find_options(symbol, expiry)
  └────────┬───────────┘
           │
           ▼
╔══════════════════════════════════════════════════════════════╗
║              PHASE 1 — VRP GATE (All must pass)            ║
║                                                            ║
║   ┌──────────────────────────────────────────────────────┐ ║
║   │         INDEX (Tier 1)      │    STOCK (Tier 2)      │ ║
║   ├─────────────────────────────┼────────────────────────┤ ║
║   │  IV-RV Spread  > 2.5 pts   │  IV-RV Spread > 4.5 pts│ ║
║   │  IV Percentile > 60th      │  IV Percentile > 65th  │ ║
║   │  IV/RV Ratio   > 1.20      │  IV/RV Ratio   > 1.30  │ ║
║   └──────────────────────────────────────────────────────┘ ║
║                                                            ║
║   Circuit Breaker Active? → Spread tightened to 3.5 pts   ║
╚════════════════════╤═════════════════════════════════════════╝
                     │
              ┌──────┴──────┐
              │  Gate Pass? │
              └──┬──────┬───┘
            NO   │      │ YES
                 ▼      ▼
          REJECT    ┌─────────────────────┐
                    │  Fetch India VIX    │
                    └─────────┬───────────┘
                              │
                              ▼
╔══════════════════════════════════════════════════════════════╗
║           PHASE 2 — STRATEGY SELECTION                     ║
║           (top-to-bottom, first match wins)                ║
║                                                            ║
║   VIX < 12?  ─────────────▶  STRANGLE @ 12-delta          ║
║       │ NO                    (low-vol protocol)           ║
║       ▼                                                    ║
║   Put Skew > 1.3                                           ║
║   + Term Rich? ───────────▶  PUT_CALENDAR @ 30-delta       ║
║       │ NO                                                 ║
║       ▼                                                    ║
║   Put Skew > 1.3? ────────▶  PUT_SPREAD @ 30-delta         ║
║       │ NO                                                 ║
║       ▼                                                    ║
║   Call Skew > 1.2? ───────▶  RATIO_CALL_SPREAD             ║
║       │ NO                    (16D short / 10D wing)       ║
║       ▼                                                    ║
║   Default ─────────────────▶  IRON_CONDOR                  ║
║                               (16D short / 10D wing)      ║
╚════════════════════╤═════════════════════════════════════════╝
                     │
                     ▼
╔══════════════════════════════════════════════════════════════╗
║           PHASE 3 — STRIKE FINDING                         ║
║                                                            ║
║   For each leg:                                            ║
║     1. Get OTM strikes above/below spot                    ║
║     2. Compute BS delta for each strike                    ║
║     3. Select strike closest to target delta               ║
║                                                            ║
║   Iron Condor:                                             ║
║     Short Put  @ 16Δ  ─┐                                  ║
║     Short Call @ 16Δ  ─┤  4 legs                           ║
║     Long Put   @ 10Δ  ─┤  (wings for protection)          ║
║     Long Call  @ 10Δ  ─┘                                   ║
║                                                            ║
║   Wings not found? → Downgrade to STRANGLE (2 legs)        ║
╚════════════════════╤═════════════════════════════════════════╝
                     │
                     ▼
╔══════════════════════════════════════════════════════════════╗
║           PHASE 4 — SIZING                                 ║
║                                                            ║
║   risk_budget = AUM × 2%    (max loss per name)            ║
║   lots = budget / max_loss_per_lot                         ║
║                                                            ║
║   Guards:                                                  ║
║     • Margin cap: AUM × 80% / margin_per_lot              ║
║     • Hard cap: 500 lots maximum                           ║
║     • Floor: minimum 1 lot                                 ║
║     • Premium check: net premium < 1% of AUM              ║
╚════════════════════╤═════════════════════════════════════════╝
                     │
                     ▼
╔══════════════════════════════════════════════════════════════╗
║           PORTFOLIO RISK CHECKS (Pre-Execution)            ║
║                                                            ║
║   ✓ Beta-weighted delta  ≤ ±10 per Cr AUM                 ║
║   ✓ Vega Layer 1: -0.25% AUM per 1 VIX-point rise         ║
║   ✓ Vega Layer 2: -8% AUM if VIX doubles                  ║
║   ✓ Sector limit: ≤ 5 per GICS sector                     ║
║   ✓ Correlation: avg pairwise > 0.65 → 30% notional cut   ║
╚════════════════════╤═════════════════════════════════════════╝
                     │
                     ▼
  ┌────────────────────────────┐
  │  PENDING SIGNAL CREATED    │   TTL = 30 minutes
  │  (awaits user execution)   │   max 5 pending signals
  └────────────┬───────────────┘
               │ User clicks "Execute"
               ▼
╔══════════════════════════════════════════════════════════════╗
║           PHASE 5 — EXECUTION (SlippageController)         ║
║                                                            ║
║   For each leg:                                            ║
║     1. Start at theoretical mid price                      ║
║     2. Chase: walk +0.25% per step, every 3 seconds        ║
║     3. Max 4 steps = 1% total slippage budget              ║
║     4. Abandon if slippage > 1%                            ║
║     5. Reject if credit < 2× transaction costs             ║
╚══════════════════════════════════════════════════════════════╝
```

---

## 2. Dynamic Hedging Engine — Position Adjustment Flow

```
╔══════════════════════════════════════════════════════════════════════════════╗
║               DYNAMIC HEDGING ENGINE — ADJUSTMENT FLOW                     ║
║               Monitors every 10s (delta) / 30s (MTM)                       ║
╚══════════════════════════════════════════════════════════════════════════════╝

  ACTIVE POSITION (short premium)
          │
          │  evaluate_position() — strict priority order
          │  first matching rule wins
          ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │  PRIORITY 0: EXPIRY EXIT                                          │
  │  DTE ≤ 1?  ──── YES ──▶  GAMMA_EXIT (SEBI 2% surcharge applies)  │
  └──────────┬──────────────────────────────────────────────────────────┘
             │ NO
             ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │  PRIORITY 1: GAMMA EXIT                                           │
  │  DTE ≤ 14?  ──── YES ──▶  GAMMA_EXIT (close all legs)            │
  └──────────┬──────────────────────────────────────────────────────────┘
             │ NO
             ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │  PRIORITY 2: PROFIT TARGET                                        │
  │  P&L ≥ 40% of max credit?  ──── YES ──▶  PROFIT_EXIT             │
  │                                           (close, recycle capital) │
  └──────────┬──────────────────────────────────────────────────────────┘
             │ NO
             ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │  PRIORITY 3: VANNA CLOSE (IV Crush)                               │
  │  IV dropped ≥ 20% from entry                                      │
  │  AND spot within 0.5% of entry?                                   │
  │  ──── YES ──▶  VANNA_CLOSE (IV collapsed, take profit early)      │
  └──────────┬──────────────────────────────────────────────────────────┘
             │ NO
             ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │  PRIORITY 4: COOLDOWN CHECK                                       │
  │  Adjustment made within last 15 min?                              │
  │  ──── YES ──▶  NONE (skip all further checks, prevent whipsaw)    │
  └──────────┬──────────────────────────────────────────────────────────┘
             │ NO (no recent adjustment)
             ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │  PRIORITY 5: LOW-VOL REGIME                                       │
  │  India VIX < 12?                                                  │
  │  ──── YES ──▶  LOW_VOL_ADJUST                                     │
  │                  • Reduce notional by 40%                          │
  │                  • Widen short strikes to 12-delta                 │
  │                  • Allocate 7.5% budget to tail hedges             │
  └──────────┬──────────────────────────────────────────────────────────┘
             │ NO
             ▼
╔══════════════════════════════════════════════════════════════════════════╗
║  PRIORITY 6: DELTA TRIGGER — THE CORE ADJUSTMENT                      ║
║                                                                        ║
║  Any short leg EWMA-smoothed delta > 0.30?                             ║
║                                                                        ║
║  ┌─── YES ──────────────────────────────────────────────────┐          ║
║  │                                                          │          ║
║  │  Is it an IMPULSIVE move?                                │          ║
║  │  (price > 1.5% in 5 min AND volume > 1.5× average)      │          ║
║  │                                                          │          ║
║  │      YES                           NO                    │          ║
║  │       │                             │                    │          ║
║  │       ▼                             ▼                    │          ║
║  │  ┌──────────────┐    ┌─────────────────────────┐         │          ║
║  │  │  CLOSE_ALL   │    │  ROLL_CHALLENGED        │         │          ║
║  │  │  (emergency  │    │  Roll threatened leg    │         │          ║
║  │  │   full exit) │    │  to 16Δ in next expiry  │         │          ║
║  │  └──────────────┘    └─────────────────────────┘         │          ║
║  │                                                          │          ║
║  └──────────────────────────────────────────────────────────┘          ║
║                                                                        ║
║  Hysteresis: fires at 0.30Δ, resets only at 0.20Δ                      ║
║  EWMA smoothing: 5-tick span prevents noise                            ║
╚═════════════════════════╤══════════════════════════════════════════════╝
                          │ NO (delta OK)
                          ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │  PRIORITY 7: UNTESTED SIDE ROLL                                   │
  │  Challenged side already re-centered?                             │
  │  AND IV within 10% of entry IV?                                   │
  │  ──── YES ──▶  ROLL_UNTESTED (bring opposite side to 20Δ)        │
  │                (tighten profit zone after danger passes)           │
  └──────────┬──────────────────────────────────────────────────────────┘
             │ NO
             ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │  PRIORITY 8: ITM BREACH ROLL-OUT                                  │
  │  Strike breached by > 1%                                          │
  │  AND theta < 20% of entry theta?                                  │
  │  ──── YES ──▶  ROLL_OUT (same strike, next expiry)                │
  │                (theta exhausted, buy more time)                    │
  └──────────┬──────────────────────────────────────────────────────────┘
             │ NO
             ▼
  ┌────────────────────┐
  │  NONE              │   Position is healthy, no action needed
  │  (hold, collect θ) │
  └────────────────────┘
```

---

## 3. Delta Trigger Deep Dive — What Happens When a Trade Goes Against You

```
╔══════════════════════════════════════════════════════════════════════════════╗
║           TRADE GOES AGAINST YOU — DEFENSE LAYERS                          ║
╚══════════════════════════════════════════════════════════════════════════════╝

  Example: Short Iron Condor on RELIANCE
  ──────────────────────────────────────
  Entry: Spot = 2800, Short Put @ 2650 (16Δ), Short Call @ 2950 (16Δ)
         Long Put  @ 2550 (10Δ), Long Call @ 3050 (10Δ)

  RELIANCE starts falling...

  TIME ──────────────────────────────────────────────────────────────▶

  Spot: 2800    2750    2700    2680    2650    2620    2600
         │       │       │       │       │       │       │
         │       │       │       │       │       │       │
  ───────┼───────┼───────┼───────┼───────┼───────┼───────┼──────────
  PUT    │ 0.16  │ 0.20  │ 0.25  │ 0.28  │ 0.30+ │ 0.35  │ 0.45
  DELTA  │       │       │       │       │  ▲    │       │
         │       │       │       │       │  │    │       │
         │       │       │       │       │ TRIGGER       │
         │       │       │       │       │ FIRES │       │
  ───────┴───────┴───────┴───────┴───────┴───────┴───────┴──────────

  LAYER 1: EWMA Smoothing (5-tick span)
  ┌──────────────────────────────────────────────────────────────┐
  │  Raw deltas: 0.28, 0.31, 0.29, 0.32, 0.30                  │
  │  EWMA delta: 0.2984 → NOT triggered (noise filtered out)    │
  │                                                              │
  │  Raw deltas: 0.30, 0.33, 0.31, 0.34, 0.32                  │
  │  EWMA delta: 0.3180 → TRIGGERED (confirmed breach)          │
  └──────────────────────────────────────────────────────────────┘

  LAYER 2: Impulsive vs. Gradual Classification
  ┌──────────────────────────────────────────────────────────────┐
  │                                                              │
  │  IMPULSIVE (panic/event-driven)          GRADUAL (drift)     │
  │  ─────────────────────────          ─────────────────────     │
  │  Price moved > 1.5% in 5 min       Price drifted slowly     │
  │  Volume > 1.5× average              Normal volume            │
  │           │                                  │               │
  │           ▼                                  ▼               │
  │    ┌──────────────┐              ┌───────────────────┐       │
  │    │  CLOSE ALL   │              │  ROLL CHALLENGED  │       │
  │    │  Exit entire │              │  Move short put   │       │
  │    │  position    │              │  to 16Δ in next   │       │
  │    │  immediately │              │  expiry cycle     │       │
  │    └──────────────┘              └───────────────────┘       │
  │                                                              │
  └──────────────────────────────────────────────────────────────┘

  LAYER 3: Post-Roll Sequence (if gradual drift)
  ┌──────────────────────────────────────────────────────────────┐
  │                                                              │
  │  Step 1: Roll challenged put leg                             │
  │  ─────────────────────────────                               │
  │  Before: Short 2650 Put (now 30Δ, dangerous)                 │
  │  After:  Short 2550 Put in next expiry (back to 16Δ)         │
  │                                                              │
  │            ┌─── 15-min cooldown ───┐                         │
  │            │   (prevent whipsaw)   │                         │
  │            └───────────┬───────────┘                         │
  │                        ▼                                     │
  │  Step 2: Consider rolling UNTESTED side                      │
  │  ──────────────────────────────────────                      │
  │  Two conditions BOTH must be true:                           │
  │   ✓ Challenged side already re-centered (16Δ)               │
  │   ✓ IV recovered to within 10% of entry IV                  │
  │                                                              │
  │  IF both true:                                               │
  │    Roll untested call from 2950 → closer to spot (20Δ)       │
  │    (tighten profit zone on the safe side)                    │
  │                                                              │
  │  IF not:                                                     │
  │    Leave untested side alone (don't narrow during stress)    │
  │                                                              │
  └──────────────────────────────────────────────────────────────┘

  LAYER 4: ITM Breach (last resort before full exit)
  ┌──────────────────────────────────────────────────────────────┐
  │                                                              │
  │  If spot drops BELOW the short strike by > 1%:              │
  │    Spot = 2620, Short Put = 2650 → breach = 1.1%            │
  │                                                              │
  │  AND theta income is exhausted:                              │
  │    Current theta < 20% of entry theta                        │
  │                                                              │
  │  Action: ROLL_OUT to next expiry (same strike, more time)   │
  │    → Buy back 2650 Put (current expiry)                      │
  │    → Sell 2650 Put (next expiry) for fresh theta             │
  │                                                              │
  └──────────────────────────────────────────────────────────────┘
```

---

## 4. Circuit Breaker — Portfolio-Level Safety Net

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                    CIRCUIT BREAKER SYSTEM                                   ║
╚══════════════════════════════════════════════════════════════════════════════╝

  Monthly P&L Tracking (resets each calendar month)
  ─────────────────────────────────────────────────

       AUM = ₹1 Crore

       ┌────┬────┬────┬────┬────┬────┬────┬────┬────┬────┐
  P&L  │    │    │    │    │    │    │    │    │    │    │
  (%): │    │    │    │    │    │    │    │    │    │    │
       │    │    │    │    │    │    │    │    │    │    │
   0%  ├────┤    │    │    │    │    │    │    │    │    │
       │    ├────┤    │    │    │    │    │    │    │    │
  -1%  │    │    │    │    │    │    │    │    │    │    │
       │    │    ├────┤    │    │    │    │    │    │    │
  -2%  │    │    │    │    │    │    │    │    │    │    │
       │    │    │    ├────┤    │    │    │    │    │    │
  -3%  │    │    │    │    │    │    │    │    │    │    │
       │    │    │    │    │    │    │    │    │    │    │
  ─4%──│────│────│────│────│────│────│────│────│────│──── │ ◀── TRIGGER
       │    │    │    │    │ ▲  │    │    │    │    │    │
       └────┴────┴────┴────┴─│──┴────┴────┴────┴────┴────┘
       Day1 Day2 Day3 Day4 Day5 ...
                             │
                    ╔════════╧════════════════════════════════╗
                    ║     CIRCUIT BREAKER ACTIVATED           ║
                    ║                                         ║
                    ║  Effects:                               ║
                    ║  ┌────────────────────────────────────┐ ║
                    ║  │ Max positions: 25 → 12 (-50%)     │ ║
                    ║  │ VRP spread gate: 2.5 → 3.5 pts    │ ║
                    ║  │ (harder to enter new trades)       │ ║
                    ║  └────────────────────────────────────┘ ║
                    ║                                         ║
                    ║  Resets: Start of next calendar month   ║
                    ╚═════════════════════════════════════════╝
```

---

## 5. Complete Position Lifecycle

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                    POSITION LIFECYCLE                                       ║
╚══════════════════════════════════════════════════════════════════════════════╝

    PENDING ──▶ ACTIVE ──▶ ADJUSTING ──▶ ACTIVE ──▶ CLOSING ──▶ CLOSED
       │           │           │            │           │
       │           │           │            │           │
   Signal      Executed    Roll/Hedge    Back to     Exit
   created     via Kite    triggered     monitoring  triggered
   (TTL 30m)

    ┌─────────┐
    │ ENTRY   │  VRP Gate passed, strategy selected, sized
    │ (35-55  │  Slippage-controlled limit orders
    │  DTE)   │
    └────┬────┘
         │
         ▼
    ┌─────────┐     ┌──────────────────────────────────────────────┐
    │ MONITOR │◀────│  Every 10s: check deltas (EWMA smoothed)    │
    │ (hold,  │     │  Every 30s: check MTM P&L                   │
    │  theta  │     │  Priority cascade: expiry → profit → vanna  │
    │  decay) │     │    → cooldown → low-vol → delta → untested  │
    └────┬────┘     │    → ITM breach                             │
         │          └──────────────────────────────────────────────┘
         │
    Five possible exits:
    ┌────┼────────────────────────────────────────────┐
    │    │                                            │
    │    ├──▶ GAMMA_EXIT    (DTE ≤ 14, mechanical)    │
    │    ├──▶ PROFIT_EXIT   (P&L ≥ 40% of credit)    │
    │    ├──▶ VANNA_CLOSE   (IV crush, spot stable)   │
    │    ├──▶ CLOSE_ALL     (impulsive delta breach)  │
    │    └──▶ ROLL_OUT      (ITM, theta exhausted)    │
    │         → may re-enter MONITOR in new expiry    │
    └─────────────────────────────────────────────────┘
```

---

## Key Thresholds Reference

| Parameter | Value | Source |
|-----------|-------|--------|
| VRP Spread (Index) | > 2.5 pts | `VRP_GATE` |
| VRP Spread (Stock) | > 4.5 pts | `VRP_GATE` |
| IV Percentile (Index) | > 60th | `VRP_GATE` |
| IV Percentile (Stock) | > 65th | `VRP_GATE` |
| Delta Trigger | 0.30 (fire) / 0.20 (reset) | `GREEKS_CFG` |
| EWMA Span | 5 ticks | `GREEKS_CFG` |
| Profit Target | 40% of credit | `GREEKS_CFG` |
| Gamma Exit | DTE ≤ 14 | `GREEKS_CFG` |
| Cooldown | 15 minutes | `WHIPSAW_CFG` |
| Impulsive Move | >1.5% in 5 min + 1.5× volume | `WHIPSAW_CFG` |
| Circuit Breaker | -4% monthly drawdown | `RISK_CFG` |
| Max Positions | 25 (normal) / 12 (breaker) | `RISK_CFG` |
| Risk Budget | 2% AUM per name | `RISK_CFG` |
| Max Lots | 500 hard cap | `SizingEngine` |
| Vega Limit L1 | -0.25% AUM per VIX point | `RISK_CFG` |
| Vega Limit L2 | -8% AUM if VIX doubles | `RISK_CFG` |
| Sector Limit | ≤ 5 per GICS sector | `RISK_CFG` |
| Correlation Cap | > 0.65 → 30% notional cut | `RISK_CFG` |
| Slippage Budget | 1% max (4 steps × 0.25%) | `SlippageController` |
| Target DTE | 35-55 days (~45 DTE) | `SCANNER_CFG` |

---

## 6. End-to-End Example — RELIANCE Iron Condor (Full Lifecycle)

This walkthrough traces a single trade from scan to close, with real numbers.

### Setup

```
  AUM          = ₹1,00,00,000 (₹1 Crore)
  Symbol       = RELIANCE
  Lot Size     = 250 shares
  Spot Price   = ₹2,800
  India VIX    = 15.2
  Date         = March 12, 2026 (Wednesday)
  Target DTE   = 45 days → April 26 expiry (44 DTE)
```

---

### STEP 1 — Scanner Picks Up RELIANCE (09:25 IST)

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │  AutoScanner._scan_loop() fires (every 5 min)                     │
  │                                                                     │
  │  Fetching IV surface for RELIANCE...                               │
  │                                                                     │
  │  IVSurface:                                                         │
  │    ATM IV      = 28.5%                                              │
  │    30-day RV   = 21.0%                                              │
  │    IV Pctl     = 72nd (252-day lookback)                            │
  │    Put Skew    = 1.08 (mild, not elevated)                          │
  │    Call Skew   = 0.95 (normal)                                      │
  │    Term Struct = flat                                                │
  └─────────────────────────────────────────────────────────────────────┘
```

---

### STEP 2 — VRP Gate Validation

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │  VRPGateValidator.validate(iv_surface)                             │
  │                                                                     │
  │  RELIANCE is a STOCK → Tier 2 thresholds apply                     │
  │                                                                     │
  │  Check 1: IV-RV Spread                                             │
  │    28.5 - 21.0 = 7.5 pts  >  4.5 pts threshold    ✅ PASS         │
  │                                                                     │
  │  Check 2: IV Percentile                                             │
  │    72nd percentile  >  65th threshold               ✅ PASS         │
  │                                                                     │
  │  Check 3: IV/RV Ratio                                               │
  │    28.5 / 21.0 = 1.357  >  1.30 threshold          ✅ PASS         │
  │                                                                     │
  │  Circuit Breaker active? NO (month started clean)                   │
  │                                                                     │
  │  ══════════════════════════════════════════════                      │
  │  RESULT: VRP GATE PASSED — IV is rich, proceed                     │
  │  ══════════════════════════════════════════════                      │
  └─────────────────────────────────────────────────────────────────────┘
```

---

### STEP 3 — Strategy Selection

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │  StrategySelector.select(iv_surface, india_vix=15.2)               │
  │                                                                     │
  │  Decision tree (top-to-bottom):                                     │
  │                                                                     │
  │  VIX < 12?              15.2 < 12?          ❌ NO                  │
  │  Put Skew > 1.3?        1.08 > 1.3?         ❌ NO                  │
  │  Call Skew > 1.2?       0.95 > 1.2?         ❌ NO                  │
  │  Default fallthrough →  IRON CONDOR                                 │
  │                                                                     │
  │  ══════════════════════════════════════════════                      │
  │  RESULT: IRON_CONDOR                                                │
  │    Short legs @ 16-delta                                            │
  │    Long wings @ 10-delta                                            │
  │  ══════════════════════════════════════════════                      │
  └─────────────────────────────────────────────────────────────────────┘
```

---

### STEP 4 — Strike Finding

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │  _find_strikes(RELIANCE, spot=2800, expiry=Apr26, IV=28.5%)        │
  │                                                                     │
  │  Using Black-Scholes to compute delta at each OTM strike:          │
  │                                                                     │
  │  PUT SIDE (below spot):                                             │
  │    Strike  BS Delta   Target                                        │
  │    2700    -0.22      │                                             │
  │    2680    -0.19      │                                             │
  │    2660    -0.17      │                                             │
  │    2640    -0.15      ← closest to 16Δ → SHORT PUT @ 2660          │
  │    2600    -0.11      │                                             │
  │    2580    -0.09      ← closest to 10Δ → LONG PUT  @ 2580          │
  │                                                                     │
  │  CALL SIDE (above spot):                                            │
  │    Strike  BS Delta   Target                                        │
  │    2900    +0.22      │                                             │
  │    2920    +0.19      │                                             │
  │    2940    +0.17      │                                             │
  │    2960    +0.15      ← closest to 16Δ → SHORT CALL @ 2940         │
  │    3000    +0.11      │                                             │
  │    3020    +0.09      ← closest to 10Δ → LONG CALL  @ 3020         │
  │                                                                     │
  │  ══════════════════════════════════════════════════════════          │
  │  RESULT: 4-leg Iron Condor                                          │
  │                                                                     │
  │  Leg 1: SELL 2660 PE  @ ₹18.50  (16Δ)                              │
  │  Leg 2: SELL 2940 CE  @ ₹16.80  (16Δ)                              │
  │  Leg 3: BUY  2580 PE  @ ₹ 9.20  (10Δ)  [wing, 80-pt wide]        │
  │  Leg 4: BUY  3020 CE  @ ₹ 7.40  (10Δ)  [wing, 80-pt wide]        │
  │                                                                     │
  │  Net Credit = (18.50 + 16.80) - (9.20 + 7.40)                      │
  │             = 35.30 - 16.60 = ₹18.70 per share                     │
  │  Max Loss   = wing_width - credit = 80 - 18.70 = ₹61.30 / share   │
  │  ══════════════════════════════════════════════════════════          │
  └─────────────────────────────────────────────────────────────────────┘
```

---

### STEP 5 — Position Sizing

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │  SizingEngine.compute_lots()                                       │
  │                                                                     │
  │  Inputs:                                                            │
  │    AUM            = ₹1,00,00,000                                    │
  │    max_loss/share = ₹61.30                                          │
  │    lot_size       = 250 shares                                      │
  │    max_loss/lot   = 61.30 × 250 = ₹15,325                          │
  │                                                                     │
  │  Step 1: Risk budget                                                │
  │    budget = AUM × 2% = ₹1,00,00,000 × 0.02 = ₹2,00,000            │
  │                                                                     │
  │  Step 2: Loss-based lots                                            │
  │    lots = ₹2,00,000 / ₹15,325 = 13.05 → 13 lots                   │
  │                                                                     │
  │  Step 3: Margin constraint                                          │
  │    margin/lot ≈ ₹1,20,000 (from Kite)                              │
  │    margin cap = (AUM × 80%) / margin = ₹80,00,000 / ₹1,20,000     │
  │              = 66 lots → NOT binding                                │
  │                                                                     │
  │  Step 4: Hard cap check                                             │
  │    13 < 500 → NOT binding                                           │
  │                                                                     │
  │  Step 5: Premium budget check                                       │
  │    net premium = 18.70 × 250 × 13 = ₹60,775                        │
  │    limit = AUM × 1% = ₹1,00,000                                    │
  │    ₹60,775 < ₹1,00,000  ✅ PASS                                    │
  │                                                                     │
  │  ══════════════════════════════════════════════                      │
  │  RESULT: 13 LOTS                                                    │
  │                                                                     │
  │  Total credit  = ₹18.70 × 250 × 13 = ₹60,775                      │
  │  Max loss      = ₹61.30 × 250 × 13 = ₹1,99,225                    │
  │  Margin reqd   = ₹1,20,000 × 13     = ₹15,60,000                   │
  │  ══════════════════════════════════════════════                      │
  └─────────────────────────────────────────────────────────────────────┘
```

---

### STEP 6 — Portfolio Risk Checks

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │  PortfolioGreeksEngine (pre-execution gate)                        │
  │                                                                     │
  │  Existing portfolio: 8 active positions                             │
  │                                                                     │
  │  ✓ Position count:  8 + 1 = 9  ≤ 25 max                ✅ PASS     │
  │  ✓ Sector (Energy): 1 existing + 1 = 2  ≤ 5 max        ✅ PASS     │
  │  ✓ Beta-weighted Δ: +3.2 NIFTY units  ≤ ±10/Cr         ✅ PASS     │
  │  ✓ Vega L1: ₹18,500 loss/VIX-pt  ≤ ₹25,000 (0.25%)    ✅ PASS     │
  │  ✓ Vega L2: ₹3,40,000 VIX-double  ≤ ₹8,00,000 (8%)    ✅ PASS     │
  │  ✓ Correlation: avg pairwise = 0.48  ≤ 0.65            ✅ PASS     │
  │                                                                     │
  │  ══════════════════════════════════════════════                      │
  │  ALL CHECKS PASSED → Signal created as PENDING                     │
  │  Signal TTL = 30 minutes, awaiting user execution                   │
  │  ══════════════════════════════════════════════                      │
  └─────────────────────────────────────────────────────────────────────┘
```

---

### STEP 7 — User Executes (09:28 IST)

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │  User clicks "Execute" on Dashboard → Confirmation popup shown     │
  │                                                                     │
  │  ┌───────────────────────────────────────────────────────────┐     │
  │  │  RELIANCE Iron Condor                                     │     │
  │  │  ──────────────────────                                   │     │
  │  │  SELL 13× 2660 PE  @ ₹18.50     ₹60,125 cr               │     │
  │  │  SELL 13× 2940 CE  @ ₹16.80     ₹54,600 cr               │     │
  │  │  BUY  13× 2580 PE  @ ₹ 9.20    -₹29,900 dr               │     │
  │  │  BUY  13× 3020 CE  @ ₹ 7.40    -₹24,050 dr               │     │
  │  │                                                           │     │
  │  │  Net Credit:   ₹60,775                                    │     │
  │  │  Max Loss:     ₹1,99,225                                  │     │
  │  │  Margin Req:   ₹15,60,000                                 │     │
  │  │  Available:    ₹42,00,000                                 │     │
  │  │                                                           │     │
  │  │  [Confirm & Execute]                                      │     │
  │  └───────────────────────────────────────────────────────────┘     │
  │                                                                     │
  │  User clicks "Confirm & Execute"                                   │
  │                                                                     │
  │  SlippageController executes 4 legs:                               │
  │    Leg 1: SELL 2660 PE → mid ₹18.50, filled @ ₹18.35 (step 1)     │
  │    Leg 2: SELL 2940 CE → mid ₹16.80, filled @ ₹16.70 (step 1)     │
  │    Leg 3: BUY  2580 PE → mid ₹9.20,  filled @ ₹9.25  (step 1)    │
  │    Leg 4: BUY  3020 CE → mid ₹7.40,  filled @ ₹7.50  (step 2)    │
  │                                                                     │
  │  Actual credit = (18.35 + 16.70) - (9.25 + 7.50) = ₹18.30/share   │
  │  Slippage = ₹0.40/share = 0.21% < 1% budget  ✅                   │
  │  Credit ₹59,475 > 2× txn costs ₹4,200  ✅                         │
  │                                                                     │
  │  ══════════════════════════════════════════════                      │
  │  POSITION NOW ACTIVE — DTE: 44, Entry IV: 28.5%                    │
  │  ══════════════════════════════════════════════                      │
  └─────────────────────────────────────────────────────────────────────┘
```

---

### STEP 8 — Monitoring Phase (Days 1-18, No Action Needed)

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │  DynamicHedgingEngine.evaluate_position() — every 10s              │
  │                                                                     │
  │  Day 1-18: RELIANCE drifts between ₹2,760 — ₹2,840               │
  │                                                                     │
  │  Timeline:                                                          │
  │  ─────────────────────────────────────────────────────────────      │
  │  Day  Spot   Put Δ  Call Δ  IV     P&L     Decision                │
  │  ─────────────────────────────────────────────────────────────      │
  │   1   2795   0.17   0.15    28.2%  +₹ 800  NONE (healthy)         │
  │   3   2810   0.14   0.17    27.8%  +₹3,200  NONE                  │
  │   5   2780   0.19   0.13    27.5%  +₹5,100  NONE                  │
  │   8   2760   0.22   0.11    26.0%  +₹9,800  NONE (Δ < 0.30)      │
  │  10   2790   0.16   0.14    25.5%  +₹14,200 NONE                  │
  │  14   2830   0.11   0.19    24.0%  +₹19,500 NONE                  │
  │  18   2840   0.10   0.20    23.5%  +₹22,100 NONE                  │
  │  ─────────────────────────────────────────────────────────────      │
  │                                                                     │
  │  Theta decay doing the work. All deltas well under 0.30.           │
  │  IV dropping from 28.5% → 23.5% accelerating profit.               │
  └─────────────────────────────────────────────────────────────────────┘
```

---

### STEP 9 — Trade Goes Against Us (Day 19-21)

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │  Day 19: RELIANCE reports weak quarterly guidance after market      │
  │  Day 20: Gap down opens at ₹2,700, sells off to ₹2,680            │
  │                                                                     │
  │                                                                     │
  │  Spot  ₹2840                                                        │
  │    │    ·                                                           │
  │    │     ·                                                          │
  │    │      ·                                          DTE = 25       │
  │    │       ·                                                        │
  │    │   ₹2700 ·· gap down                                           │
  │    │            ·                                                   │
  │    │         ₹2680 ·                                                │
  │    │                ·                                               │
  │    │           ₹2660 · ← SHORT PUT STRIKE                          │
  │    │                  ·                                             │
  │    │             ₹2630 · ← BREACH (1.1%)                           │
  │    │                                                                │
  │    └──────────────────────────────────────▶ Time                    │
  │       Day18    Day19   Day20   Day21                                │
  │                                                                     │
  │                                                                     │
  │  evaluate_position() at 09:35 IST, Day 20:                         │
  │  ─────────────────────────────────────────                          │
  │  Priority 0: DTE = 25 > 1    → skip                                │
  │  Priority 1: DTE = 25 > 14   → skip                                │
  │  Priority 2: P&L = -₹38,000 (not profit) → skip                   │
  │  Priority 3: IV = 32% (UP, not down) → skip                        │
  │  Priority 4: No recent adjustment → skip                           │
  │  Priority 5: VIX = 18.5 > 12 → skip                               │
  │  Priority 6: DELTA CHECK                                           │
  │                                                                     │
  │    Short 2660 Put EWMA delta: 0.34  >  0.30 trigger               │
  │    ══════════════════════════════════════════                        │
  │    DELTA TRIGGER FIRED                                              │
  │    ══════════════════════════════════════════                        │
  │                                                                     │
  │    Is this an impulsive move?                                       │
  │      Price change in 5 min: ₹2,700 → ₹2,680 = -0.74%              │
  │      0.74% < 1.5% threshold → NOT impulsive                        │
  │      (gap was overnight, intraday move is gradual)                  │
  │                                                                     │
  │    ══════════════════════════════════════════                        │
  │    DECISION: ROLL_CHALLENGED                                        │
  │    Roll short put to 16Δ in next expiry                             │
  │    ══════════════════════════════════════════                        │
  └─────────────────────────────────────────────────────────────────────┘
```

---

### STEP 10 — Roll Challenged Leg (Day 20, 09:36 IST)

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │  Executing ROLL_CHALLENGED:                                        │
  │                                                                     │
  │  CLOSE: BUY BACK 13× RELIANCE 2660 PE (Apr 26 expiry)             │
  │    Entry sell price:  ₹18.35                                        │
  │    Buyback price:     ₹48.20                                        │
  │    Loss on this leg:  (48.20 - 18.35) × 250 × 13 = -₹97,013       │
  │                                                                     │
  │  OPEN:  SELL 13× RELIANCE 2580 PE (May 29 expiry, ~69 DTE)        │
  │    New strike 2580 is 16Δ at spot ₹2,680                            │
  │    Sell price:  ₹32.50                                              │
  │    New credit:  ₹32.50 × 250 × 13 = ₹1,05,625                     │
  │                                                                     │
  │  Also adjusting long wing:                                          │
  │  CLOSE: SELL 13× 2580 PE (Apr 26) @ ₹28.40 (was bought @ ₹9.25)   │
  │    Profit on wing: (28.40 - 9.25) × 250 × 13 = +₹62,238           │
  │                                                                     │
  │  OPEN:  BUY 13× 2500 PE (May 29 expiry)                            │
  │    New wing @ 10Δ, price: ₹18.60                                    │
  │    Cost: ₹18.60 × 250 × 13 = -₹60,450                             │
  │                                                                     │
  │  ══════════════════════════════════════════════════════════          │
  │  ROLL SUMMARY:                                                      │
  │    Realized loss on closed legs:  -₹97,013 + ₹62,238 = -₹34,775   │
  │    New position: wider, further out                                 │
  │    Fresh theta income from new short put                            │
  │    Cooldown set: 15 minutes (until 09:51 IST)                      │
  │  ══════════════════════════════════════════════════════════          │
  │                                                                     │
  │  Updated Position:                                                  │
  │    SELL 13× 2580 PE  (May 29)  @ ₹32.50   ← NEW (rolled)          │
  │    SELL 13× 2940 CE  (Apr 26)  @ ₹16.70   ← unchanged             │
  │    BUY  13× 2500 PE  (May 29)  @ ₹18.60   ← NEW wing              │
  │    BUY  13× 3020 CE  (Apr 26)  @ ₹ 7.50   ← unchanged             │
  └─────────────────────────────────────────────────────────────────────┘
```

---

### STEP 11 — Untested Side Check (Day 22)

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │  Day 22: RELIANCE stabilizes at ₹2,710                            │
  │                                                                     │
  │  evaluate_position() hits Priority 7: UNTESTED SIDE ROLL           │
  │                                                                     │
  │  Condition 1: Challenged side re-centered?                          │
  │    New short 2580 Put delta = 0.14 (back to safe zone)  ✅ YES     │
  │                                                                     │
  │  Condition 2: IV within 10% of entry IV?                            │
  │    Current IV  = 31.0%                                              │
  │    Entry IV    = 28.5%                                              │
  │    Difference  = (31.0 - 28.5) / 28.5 = 8.8%  ≤ 10%   ✅ YES     │
  │                                                                     │
  │  BOTH conditions met:                                               │
  │  ══════════════════════════════════════════════                      │
  │  DECISION: ROLL_UNTESTED                                            │
  │  Roll short call from 2940 → 2880 (20Δ at current spot)           │
  │  Tighten the call side to collect more premium                     │
  │  ══════════════════════════════════════════════                      │
  │                                                                     │
  │  CLOSE: BUY BACK 13× 2940 CE (Apr 26) @ ₹5.20                     │
  │    Profit: (16.70 - 5.20) × 250 × 13 = +₹37,375                   │
  │                                                                     │
  │  OPEN:  SELL 13× 2880 CE (Apr 26) @ ₹12.80                         │
  │    Credit: ₹12.80 × 250 × 13 = ₹41,600                            │
  │                                                                     │
  │  Updated Position (final form):                                     │
  │    SELL 13× 2580 PE  (May 29)  ← challenged side (rolled)          │
  │    SELL 13× 2880 CE  (Apr 26)  ← untested side (tightened)         │
  │    BUY  13× 2500 PE  (May 29)  ← wing                              │
  │    BUY  13× 3020 CE  (Apr 26)  ← wing (wider protection)          │
  └─────────────────────────────────────────────────────────────────────┘
```

---

### STEP 12 — Profit Target Hit (Day 30)

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │  Day 30: RELIANCE recovers to ₹2,780. IV drops to 22%.            │
  │  DTE on Apr legs = 14 (gamma exit approaching)                     │
  │  DTE on May legs = 43                                               │
  │                                                                     │
  │  evaluate_position():                                               │
  │                                                                     │
  │  Priority 0: min DTE = 14 → exactly at gamma threshold             │
  │  Priority 1: DTE ≤ 14?  14 ≤ 14?   ✅ YES                         │
  │                                                                     │
  │  ══════════════════════════════════════════════════════════          │
  │  DECISION: GAMMA_EXIT — close all legs                              │
  │  (approaching expiry, gamma risk too high)                          │
  │  ══════════════════════════════════════════════════════════          │
  │                                                                     │
  │  Closing all 4 legs:                                                │
  │    BUY BACK 2580 PE (May 29) @ ₹14.80  (sold @ 32.50, +₹17.70)    │
  │    BUY BACK 2880 CE (Apr 26) @ ₹ 3.10  (sold @ 12.80, +₹ 9.70)   │
  │    SELL     2500 PE (May 29) @ ₹ 8.90  (bought @ 18.60, -₹ 9.70)  │
  │    SELL     3020 CE (Apr 26) @ ₹ 0.40  (bought @  7.50, -₹ 7.10)  │
  └─────────────────────────────────────────────────────────────────────┘
```

---

### FINAL P&L SUMMARY

```
  ╔══════════════════════════════════════════════════════════════════════╗
  ║                    TRADE P&L BREAKDOWN                              ║
  ╠══════════════════════════════════════════════════════════════════════╣
  ║                                                                     ║
  ║  PHASE 1: Original Position (Day 1-20)                             ║
  ║  ─────────────────────────────────────                              ║
  ║  Short 2660 PE: sold 18.35, bought back 48.20    = -₹29.85/share   ║
  ║  Long  2580 PE: bought 9.25, sold 28.40          = +₹19.15/share   ║
  ║  Realized P&L Phase 1 (put side):  -₹10.70 × 250 × 13 = -₹34,775 ║
  ║                                                                     ║
  ║  PHASE 2: Rolled Position (Day 20-30)                              ║
  ║  ────────────────────────────────────                               ║
  ║  Short 2580 PE: sold 32.50, bought back 14.80    = +₹17.70/share   ║
  ║  Short 2880 CE: sold 12.80, bought back 3.10     = +₹ 9.70/share   ║
  ║  Long  2500 PE: bought 18.60, sold 8.90          = -₹ 9.70/share   ║
  ║                                                                     ║
  ║  Original call side (held through):                                 ║
  ║  Short 2940 CE: sold 16.70, bought back 5.20     = +₹11.50/share   ║
  ║  Long  3020 CE: bought 7.50, sold 0.40           = -₹ 7.10/share   ║
  ║                                                                     ║
  ║  ══════════════════════════════════════════════════════════          ║
  ║                                                                     ║
  ║  Total P&L per share:                                               ║
  ║    Phase 1 put close:    -₹10.70                                    ║
  ║    Phase 2 new put:      +₹17.70                                    ║
  ║    Phase 2 new put wing: -₹ 9.70                                    ║
  ║    Call side (rolled):   +₹ 9.70                                    ║
  ║    Original call wing:   -₹ 7.10                                    ║
  ║    Original short call:  +₹11.50                                    ║
  ║    ─────────────────────────────                                    ║
  ║    Net: +₹11.40 per share                                          ║
  ║                                                                     ║
  ║  Total P&L = ₹11.40 × 250 × 13 = +₹37,050                         ║
  ║                                                                     ║
  ║  Return on max risk: ₹37,050 / ₹1,99,225 = +18.6%                 ║
  ║  Return on AUM:      ₹37,050 / ₹1,00,00,000 = +0.37%              ║
  ║  Duration: 30 calendar days                                         ║
  ║                                                                     ║
  ║  Transaction costs (8 legs × ₹20 brokerage + STT + exchange):      ║
  ║  ≈ ₹4,800 total → Net P&L ≈ +₹32,250                              ║
  ║                                                                     ║
  ╠══════════════════════════════════════════════════════════════════════╣
  ║                                                                     ║
  ║  KEY TAKEAWAY: Despite RELIANCE dropping 5% against us,            ║
  ║  the engine's roll mechanics preserved the position and             ║
  ║  the trade ended profitable. The 4-layer defense worked:           ║
  ║                                                                     ║
  ║    Layer 1: EWMA filtered Day 19 noise (raw Δ spiked briefly)      ║
  ║    Layer 2: Gradual classification → ROLL (not panic CLOSE_ALL)    ║
  ║    Layer 3: Post-roll untested tighten → recaptured premium        ║
  ║    Layer 4: Gamma exit → closed before expiry risk escalated       ║
  ║                                                                     ║
  ╚══════════════════════════════════════════════════════════════════════╝
```

---

## 7. Comparative Review — tradeApp vs Sensibull (India's #1 Options Platform)

Sensibull (by Zerodha) is the most widely used options analytics platform in India with 500K+ users. This section benchmarks every component of the tradeApp VRP Framework against Sensibull's capabilities.

### Feature-by-Feature Comparison

```
╔═══════════════════════════════════════════════════════════════════════════════╗
║               tradeApp VRP Framework v2  vs  Sensibull Pro                  ║
╠═══════════════════════════════════════════════════════════════════════════════╣
║                                                                             ║
║  CATEGORY              tradeApp                 Sensibull                   ║
║  ════════              ════════                 ═════════                   ║
║                                                                             ║
║  ┌───────────────────────────────────────────────────────────────────────┐  ║
║  │  1. SIGNAL GENERATION / ENTRY LOGIC                                  │  ║
║  ├───────────────────────────────────────────────────────────────────────┤  ║
║  │                                                                       │  ║
║  │  IV-RV Spread         ✅ Core metric          ❌ Not available        │  ║
║  │  IV Percentile        ✅ 252-day lookback      ✅ IVP available       │  ║
║  │  IV/RV Ratio          ✅ Tier 1+2 thresholds   ❌ Not computed        │  ║
║  │  Realized Vol (HV)    ✅ 30-day rolling        ❌ Not displayed       │  ║
║  │  VRP Gate (3-check)   ✅ Automated gate        ❌ No concept of VRP   │  ║
║  │  Put/Call Skew Ratio  ✅ Drives strategy pick  ❌ Not exposed         │  ║
║  │  Term Structure       ✅ Analyzed for calendars ❌ Not analyzed        │  ║
║  │                                                                       │  ║
║  │  VERDICT: tradeApp has institutional-grade signal generation.        │  ║
║  │  Sensibull relies on user judgment for entry timing.                 │  ║
║  └───────────────────────────────────────────────────────────────────────┘  ║
║                                                                             ║
║  ┌───────────────────────────────────────────────────────────────────────┐  ║
║  │  2. STRATEGY SELECTION                                               │  ║
║  ├───────────────────────────────────────────────────────────────────────┤  ║
║  │                                                                       │  ║
║  │  Auto selection       ✅ Rule-based decision    ~ Easy Options        │  ║
║  │                         tree (5 strategies)      (3 risk tiers)       │  ║
║  │  Skew-driven routing  ✅ Put/call skew →        ❌ Manual selection   │  ║
║  │                         strategy mapping                              │  ║
║  │  VIX-regime switch    ✅ Low-vol protocol       ❌ Not available      │  ║
║  │                         (VIX<12 → widen)                              │  ║
║  │  Strategy templates   5 (IC, Strangle,          40+ (including       │  ║
║  │                        Put Spread, Calendar,     exotic structures)   │  ║
║  │                        Ratio Call, BWB)                               │  ║
║  │  Custom builder       ❌ Not needed (auto)      ✅ Full custom UI     │  ║
║  │                                                                       │  ║
║  │  VERDICT: tradeApp auto-selects based on vol surface analysis.      │  ║
║  │  Sensibull has more templates but selection is manual/user-driven.   │  ║
║  └───────────────────────────────────────────────────────────────────────┘  ║
║                                                                             ║
║  ┌───────────────────────────────────────────────────────────────────────┐  ║
║  │  3. STRIKE SELECTION                                                 │  ║
║  ├───────────────────────────────────────────────────────────────────────┤  ║
║  │                                                                       │  ║
║  │  Method               ✅ Delta-based (BS)       ~ Percentage-based   │  ║
║  │                         16Δ short, 10Δ wing      (~1% of spot,       │  ║
║  │                                                   rounded to strike)  │  ║
║  │  Precision            ✅ Exact delta targeting   ~ Approximate        │  ║
║  │  Risk-calibrated      ✅ Prob of ITM = delta     ❌ Fixed % rule      │  ║
║  │  Adaptive to IV       ✅ Higher IV → wider       ❌ Same % always     │  ║
║  │                         strikes auto                                  │  ║
║  │                                                                       │  ║
║  │  VERDICT: tradeApp's delta-based strikes are self-adjusting to IV.  │  ║
║  │  Sensibull's %-based approach ignores vol environment entirely.      │  ║
║  └───────────────────────────────────────────────────────────────────────┘  ║
║                                                                             ║
║  ┌───────────────────────────────────────────────────────────────────────┐  ║
║  │  4. POSITION SIZING                                                  │  ║
║  ├───────────────────────────────────────────────────────────────────────┤  ║
║  │                                                                       │  ║
║  │  Risk-based sizing    ✅ 2% AUM per name        ❌ No sizing engine   │  ║
║  │  Margin constraint    ✅ 80% AUM cap            ~ Shows margin only   │  ║
║  │  Hard cap             ✅ 500 lots max            ❌ No cap             │  ║
║  │  Premium budget       ✅ 1% AUM max premium     ❌ Not available      │  ║
║  │  Easy Options sizing  N/A                        ~ Max loss ≈₹4-5K    │  ║
║  │                                                                       │  ║
║  │  VERDICT: tradeApp has full Kelly-style sizing with 4 constraints.  │  ║
║  │  Sensibull has no position sizing — user decides lot count.          │  ║
║  └───────────────────────────────────────────────────────────────────────┘  ║
║                                                                             ║
║  ┌───────────────────────────────────────────────────────────────────────┐  ║
║  │  5. PORTFOLIO RISK MANAGEMENT                                        │  ║
║  ├───────────────────────────────────────────────────────────────────────┤  ║
║  │                                                                       │  ║
║  │  Portfolio Greeks     ✅ Aggregated, NIFTY-     ❌ Per-position only  │  ║
║  │                         normalized                                    │  ║
║  │  Beta-weighted delta  ✅ Per-Cr AUM units       ❌ Not available      │  ║
║  │  Vega stress test     ✅ VIX-doubling scenario  ❌ Not available      │  ║
║  │  Sector limits        ✅ 5 per GICS sector      ❌ Not available      │  ║
║  │  Correlation check    ✅ 20-day pairwise avg    ❌ Not available      │  ║
║  │  Position count limit ✅ 25 (12 in CB mode)     ❌ Not available      │  ║
║  │  Monthly drawdown     ✅ 4% circuit breaker     ❌ Not available      │  ║
║  │  Margin shortfall     ✅ Pre-trade check        ✅ Warnings shown     │  ║
║  │                                                                       │  ║
║  │  VERDICT: tradeApp has 7 portfolio-level risk gates.                │  ║
║  │  Sensibull has no portfolio-level risk management at all.            │  ║
║  └───────────────────────────────────────────────────────────────────────┘  ║
║                                                                             ║
║  ┌───────────────────────────────────────────────────────────────────────┐  ║
║  │  6. DYNAMIC HEDGING / ADJUSTMENTS (THE BIGGEST GAP)                 │  ║
║  ├───────────────────────────────────────────────────────────────────────┤  ║
║  │                                                                       │  ║
║  │  Auto delta monitor   ✅ Every 10s, EWMA        ❌ Manual only        │  ║
║  │  Hysteresis           ✅ 30Δ fire / 20Δ reset   ❌ Not available      │  ║
║  │  Impulsive detection  ✅ Price+volume classify   ❌ Not available      │  ║
║  │  Roll challenged leg  ✅ Auto to 16Δ next exp   ❌ Manual             │  ║
║  │  Roll untested side   ✅ 2-condition gate        ❌ Not available      │  ║
║  │  ITM breach rollout   ✅ Theta-exhaustion check  ❌ Not available      │  ║
║  │  Vanna close          ✅ IV-crush detection      ❌ Not available      │  ║
║  │  Gamma exit           ✅ DTE ≤ 14 auto          ❌ Manual             │  ║
║  │  Profit target        ✅ 40% auto-close          ❌ Manual             │  ║
║  │  Cooldown/whipsaw     ✅ 15-min Redis lock       ❌ Not applicable    │  ║
║  │  Adjustment types     10 decision outcomes       0 (no adjustments)   │  ║
║  │                                                                       │  ║
║  │  Sensibull offers:                                                   │  ║
║  │    • Analyse Widget: model what-if before manual adjustment          │  ║
║  │    • Conditional Exit: exit all when underlying hits price level     │  ║
║  │    • No rolling, no delta-triggered rebalance, no Greeks alerts      │  ║
║  │                                                                       │  ║
║  │  VERDICT: This is where tradeApp is FUNDAMENTALLY different.        │  ║
║  │  Sensibull is a visualization tool. tradeApp is a decision engine.  │  ║
║  └───────────────────────────────────────────────────────────────────────┘  ║
║                                                                             ║
║  ┌───────────────────────────────────────────────────────────────────────┐  ║
║  │  7. EXECUTION                                                        │  ║
║  ├───────────────────────────────────────────────────────────────────────┤  ║
║  │                                                                       │  ║
║  │  Basket orders        ✅ Via Kite API            ✅ One-click UI      │  ║
║  │  Order slicing        ❌ Not implemented         ✅ Auto-slicing      │  ║
║  │  Slippage control     ✅ 4-step chase, 1% cap   ~ Market Protection  │  ║
║  │                                                    (0.5-5% from LTP)  │  ║
║  │  Edge validation      ✅ Credit > 2× txn cost   ❌ Not available      │  ║
║  │  Txn cost engine      ✅ Full SEBI 2024 model   ❌ Not computed       │  ║
║  │                                                                       │  ║
║  │  VERDICT: Comparable. Sensibull has better order slicing.           │  ║
║  │  tradeApp has better slippage control and edge validation.          │  ║
║  └───────────────────────────────────────────────────────────────────────┘  ║
║                                                                             ║
║  ┌───────────────────────────────────────────────────────────────────────┐  ║
║  │  8. SCANNING / SCREENING                                            │  ║
║  ├───────────────────────────────────────────────────────────────────────┤  ║
║  │                                                                       │  ║
║  │  Universe             ✅ 107 symbols auto-scan  ✅ All F&O stocks     │  ║
║  │  IV-RV screening      ✅ Spread, ratio, pctl    ❌ IVP only           │  ║
║  │  Skew screening       ✅ Put/Call skew ratios   ❌ Not available      │  ║
║  │  Sector filtering     ✅ GICS sector mapping    ❌ Not available      │  ║
║  │  VRP gate filtering   ✅ Pass/Fail per symbol   ❌ Not available      │  ║
║  │  OI/PCR screening     ❌ Not implemented        ✅ Full OI suite      │  ║
║  │  Buildup detection    ❌ Not implemented        ✅ Long/short buildup │  ║
║  │  Heat map             ❌ Not available           ✅ Color-coded map   │  ║
║  │                                                                       │  ║
║  │  VERDICT: Different focus. tradeApp screens for vol edge.           │  ║
║  │  Sensibull screens for OI/flow patterns. Both are useful.           │  ║
║  └───────────────────────────────────────────────────────────────────────┘  ║
║                                                                             ║
║  ┌───────────────────────────────────────────────────────────────────────┐  ║
║  │  9. BACKTESTING                                                      │  ║
║  ├───────────────────────────────────────────────────────────────────────┤  ║
║  │                                                                       │  ║
║  │  Backtesting engine   ✅ Built-in (backtester)  ❌ Not available      │  ║
║  │                         Most requested feature by Sensibull users    │  ║
║  │                                                                       │  ║
║  │  VERDICT: tradeApp has it. Sensibull users have been asking for     │  ║
║  │  years — still not available.                                        │  ║
║  └───────────────────────────────────────────────────────────────────────┘  ║
║                                                                             ║
║  ┌───────────────────────────────────────────────────────────────────────┐  ║
║  │  10. CIRCUIT BREAKER / DRAWDOWN PROTECTION                          │  ║
║  ├───────────────────────────────────────────────────────────────────────┤  ║
║  │                                                                       │  ║
║  │  Monthly drawdown     ✅ 4% trigger, auto-      ❌ Not available      │  ║
║  │                         tightens all gates                            │  ║
║  │  Position reduction   ✅ 50% cut when active    ❌ Not available      │  ║
║  │  VRP gate tightening  ✅ Spread → 3.5 pts       ❌ No concept         │  ║
║  │  Monthly reset        ✅ Calendar month cycle   ❌ Not available      │  ║
║  │                                                                       │  ║
║  │  VERDICT: tradeApp has a full circuit breaker system.               │  ║
║  │  Sensibull has no drawdown protection at all.                        │  ║
║  └───────────────────────────────────────────────────────────────────────┘  ║
╚═══════════════════════════════════════════════════════════════════════════════╝
```

---

### Overall Scorecard

```
╔══════════════════════════════════════════════════════════════════════════╗
║                       OVERALL SCORECARD                                ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                        ║
║  Category                    tradeApp    Sensibull    Winner            ║
║  ────────────────────────    ────────    ─────────    ──────            ║
║  Signal Generation              9/10       3/10       tradeApp          ║
║  Strategy Selection             8/10       7/10       tradeApp          ║
║  Strike Selection               9/10       5/10       tradeApp          ║
║  Position Sizing                9/10       2/10       tradeApp          ║
║  Portfolio Risk Mgmt            9/10       1/10       tradeApp          ║
║  Dynamic Hedging               10/10       1/10       tradeApp          ║
║  Execution                      7/10       8/10       Sensibull         ║
║  Scanning/Screening             7/10       7/10       Tie               ║
║  Backtesting                    7/10       0/10       tradeApp          ║
║  Circuit Breaker                9/10       0/10       tradeApp          ║
║  UI/UX/Visualization            4/10       9/10       Sensibull         ║
║  Broker Integration             6/10       8/10       Sensibull         ║
║  ────────────────────────    ────────    ─────────    ──────            ║
║  TOTAL                        94/120     51/120       tradeApp          ║
║                                                                        ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                        ║
║  SUMMARY:                                                              ║
║                                                                        ║
║  Sensibull is a VISUALIZATION + EXECUTION platform.                    ║
║  It excels at:                                                         ║
║    • Payoff diagrams and scenario analysis                             ║
║    • 40+ strategy templates with one-click execution                   ║
║    • OI/flow analysis (buildup, PCR, Max Pain)                         ║
║    • Beautiful UI accessible to retail traders                         ║
║    • Multi-broker support (Zerodha, Angel, Upstox, ICICI)             ║
║                                                                        ║
║  tradeApp is a DECISION + RISK ENGINE.                                 ║
║  It excels at:                                                         ║
║    • Automated signal generation via VRP analysis                      ║
║    • Institutional-grade risk management (7 portfolio gates)           ║
║    • Dynamic hedging with 10 adjustment outcomes                       ║
║    • Anti-whipsaw mechanics (EWMA, hysteresis, cooldown)              ║
║    • Circuit breaker for drawdown protection                           ║
║    • Backtesting capability                                            ║
║                                                                        ║
║  KEY INSIGHT: Sensibull tells you WHAT your position looks like.       ║
║  tradeApp tells you WHAT TO DO about it.                               ║
║                                                                        ║
║  They are complementary, not competitive.                              ║
║  Sensibull = analytics dashboard for manual traders.                   ║
║  tradeApp  = autonomous trading system for systematic traders.         ║
║                                                                        ║
╚══════════════════════════════════════════════════════════════════════════╝
```

---

### Gaps in tradeApp (Where Sensibull is Better)

```
╔══════════════════════════════════════════════════════════════════════════╗
║               tradeApp IMPROVEMENT OPPORTUNITIES                       ║
║               (Features Sensibull Has That tradeApp Lacks)             ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                        ║
║  1. ORDER SLICING (Priority: Medium)                                   ║
║     Sensibull auto-slices orders exceeding exchange freeze qty.         ║
║     tradeApp currently sends full-size orders via Kite API.            ║
║     Impact: Large lot counts may get rejected by exchange.             ║
║                                                                        ║
║  2. OI / FLOW ANALYSIS (Priority: Low)                                 ║
║     Sensibull has OI charts, PCR, Max Pain, long/short buildup.        ║
║     tradeApp focuses purely on vol surface — no OI integration.        ║
║     Impact: OI data can confirm/deny vol signals.                      ║
║                                                                        ║
║  3. PAYOFF VISUALIZATION (Priority: Medium)                            ║
║     Sensibull shows interactive payoff diagrams with SD bands.         ║
║     tradeApp execution popup shows legs/margin but no payoff chart.    ║
║     Impact: Visual confirmation helps user trust before execution.     ║
║                                                                        ║
║  4. PROBABILITY OF PROFIT (Priority: Low)                              ║
║     Sensibull shows POP per strategy.                                  ║
║     tradeApp has BS engine capable of computing POP but doesn't        ║
║     surface it in the UI.                                              ║
║     Impact: Quick risk communication metric.                           ║
║                                                                        ║
║  5. MULTI-BROKER SUPPORT (Priority: Low)                               ║
║     Sensibull supports 4+ brokers.                                     ║
║     tradeApp only supports Zerodha Kite.                               ║
║     Impact: Limits user base to Zerodha customers.                     ║
║                                                                        ║
║  6. PAPER TRADING / DRAFT MODE (Priority: Medium)                      ║
║     Sensibull has draft portfolios for virtual trading.                 ║
║     tradeApp has backtester but no live paper trading mode.            ║
║     Impact: Users can't test the engine in real-time without risk.     ║
║                                                                        ║
╚══════════════════════════════════════════════════════════════════════════╝
```

---

## 8. Full Industry Review — tradeApp vs All Major Indian Option Selling Platforms

This section benchmarks tradeApp against every major Indian options platform:
**Sensibull** (Zerodha), **Opstra** (Definedge), **Tradetron**, **AlgoTest**, **QuantsApp**, **StockMock**, and **Streak** (Zerodha).

---

### Platform Profiles

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                  INDIAN OPTIONS PLATFORM LANDSCAPE                        ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                            ║
║  Platform     Type                     Primary Strength                    ║
║  ──────────   ────                     ────────────────                    ║
║  Sensibull    Analytics + Execution    Payoff visualization, OI analysis   ║
║  Opstra       Analytics                IV skew charts, OI bubble charts    ║
║  Tradetron    No-code Algo Builder     200 keywords, repair sets, 100+     ║
║                                        broker APIs                         ║
║  AlgoTest     Backtesting + Execution  Monte Carlo, VRP tool, 4 RV        ║
║                                        estimators, Kelly sizing            ║
║  QuantsApp    OI & Order Flow          33+ real-time order analytics,      ║
║                                        deepest OI suite in India           ║
║  StockMock    Options Backtesting      Time-based backtest, shift/re-entry ║
║                                        rules for option selling            ║
║  Streak       Tech Strategy Automation No-code builder, server-side live   ║
║                                        execution, marketplace              ║
║  ──────────   ────                     ────────────────                    ║
║  tradeApp     Decision + Risk Engine   VRP signal generation, dynamic      ║
║                                        hedging, portfolio risk gates,      ║
║                                        circuit breaker                     ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

### Master Comparison Matrix (30 Features × 8 Platforms)

```
╔═══════════════════════════════════════════════════════════════════════════════════════════════════╗
║  Feature                  tradeApp  Sensibull  Opstra  Tradetron  AlgoTest  QuantsApp  StockMock  Streak ║
╠═══════════════════════════════════════════════════════════════════════════════════════════════════╣
║                                                                                                   ║
║  ── SIGNAL GENERATION ─────────────────────────────────────────────────────────────────────────── ║
║  IV-RV Spread              ✅        ❌         ~        ~          ✅        ❌         ❌         ❌  ║
║  IV Percentile             ✅        ✅         ✅       ~          ✅        ~          ❌         ❌  ║
║  IV/RV Ratio               ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║  Realized Vol (HV)         ✅        ❌         ✅       ~          ✅        ❌         ❌         ❌  ║
║  VRP Gate (auto filter)    ✅        ❌         ❌       ❌         ~         ❌         ❌         ❌  ║
║  Put/Call Skew Analysis    ✅        ❌         ✅       ❌         ✅        ❌         ❌         ❌  ║
║  Term Structure            ✅        ❌         ~        ❌         ❌        ❌         ❌         ❌  ║
║  VRP Distribution Analysis ❌        ❌         ❌       ❌         ✅        ❌         ❌         ❌  ║
║  Multiple RV Estimators    ❌        ❌         ❌       ❌         ✅        ❌         ❌         ❌  ║
║                                                                                                   ║
║  ── STRATEGY & STRIKE SELECTION ───────────────────────────────────────────────────────────────── ║
║  Auto strategy selection   ✅        ~          ❌       ❌         ❌        ❌         ❌         ❌  ║
║  Delta-based strikes       ✅        ❌         ❌       ❌         ✅        ❌         ~          ❌  ║
║  Skew-driven routing       ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║  VIX-regime adaptation     ✅        ❌         ❌       ~          ❌        ❌         ❌         ❌  ║
║  Strategy templates        5         40+        20+     30+        10+       ~          10+        ❌  ║
║  Custom strategy builder   ❌        ✅         ✅      ✅         ✅        ✅         ✅         ✅  ║
║                                                                                                   ║
║  ── POSITION SIZING ───────────────────────────────────────────────────────────────────────────── ║
║  Risk-based sizing         ✅        ❌         ❌       ~          ✅        ❌         ❌         ❌  ║
║  Kelly Criterion           ~         ❌         ❌       ❌         ✅        ❌         ❌         ❌  ║
║  Margin constraint         ✅        ~          ~        ~          ✅        ❌         ❌         ❌  ║
║  Hard lot cap              ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║  Premium budget check      ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║                                                                                                   ║
║  ── PORTFOLIO RISK MANAGEMENT ─────────────────────────────────────────────────────────────────── ║
║  Portfolio Greeks (agg)    ✅        ❌         ~        ✅         ~         ~          ❌         ❌  ║
║  Beta-weighted delta       ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║  Vega stress test          ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║  Sector concentration      ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║  Correlation constraint    ✅        ❌         ❌       ❌         ~         ❌         ❌         ❌  ║
║  Position count limits     ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║  Circuit breaker           ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║  Monthly drawdown trigger  ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║                                                                                                   ║
║  ── DYNAMIC HEDGING / ADJUSTMENTS ─────────────────────────────────────────────────────────────── ║
║  Auto delta monitoring     ✅        ❌         ❌       ✅         ❌        ❌         ❌         ❌  ║
║  EWMA smoothing            ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║  Hysteresis (fire/reset)   ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║  Impulsive move detect     ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║  Roll challenged leg       ✅        ❌         ❌       ~          ❌        ❌         ~          ❌  ║
║  Roll untested side        ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║  ITM breach rollout        ✅        ❌         ❌       ~          ❌        ❌         ❌         ❌  ║
║  Vanna close               ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║  Gamma exit (DTE-based)    ✅        ❌         ❌       ~          ❌        ❌         ❌         ❌  ║
║  Profit target auto-close  ✅        ❌         ❌       ✅         ❌        ❌         ❌         ❌  ║
║  Cooldown / anti-whipsaw   ✅        ❌         ❌       ~          ❌        ❌         ❌         ❌  ║
║  Adjustment decision count 10        0          0        ~5         0         0          ~3         0   ║
║                                                                                                   ║
║  ── EXECUTION ─────────────────────────────────────────────────────────────────────────────────── ║
║  Basket orders             ✅        ✅         ❌       ✅         ✅        ❌         ❌         ✅  ║
║  Order slicing             ❌        ✅         ❌       ❌         ✅        ❌         ❌         ❌  ║
║  Slippage control          ✅        ~          ❌       ❌         ❌        ❌         ❌         ❌  ║
║  Edge validation           ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║  Txn cost engine           ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║  Multi-broker support      ❌        ✅(4)      ❌       ✅(100+)   ✅(50+)   ❌         ❌         ✅(5+) ║
║                                                                                                   ║
║  ── BACKTESTING ───────────────────────────────────────────────────────────────────────────────── ║
║  Options backtester        ✅        ❌         ✅       ✅         ✅        ❌         ✅         ~   ║
║  Monte Carlo simulation    ❌        ❌         ❌       ❌         ✅        ❌         ❌         ❌  ║
║  In/Out-of-sample split    ❌        ❌         ❌       ❌         ✅        ❌         ❌         ❌  ║
║  Shift/re-entry rules      ❌        ❌         ❌       ✅         ~         ❌         ✅         ❌  ║
║  Parameter optimizer       ❌        ❌         ❌       ❌         ✅        ❌         ❌         ❌  ║
║                                                                                                   ║
║  ── SCANNING / SCREENING ──────────────────────────────────────────────────────────────────────── ║
║  IV/VRP-based scanner      ✅        ❌         ~        ❌         ✅        ❌         ❌         ❌  ║
║  OI analysis suite         ❌        ✅         ✅       ~          ~         ✅         ❌         ❌  ║
║  Order flow analytics      ❌        ❌         ❌       ❌         ❌        ✅         ❌         ❌  ║
║  Sector filtering          ✅        ❌         ❌       ❌         ❌        ❌         ❌         ❌  ║
║  Heatmap                   ❌        ✅         ✅       ❌         ❌        ❌         ❌         ❌  ║
║                                                                                                   ║
║  ── UI / VISUALIZATION ────────────────────────────────────────────────────────────────────────── ║
║  Payoff diagrams           ❌        ✅         ✅       ❌         ✅        ❌         ❌         ❌  ║
║  IV skew charts            ❌        ❌         ✅       ❌         ✅        ❌         ❌         ❌  ║
║  Straddle/strangle charts  ❌        ✅         ✅       ❌         ✅        ❌         ❌         ❌  ║
║  Paper trading mode        ❌        ✅         ❌       ✅         ✅        ❌         ✅         ✅  ║
║  Mobile app                ❌        ❌         ❌       ❌         ❌        ✅         ❌         ✅  ║
║                                                                                                   ║
║  Legend:  ✅ = Yes    ~ = Partial/Basic    ❌ = Not available                                      ║
╚═══════════════════════════════════════════════════════════════════════════════════════════════════╝
```

---

### Category-Wise Deep Dive

#### A. Signal Generation — Who Can Find the Edge?

```
╔══════════════════════════════════════════════════════════════════════════╗
║              SIGNAL GENERATION CAPABILITY RANKING                      ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                        ║
║  Rank  Platform     Score   Why                                        ║
║  ────  ────────     ─────   ───                                        ║
║   1    tradeApp     9/10    Only platform with a 3-check VRP gate      ║
║                             (spread + percentile + ratio), skew-       ║
║                             driven strategy routing, term structure    ║
║                             analysis, and full scan of 107 symbols     ║
║                                                                        ║
║   2    AlgoTest     8/10    Dedicated VRP tool with 4 RV estimators    ║
║                             (Integrated, Parkinson, Garman-Klass,      ║
║                             Yang-Zhang), IVP/IVR with 30-day          ║
║                             constant maturity, delta-based skew viz.   ║
║                             But: no automated gate, no signal gen.     ║
║                                                                        ║
║   3    Opstra       5/10    IV skew chart, IV vs HV comparison,        ║
║                             IV percentile. But no automated scan,     ║
║                             no VRP metric, no ratio calculation.       ║
║                                                                        ║
║   4    Sensibull    3/10    IVP available, IV chart (Pro). No HV,     ║
║                             no IV-RV spread, no skew, no VRP.         ║
║                                                                        ║
║   5    Tradetron    2/10    ATMIV and HV keywords exist (buildable),  ║
║                             but no native analytics. User must        ║
║                             construct everything from scratch.         ║
║                                                                        ║
║  6-8   QuantsApp,   0/10   No IV-RV analysis capability at all.       ║
║        StockMock,                                                      ║
║        Streak                                                          ║
║                                                                        ║
║  KEY INSIGHT: Only tradeApp and AlgoTest can systematically           ║
║  identify when implied vol is rich relative to realized vol.           ║
║  Everyone else relies on user intuition or IVP alone.                 ║
║                                                                        ║
║  tradeApp's edge over AlgoTest: automated gate with tiered            ║
║  thresholds (index vs stock), circuit breaker tightening,             ║
║  and continuous background scanning every 5 minutes.                   ║
║  AlgoTest's edge over tradeApp: 4 RV estimators vs 1,                ║
║  VRP distribution analysis, constant maturity normalization.           ║
╚══════════════════════════════════════════════════════════════════════════╝
```

#### B. Dynamic Hedging — Who Can Defend a Losing Position?

```
╔══════════════════════════════════════════════════════════════════════════╗
║              DYNAMIC HEDGING CAPABILITY RANKING                        ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                        ║
║  Rank  Platform     Score   Why                                        ║
║  ────  ────────     ─────   ───                                        ║
║   1    tradeApp     10/10   Only platform in India with a full         ║
║                             priority-cascaded hedging engine:          ║
║                             • 9 priority levels evaluated in order     ║
║                             • 10 distinct adjustment outcomes          ║
║                             • EWMA smoothing (5-tick) + hysteresis     ║
║                             • Impulsive vs gradual classification      ║
║                             • Vanna close (IV-crush detection)         ║
║                             • 2-condition untested side gate           ║
║                             • 15-min Redis cooldown (anti-whipsaw)    ║
║                             • ITM breach + theta exhaustion rollout    ║
║                                                                        ║
║   2    Tradetron    4/10    Can build adjustment logic via repair      ║
║                             sets + 200 keywords:                       ║
║                             • Net Delta / Gamma / Theta / Vega         ║
║                             • Delta Neutral auto-quantity keyword      ║
║                             • Cascading repair with tracking keywords  ║
║                             BUT: no built-in engine, no EWMA,         ║
║                             no hysteresis, no impulsive detection,     ║
║                             no cooldown, no vanna. User must build    ║
║                             everything from scratch with no-code      ║
║                             blocks — error-prone for complex logic.    ║
║                                                                        ║
║   3    StockMock    1/10    Shift rules + re-entry in backtests only.  ║
║                             Not live. Not Greeks-driven.               ║
║                                                                        ║
║  4-8   Sensibull,   0/10   Zero automated adjustment capability.      ║
║        Opstra,              Manual-only "analyze then act yourself."   ║
║        AlgoTest,                                                       ║
║        QuantsApp,                                                      ║
║        Streak                                                          ║
║                                                                        ║
║  KEY INSIGHT: tradeApp is the ONLY platform in India with a           ║
║  pre-built, priority-cascaded, anti-whipsaw hedging engine.           ║
║                                                                        ║
║  Tradetron CAN theoretically replicate parts of it, but:             ║
║  • No EWMA smoothing for delta (raw delta only)                       ║
║  • No hysteresis (would fire/reset at same threshold)                 ║
║  • No impulsive move classification (price+volume+time)               ║
║  • No vanna close logic (IV-crush + spot-stability)                   ║
║  • No volga correction in stress tests                                 ║
║  • Building this in Tradetron's no-code blocks would require          ║
║    50+ condition nodes and still lack several capabilities.            ║
╚══════════════════════════════════════════════════════════════════════════╝
```

#### C. Portfolio Risk Management — Who Prevents Blow-Ups?

```
╔══════════════════════════════════════════════════════════════════════════╗
║              PORTFOLIO RISK MANAGEMENT RANKING                         ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                        ║
║  tradeApp has 7 INDEPENDENT risk gates. No other Indian platform      ║
║  has more than 1.                                                      ║
║                                                                        ║
║  Gate                          tradeApp    Best Alternative            ║
║  ────                          ────────    ────────────────            ║
║  Beta-weighted delta limit     ✅ ±10/Cr   ❌ Nobody                   ║
║  Vega L1 (per VIX-pt)         ✅ 0.25%     ❌ Nobody                   ║
║  Vega L2 (VIX-double stress)  ✅ 8%        ❌ Nobody                   ║
║  Sector concentration (GICS)  ✅ 5/sector  ❌ Nobody                   ║
║  Correlation constraint        ✅ 0.65 cap  ~ AlgoTest (matrix only)  ║
║  Position count limit          ✅ 25/12     ❌ Nobody                   ║
║  Circuit breaker (drawdown)   ✅ 4% trigger ❌ Nobody                  ║
║                                                                        ║
║  Tradetron's portfolio Greeks keywords (Net Delta/Gamma/Theta/Vega)   ║
║  can enforce SOME of these, but:                                       ║
║  • No beta-weighting (raw delta sum, not NIFTY-normalized)            ║
║  • No sector-level limits                                              ║
║  • No correlation monitoring                                           ║
║  • No circuit breaker with automatic gate tightening                  ║
║  • No volga-corrected stress tests                                     ║
║                                                                        ║
║  AlgoTest's correlation matrix is analytical (backtesting),            ║
║  not a live enforcement gate.                                          ║
║                                                                        ║
║  VERDICT: tradeApp is the ONLY platform in India that can prevent     ║
║  portfolio-level blow-ups through enforced, pre-trade risk gates.     ║
║  Every other platform leaves this entirely to the trader.              ║
╚══════════════════════════════════════════════════════════════════════════╝
```

#### D. Backtesting — Who Can Validate Strategies?

```
╔══════════════════════════════════════════════════════════════════════════╗
║              BACKTESTING CAPABILITY RANKING                            ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                        ║
║  Rank  Platform     Score   Why                                        ║
║  ────  ────────     ─────   ───                                        ║
║   1    AlgoTest     10/10   Gold standard for Indian options backtest  ║
║                             • Monte Carlo drawdown (10K simulations)   ║
║                             • In-sample / out-of-sample split          ║
║                             • Parameter optimizer with heatmap         ║
║                             • Correlation matrix for portfolio         ║
║                             • 18 performance metrics                   ║
║                             • Slippage-adjustable                      ║
║                             • Portfolio backtesting (multi-strategy)   ║
║                             • NIFTY500 stocks + 6 indices              ║
║                                                                        ║
║   2    StockMock    7/10    Strong for time-based option selling       ║
║                             • Minute-level granularity                  ║
║                             • Shift/re-entry/trail SL rules            ║
║                             • Combined premium SL/target               ║
║                             • Year/month/day-wise reports              ║
║                             But: no Monte Carlo, no optimizer          ║
║                                                                        ║
║   3    Tradetron    6/10    Backtests with full keyword support        ║
║                             • All 200 keywords including Greeks/IV     ║
║                             • Stats PDF + positions CSV                ║
║                             But: data only from Jan 2020, no slippage  ║
║                             modeling, credit-based pricing, slow       ║
║                                                                        ║
║   4    tradeApp     5/10    Built-in backtester (backtester.py)        ║
║                             • Integrated with engine's VRP logic       ║
║                             But: no Monte Carlo, no optimizer,         ║
║                             no in/out-sample, fewer report metrics     ║
║                                                                        ║
║   5    Opstra       4/10    Template-based backtest with historical    ║
║                             options data. Limited customization.       ║
║                                                                        ║
║  6-8   Streak,      1/10   Limited or no options-specific backtesting ║
║        QuantsApp,                                                      ║
║        Sensibull                                                       ║
║                                                                        ║
║  KEY INSIGHT: AlgoTest's Monte Carlo + optimizer is best-in-class.    ║
║  tradeApp's advantage: its backtester can test the FULL VRP pipeline  ║
║  (gate + strategy + sizing + hedging), not just entry/exit rules.     ║
║  No other backtester can test VRP-gate-filtered, hedging-adjusted     ║
║  iron condor portfolios with sector limits and circuit breakers.      ║
╚══════════════════════════════════════════════════════════════════════════╝
```

#### E. Execution — Who Can Place Orders Best?

```
╔══════════════════════════════════════════════════════════════════════════╗
║              EXECUTION CAPABILITY RANKING                              ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                        ║
║  Rank  Platform     Score   Why                                        ║
║  ────  ────────     ─────   ───                                        ║
║   1    Tradetron    9/10    100+ broker integrations, dedicated bots,  ║
║                             continuous condition checking, multi-       ║
║                             broker execution, webhook triggers          ║
║                                                                        ║
║   2    Sensibull    8/10    One-click basket orders, auto order        ║
║                             slicing (freeze qty), market protection,   ║
║                             conditional exits, 4+ brokers              ║
║                                                                        ║
║   3    AlgoTest     7/10    50+ brokers, multi-broker/multi-API,       ║
║                             algo deployment, signal bridge              ║
║                                                                        ║
║   4    Streak       7/10    Server-side execution, 5+ brokers,         ║
║                             no-code deployment, auto-activation         ║
║                                                                        ║
║   5    tradeApp     6/10    Kite API basket orders, slippage control   ║
║                             (4-step chase, 1% cap), edge validation    ║
║                             (credit > 2× txn), full SEBI txn costs.   ║
║                             But: single broker only, no order slicing  ║
║                                                                        ║
║  6-8   Opstra,      0/10   No execution capability at all             ║
║        QuantsApp,                                                      ║
║        StockMock                                                       ║
║                                                                        ║
║  tradeApp's execution QUALITY is superior (slippage control +         ║
║  edge validation + txn cost engine). But execution BREADTH             ║
║  (broker count, order types) lags behind Tradetron/Sensibull.         ║
╚══════════════════════════════════════════════════════════════════════════╝
```

---

### Overall Industry Scorecard

```
╔══════════════════════════════════════════════════════════════════════════════════════════════╗
║                            OVERALL INDUSTRY SCORECARD                                      ║
╠══════════════════════════════════════════════════════════════════════════════════════════════╣
║                                                                                            ║
║  Category              tradeApp  Sensibull  Opstra  Tradetron  AlgoTest  QuantsApp  Stock-  Streak ║
║                                                                                   Mock          ║
║  ──────────────────    ────────  ─────────  ──────  ─────────  ────────  ─────────  ──────  ───── ║
║  Signal Generation        9         3         5        2          8         0         0       0   ║
║  Strategy Selection       8         7         6        5          5         2         4       3   ║
║  Position Sizing          9         2         1        3          8         0         1       1   ║
║  Portfolio Risk Mgmt      9         1         1        3          2         0         0       0   ║
║  Dynamic Hedging         10         0         0        4          0         0         1       0   ║
║  Execution                6         8         0        9          7         0         0       7   ║
║  Backtesting              5         0         4        6         10         1         7       2   ║
║  Scanning                 7         7         6        3          6         8         2       4   ║
║  OI / Flow Analysis       0         7         7        2          3         9         0       0   ║
║  UI / Visualization       4         9         7        4          7         5         5       6   ║
║  ──────────────────    ────────  ─────────  ──────  ─────────  ────────  ─────────  ──────  ───── ║
║  TOTAL (/100)            67        44        37       41         56        25        20      23   ║
║                                                                                            ║
╠══════════════════════════════════════════════════════════════════════════════════════════════╣
║                                                                                            ║
║  RANKING:                                                                                  ║
║    #1  tradeApp    67/100  — Decision engine + risk management leader                      ║
║    #2  AlgoTest    56/100  — Backtesting + volatility analytics leader                     ║
║    #3  Sensibull   44/100  — Visualization + execution leader                              ║
║    #4  Tradetron   41/100  — Automation breadth leader                                     ║
║    #5  Opstra      37/100  — IV/OI analytics for manual traders                            ║
║    #6  QuantsApp   25/100  — OI + order flow specialist                                    ║
║    #7  Streak      23/100  — Technical strategy automation                                 ║
║    #8  StockMock   20/100  — Time-based option selling backtester                          ║
║                                                                                            ║
╚══════════════════════════════════════════════════════════════════════════════════════════════╝
```

---

### What No One Else Has (tradeApp Unique Features)

```
╔══════════════════════════════════════════════════════════════════════════╗
║              tradeApp — FEATURES UNIQUE IN INDIAN MARKET              ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                        ║
║  These features exist in ZERO other Indian option selling platforms:   ║
║                                                                        ║
║  1. VRP GATE with tiered thresholds (index vs stock)                  ║
║     → Nobody else auto-filters entry by IV richness                   ║
║                                                                        ║
║  2. Skew-driven strategy routing                                       ║
║     → Put skew > 1.3? → Put Calendar. Nobody else does this.         ║
║                                                                        ║
║  3. VIX-regime adaptation (low-vol protocol)                           ║
║     → VIX < 12? Auto-widen to 12Δ + tail hedge. Nobody else.         ║
║                                                                        ║
║  4. Priority-cascaded hedging engine (9 levels, 10 outcomes)          ║
║     → Evaluated in strict order, first match wins. Nobody else.       ║
║                                                                        ║
║  5. EWMA-smoothed delta trigger with hysteresis                       ║
║     → Fire at 30Δ, reset at 20Δ, 5-tick EWMA. Nobody else.           ║
║                                                                        ║
║  6. Impulsive vs. gradual move classification                          ║
║     → Price + volume + time → CLOSE_ALL vs ROLL. Nobody else.         ║
║                                                                        ║
║  7. Vanna close (IV-crush detection)                                   ║
║     → IV dropped 20% + spot stable → early exit. Nobody else.         ║
║                                                                        ║
║  8. Untested side roll with 2-condition gate                          ║
║     → Only tighten when challenged side is safe AND IV recovered.     ║
║     → Nobody else has this safety gate.                               ║
║                                                                        ║
║  9. Beta-weighted portfolio delta (NIFTY-normalized per Cr AUM)       ║
║     → Apples-to-apples delta across stocks. Nobody else.              ║
║                                                                        ║
║  10. Volga-corrected VIX stress test                                   ║
║      → Adds 15% convexity correction per VIX doubling. Nobody else.  ║
║                                                                        ║
║  11. Sector concentration limit (GICS-based)                          ║
║      → Max 5 positions per sector. Nobody else tracks this.           ║
║                                                                        ║
║  12. Circuit breaker with auto-tightening                             ║
║      → 4% monthly drawdown → positions halved, gates tightened.      ║
║      → Nobody else has portfolio-level drawdown protection.           ║
║                                                                        ║
║  13. Slippage controller with edge validation                          ║
║      → 4-step chase + abandon > 1% + reject if credit < 2× txn.     ║
║      → Nobody else validates trade edge before execution.             ║
║                                                                        ║
║  14. IV/RV Ratio as a gate condition                                   ║
║      → Relative richness metric. Only tradeApp uses this.            ║
║                                                                        ║
╚══════════════════════════════════════════════════════════════════════════╝
```

---

### What tradeApp Should Learn From Others

```
╔══════════════════════════════════════════════════════════════════════════╗
║              IMPROVEMENT ROADMAP (Lessons From Industry)              ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                        ║
║  FROM AlgoTest (Priority: High)                                       ║
║  ───────────────────────────────                                       ║
║  • Multiple RV estimators (Parkinson, Garman-Klass, Yang-Zhang)       ║
║    tradeApp uses 1 (close-to-close 30-day). Adding Parkinson          ║
║    (intraday range) would improve VRP gate accuracy.                  ║
║  • Monte Carlo drawdown simulation for backtester                     ║
║    Run 10K random trade orders to estimate worst-case drawdown.       ║
║  • In-sample / out-of-sample split for backtest validation            ║
║  • Parameter optimizer with heatmap visualization                     ║
║  • IVP/IVR with constant maturity normalization (30-day)              ║
║    Normalizes IVP across different DTEs for fair comparison.          ║
║  • Kelly Criterion display in position sizing UI                      ║
║  • VRP distribution analysis (frequency of +/- VRP by IV regime)      ║
║                                                                        ║
║  FROM Tradetron (Priority: Medium)                                    ║
║  ─────────────────────────────────                                     ║
║  • Multi-broker support (at least Angel One + Upstox)                 ║
║  • Paper trading mode with virtual capital                             ║
║  • Webhook/API triggers for external signal integration               ║
║  • WhatsApp/SMS alert notifications                                    ║
║                                                                        ║
║  FROM Sensibull (Priority: Medium)                                    ║
║  ─────────────────────────────────                                     ║
║  • Interactive payoff diagram with SD bands                           ║
║  • Order slicing for exchange freeze quantity compliance               ║
║  • Probability of Profit (POP) display per strategy                   ║
║  • Draft/paper portfolios for real-time testing without risk          ║
║                                                                        ║
║  FROM QuantsApp (Priority: Low)                                       ║
║  ────────────────────────────────                                      ║
║  • OI buildup analysis as a signal confirmation layer                 ║
║    (long buildup + high VRP = stronger signal)                        ║
║  • Order flow analytics (buyer/seller initiative)                     ║
║  • Max Pain overlay on payoff charts                                   ║
║                                                                        ║
║  FROM Opstra (Priority: Low)                                          ║
║  ───────────────────────────                                           ║
║  • OI bubble chart (futures price change vs OI change)                ║
║  • Straddle/strangle premium charts (track premiums over time)        ║
║  • Results/earnings calendar for event-aware strategy selection       ║
║                                                                        ║
║  FROM StockMock (Priority: Low)                                       ║
║  ────────────────────────────                                          ║
║  • Time-based entry rules in backtester (enter at 09:20 IST)         ║
║  • Combined premium-based SL/target for multi-leg strategies          ║
║  • Shift rules: auto-move strike when one leg hits SL                 ║
║                                                                        ║
╚══════════════════════════════════════════════════════════════════════════╝
```

---

### Final Verdict

```
╔══════════════════════════════════════════════════════════════════════════╗
║                         FINAL VERDICT                                  ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                        ║
║  The Indian options ecosystem has many tools, but they fall into       ║
║  three categories:                                                     ║
║                                                                        ║
║  ┌─────────────────────────────────────────────────────────────────┐  ║
║  │  CATEGORY 1: "Show Me" (Visualization)                         │  ║
║  │  Sensibull, Opstra, QuantsApp                                  │  ║
║  │  → Show payoffs, OI, Greeks. User decides what to do.          │  ║
║  └─────────────────────────────────────────────────────────────────┘  ║
║                                                                        ║
║  ┌─────────────────────────────────────────────────────────────────┐  ║
║  │  CATEGORY 2: "Let Me Build" (Automation Platforms)             │  ║
║  │  Tradetron, Streak, AlgoTest                                   │  ║
║  │  → Provide building blocks. User builds custom logic.          │  ║
║  └─────────────────────────────────────────────────────────────────┘  ║
║                                                                        ║
║  ┌─────────────────────────────────────────────────────────────────┐  ║
║  │  CATEGORY 3: "Tell Me What To Do" (Decision Engines)           │  ║
║  │  tradeApp (ONLY)                                               │  ║
║  │  → Scans market, generates signals, selects strategy,          │  ║
║  │    sizes position, checks portfolio risk, executes trade,      │  ║
║  │    monitors in real-time, detects threats, rolls/hedges,       │  ║
║  │    manages drawdown — all systematically.                      │  ║
║  └─────────────────────────────────────────────────────────────────┘  ║
║                                                                        ║
║  tradeApp is the only Category 3 platform in India.                   ║
║                                                                        ║
║  The closest competitor to any SINGLE component:                      ║
║    Signal gen    → AlgoTest (VRP tool, but no automation)             ║
║    Hedging       → Tradetron (keywords exist, but no pre-built        ║
║                    engine; user must build from scratch)               ║
║    Risk mgmt     → Nobody even comes close                            ║
║    Backtesting   → AlgoTest (superior statistical rigor)              ║
║    Execution     → Tradetron (100+ brokers)                           ║
║                                                                        ║
║  No single platform combines all five. tradeApp does.                 ║
║                                                                        ║
╚══════════════════════════════════════════════════════════════════════════╝
```

---

## 9. Trade Engine Comparison — tradeApp vs India's Top Option Selling Execution Engines

Section 8 compared analytics platforms. This section compares **actual trade engines** — systems that automatically generate signals, execute trades, adjust positions, and manage risk in live markets.

### The 6 Real Trade Engines in India

```
╔══════════════════════════════════════════════════════════════════════════════╗
║              INDIA'S OPTION SELLING TRADE ENGINES                         ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                            ║
║  Engine          Users      Model               Status                     ║
║  ──────          ─────      ─────               ──────                     ║
║  tradeApp        Private    Self-hosted VRP      Active                    ║
║                             decision engine                                ║
║                                                                            ║
║  SquareOff       Pioneer    Pre-built auto       PAUSED (SEBI Feb 2025    ║
║                             strategies           algo vendor compliance)   ║
║                                                                            ║
║  Tradetron       405K+      No-code algo         Active                    ║
║                             builder + marketplace                          ║
║                                                                            ║
║  Quantman        143K+      40+ indicator        Active                    ║
║                             driven algo builder                            ║
║                                                                            ║
║  Stratzy         400K+      SEBI-registered      Active                    ║
║                  (claimed)  algo marketplace                               ║
║                             INH000009180                                   ║
║                                                                            ║
║  KEEV            Unknown    Fully automated       Active (limited info)    ║
║                             option selling                                 ║
║                                                                            ║
║  Notable: Indian PMS/AIF funds also run systematic option selling          ║
║  engines (15-25% CAGR target, 5-15% max drawdown), but these are         ║
║  not publicly accessible retail platforms.                                 ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

### Engine Architecture Comparison

```
╔══════════════════════════════════════════════════════════════════════════════╗
║              ENGINE ARCHITECTURE — HOW EACH SYSTEM WORKS                  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                            ║
║  ┌─────────────────────────────────────────────────────────────────────┐  ║
║  │  tradeApp — INTEGRATED DECISION ENGINE                             │  ║
║  │                                                                     │  ║
║  │  Scanner ─▶ VRP Gate ─▶ Strategy Selector ─▶ Strike Finder ─▶      │  ║
║  │  Sizing ─▶ Portfolio Risk ─▶ Execute ─▶ Monitor ─▶ Hedge/Roll      │  ║
║  │                                                                     │  ║
║  │  UNIQUE: Every step is connected. VRP gate feeds strategy           │  ║
║  │  selection which feeds strike finder which feeds sizing which       │  ║
║  │  feeds risk checks. ONE integrated pipeline.                        │  ║
║  └─────────────────────────────────────────────────────────────────────┘  ║
║                                                                            ║
║  ┌─────────────────────────────────────────────────────────────────────┐  ║
║  │  SquareOff — PRE-BUILT STRATEGY EXECUTOR                          │  ║
║  │                                                                     │  ║
║  │  Timer ─▶ ATM Strike ─▶ Execute ─▶ Monitor ─▶ Shift/Re-center     │  ║
║  │                                                                     │  ║
║  │  Fixed time entry (9:20 AM). ATM or ATM±N strike selection.        │  ║
║  │  Real-time adjustment: shift losing leg, add wings, re-center.     │  ║
║  │  NO IV analysis, NO VRP gate, NO portfolio risk checks.            │  ║
║  │  STATUS: Paused for SEBI algo vendor compliance.                   │  ║
║  └─────────────────────────────────────────────────────────────────────┘  ║
║                                                                            ║
║  ┌─────────────────────────────────────────────────────────────────────┐  ║
║  │  Tradetron — USER-BUILT ALGO (Most Popular Marketplace Engines)    │  ║
║  │                                                                     │  ║
║  │  Timer/Condition ─▶ ATM Strike ─▶ Execute ─▶ SL/TSL ─▶ Re-enter   │  ║
║  │                                                                     │  ║
║  │  Users build via no-code blocks + 200 keywords.                    │  ║
║  │  Top marketplace strategies (MS Wealth Booster: 901 deployments):  │  ║
║  │    - Weekly expiry-to-expiry straddle/strangle selling             │  ║
║  │    - MTM SL of Rs 9,000 (3% of capital)                            │  ║
║  │    - 80-90% margin utilization                                      │  ║
║  │    - NO IV analysis, VRP gating, or Greeks-based adjustments       │  ║
║  │    - Black box (logic not disclosed to subscribers)                │  ║
║  └─────────────────────────────────────────────────────────────────────┘  ║
║                                                                            ║
║  ┌─────────────────────────────────────────────────────────────────────┐  ║
║  │  Quantman — INDICATOR-DRIVEN ALGO BUILDER                         │  ║
║  │                                                                     │  ║
║  │  Indicator Signal ─▶ Premium/Delta Strike ─▶ Execute ─▶            │  ║
║  │  SL/TSL/Move-to-Cost ─▶ Re-enter/Adjust                           │  ║
║  │                                                                     │  ║
║  │  40+ technical indicators (SuperTrend, RSI, BB, ADX, etc.)         │  ║
║  │  Premium-based AND delta-based strike selection (unique)            │  ║
║  │  OI-based signals (TrendingOI, MaxOI strike)                       │  ║
║  │  First-class adjustment engine with typed execution                 │  ║
║  │  50+ broker integrations                                            │  ║
║  │  But: NO IV percentile, NO VRP, NO portfolio-level risk gates      │  ║
║  └─────────────────────────────────────────────────────────────────────┘  ║
║                                                                            ║
║  ┌─────────────────────────────────────────────────────────────────────┐  ║
║  │  Stratzy — ALPHA-SIGNAL ALGO MARKETPLACE (SEBI Registered)        │  ║
║  │                                                                     │  ║
║  │  Alpha Signal ─▶ Strike Select ─▶ Execute ─▶ Multi-layer SL/TSL   │  ║
║  │                                                                     │  ║
║  │  80+ pre-built strategies across selling + buying                   │  ║
║  │  Entry signals (most sophisticated in market):                     │  ║
║  │    • Correlation compression (375-min rolling, alpha > 0.8)        │  ║
║  │    • IV skew (ATM vs OTM IV ratio, dual-alpha gate)                │  ║
║  │    • Vega term structure (proprietary alpha7 indicator)             │  ║
║  │    • Premium-zone entry (alpha < 0.2 for cheap options)            │  ║
║  │  Risk: extreme drawdowns visible (-78% in 1 month on some)        │  ║
║  │  NO portfolio-level risk management across strategies              │  ║
║  └─────────────────────────────────────────────────────────────────────┘  ║
║                                                                            ║
║  ┌─────────────────────────────────────────────────────────────────────┐  ║
║  │  KEEV — SIMPLE AUTO-EXECUTION                                     │  ║
║  │                                                                     │  ║
║  │  Timer ─▶ ATM Strike ─▶ Execute ─▶ SL ─▶ Exit                     │  ║
║  │                                                                     │  ║
║  │  Fully automated, minimal public documentation.                    │  ║
║  │  Targets simplicity over sophistication.                           │  ║
║  └─────────────────────────────────────────────────────────────────────┘  ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

### Head-to-Head: 25 Engine Capabilities

```
╔════════════════════════════════════════════════════════════════════════════════════╗
║  ENGINE CAPABILITY          tradeApp  SquareOff  Tradetron  Quantman  Stratzy    ║
╠════════════════════════════════════════════════════════════════════════════════════╣
║                                                                                  ║
║  ── ENTRY INTELLIGENCE ────────────────────────────────────────────────────────  ║
║  IV-RV Spread analysis       ✅        ❌          ~          ❌        ~         ║
║  IV Percentile gate          ✅        ❌          ❌         ❌        ❌        ║
║  IV/RV Ratio gate            ✅        ❌          ❌         ❌        ❌        ║
║  Put/Call skew routing       ✅        ❌          ❌         ❌        ~         ║
║  VIX-regime adaptation       ✅        ❌          ~          ❌        ❌        ║
║  Delta-based strike select   ✅        ❌          ❌         ✅        ❌        ║
║  Premium-based strike select ❌        ❌          ~          ✅        ✅        ║
║  Correlation-based entry     ❌        ❌          ❌         ❌        ✅        ║
║  40+ technical indicators    ❌        ❌          ✅         ✅        ❌        ║
║  OI-based signals            ❌        ❌          ~          ✅        ❌        ║
║                                                                                  ║
║  ── POSITION MANAGEMENT ───────────────────────────────────────────────────────  ║
║  Auto delta monitoring       ✅        ❌          ~          ❌        ❌        ║
║  EWMA smoothing              ✅        ❌          ❌         ❌        ❌        ║
║  Hysteresis (fire/reset gap) ✅        ❌          ❌         ❌        ❌        ║
║  Impulsive move detection    ✅        ❌          ❌         ❌        ❌        ║
║  Roll challenged leg         ✅        ✅          ~          ~         ❌        ║
║  Untested side tighten       ✅        ❌          ❌         ❌        ❌        ║
║  Vanna close (IV crush)      ✅        ❌          ❌         ❌        ❌        ║
║  Profit target auto-close    ✅        ~           ✅         ✅        ✅        ║
║  Gamma exit (DTE-based)      ✅        ❌          ~          ❌        ❌        ║
║  15-min cooldown             ✅        ❌          ❌         ❌        ❌        ║
║  Trailing stop loss          ~         ~           ✅         ✅        ✅        ║
║  Move SL to cost             ❌        ❌          ❌         ✅        ❌        ║
║  Re-entry after SL           ❌        ❌          ✅         ✅        ❌        ║
║  Shift strike on SL          ❌        ✅          ~          ✅        ❌        ║
║                                                                                  ║
║  ── PORTFOLIO RISK ────────────────────────────────────────────────────────────  ║
║  Beta-weighted delta         ✅        ❌          ❌         ❌        ❌        ║
║  Vega stress test            ✅        ❌          ❌         ❌        ❌        ║
║  Sector concentration cap    ✅        ❌          ❌         ❌        ❌        ║
║  Correlation constraint      ✅        ❌          ❌         ❌        ❌        ║
║  Circuit breaker (4% DD)     ✅        ❌          ❌         ❌        ❌        ║
║  Position count limit        ✅        ❌          ❌         ❌        ❌        ║
║  Risk-based sizing (2% AUM)  ✅        ❌          ❌         ❌        ❌        ║
║                                                                                  ║
║  ── EXECUTION ─────────────────────────────────────────────────────────────────  ║
║  Slippage controller         ✅        ❌          ❌         ❌        ❌        ║
║  Edge validation (2× txn)    ✅        ❌          ❌         ❌        ❌        ║
║  SEBI txn cost engine        ✅        ❌          ❌         ❌        ❌        ║
║  Multi-broker support        ❌        ~(2)        ✅(100+)   ✅(50+)   ~         ║
║  Fully automated execution   ✅        ✅(paused)  ✅         ✅        ✅        ║
║                                                                                  ║
║  Legend:  ✅ = Yes    ~ = Partial/Basic    ❌ = Not available                    ║
╚════════════════════════════════════════════════════════════════════════════════════╝
```

---

### Engine Depth Comparison

```
╔══════════════════════════════════════════════════════════════════════════╗
║              WHAT HAPPENS WHEN RELIANCE DROPS 5%?                      ║
║              (Same scenario, different engine responses)                ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                        ║
║  ┌──────────────────────────────────────────────────────────────────┐ ║
║  │  tradeApp                                                        │ ║
║  │  ────────                                                        │ ║
║  │  1. EWMA delta computed: 0.34 (smoothed, confirmed)              │ ║
║  │  2. Hysteresis: 0.34 > 0.30 trigger, 0.34 > 0.20 reset → FIRE  │ ║
║  │  3. Classify: 0.74% in 5 min, normal volume → GRADUAL           │ ║
║  │  4. Action: ROLL_CHALLENGED (not CLOSE_ALL)                      │ ║
║  │  5. Roll short put to 16Δ in next expiry                         │ ║
║  │  6. Set 15-min cooldown (prevent whipsaw)                        │ ║
║  │  7. After cooldown: check untested side conditions               │ ║
║  │  8. If both conditions met → ROLL_UNTESTED (tighten call)        │ ║
║  │  9. Continue monitoring until gamma exit at DTE ≤ 14             │ ║
║  │  Result: Position preserved, ended +18.6% on risk                │ ║
║  └──────────────────────────────────────────────────────────────────┘ ║
║                                                                        ║
║  ┌──────────────────────────────────────────────────────────────────┐ ║
║  │  SquareOff                                                       │ ║
║  │  ─────────                                                       │ ║
║  │  1. Short put premium doubles (or MTM SL hits)                   │ ║
║  │  2. Action: Close losing leg (or entire position)                │ ║
║  │  3. Next day: re-enter fresh straddle at new ATM                 │ ║
║  │  Result: Realized full loss on the leg; no recovery attempt      │ ║
║  └──────────────────────────────────────────────────────────────────┘ ║
║                                                                        ║
║  ┌──────────────────────────────────────────────────────────────────┐ ║
║  │  Tradetron (Top Marketplace Strategy)                            │ ║
║  │  ─────────                                                       │ ║
║  │  1. MTM loss hits Rs 9,000 (3% of capital)                       │ ║
║  │  2. Action: Exit entire position                                 │ ║
║  │  3. Wait for next weekly expiry cycle to re-enter                │ ║
║  │  4. No roll, no adjustment, no recovery                          │ ║
║  │  Result: Mechanical stop + full loss realized                    │ ║
║  └──────────────────────────────────────────────────────────────────┘ ║
║                                                                        ║
║  ┌──────────────────────────────────────────────────────────────────┐ ║
║  │  Quantman                                                        │ ║
║  │  ─────────                                                       │ ║
║  │  1. Per-leg SL triggers (% of premium or fixed points)           │ ║
║  │  2. Action options depends on user config:                       │ ║
║  │     a. Close leg + re-enter at new strike (shift rule)           │ ║
║  │     b. Close leg + exit other leg too                            │ ║
║  │     c. Close all + move SL to cost                               │ ║
║  │  3. No delta analysis, no smoothing, no cooldown                 │ ║
║  │  Result: Rule-based SL action; quality depends on user config    │ ║
║  └──────────────────────────────────────────────────────────────────┘ ║
║                                                                        ║
║  ┌──────────────────────────────────────────────────────────────────┐ ║
║  │  Stratzy                                                         │ ║
║  │  ─────────                                                       │ ║
║  │  1. Per-leg loss limit (Rs 5,000-10,000) triggers                │ ║
║  │  2. Close the losing leg                                         │ ║
║  │  3. Widen SL on remaining leg to Rs 10,000                       │ ║
║  │  4. Or: strategy-level 2-3% capital SL triggers full exit        │ ║
║  │  5. No roll, no delta monitoring, no IV-aware decisions          │ ║
║  │  Result: Multi-layer SL but still binary (hold or exit)          │ ║
║  │  Published drawdown: up to -78% in a single month on some        │ ║
║  │  strategies                                                       │ ║
║  └──────────────────────────────────────────────────────────────────┘ ║
║                                                                        ║
║  KEY DIFFERENCE: tradeApp is the only engine that ADAPTS.             ║
║  Others have binary logic: hold until SL → exit → re-enter later.    ║
║  tradeApp: smooth → classify → roll/close → cooldown → tighten →     ║
║  gamma exit. A 9-step decision cascade vs a 2-step if/else.          ║
╚══════════════════════════════════════════════════════════════════════════╝
```

---

### Risk Management Depth

```
╔══════════════════════════════════════════════════════════════════════════╗
║              RISK MANAGEMENT — LAYER COMPARISON                        ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                        ║
║  Layer                    tradeApp   Others (Best Available)           ║
║  ─────                    ────────   ─────────────────────             ║
║                                                                        ║
║  L1: Per-leg SL               ~      ✅ Quantman (% or pts)           ║
║      tradeApp uses delta-based triggers instead of fixed SL.          ║
║      Others use premium-based SL (simpler but less intelligent).      ║
║                                                                        ║
║  L2: Strategy-level SL        ~      ✅ Tradetron (MTM % or fixed)    ║
║      tradeApp uses profit target (40%) and gamma exit (DTE≤14).       ║
║      Others use MTM-based all-or-nothing SL.                          ║
║                                                                        ║
║  L3: Trailing SL              ❌     ✅ Quantman, Stratzy, Tradetron  ║
║      tradeApp does not have trailing SL — it uses profit target +     ║
║      vanna close instead. Gap: trailing SL would add value.           ║
║                                                                        ║
║  L4: Greeks-based triggers    ✅     ~ Tradetron (Net Delta keyword)  ║
║      Only tradeApp uses EWMA-smoothed delta with hysteresis.          ║
║      Tradetron has raw Net Delta but no smoothing/classification.     ║
║                                                                        ║
║  L5: Impulsive classification ✅     ❌ Nobody                         ║
║      Price + volume + time → CLOSE_ALL vs ROLL_CHALLENGED.           ║
║                                                                        ║
║  L6: Anti-whipsaw (cooldown)  ✅     ❌ Nobody                         ║
║      15-min Redis lock prevents over-trading during volatility.       ║
║                                                                        ║
║  L7: Portfolio-level limits   ✅     ❌ Nobody                         ║
║      25 position cap, 5/sector, beta-weighted delta, vega stress.     ║
║      All others manage risk per-strategy only, not across portfolio.  ║
║                                                                        ║
║  L8: Circuit breaker          ✅     ❌ Nobody                         ║
║      4% monthly drawdown → halve positions, tighten gates.           ║
║      Others have daily MTM limits at best.                             ║
║                                                                        ║
║  L9: Slippage/edge control    ✅     ❌ Nobody                         ║
║      Others use market orders or basic limits with no edge check.     ║
║                                                                        ║
║  ──────────────────────────────────────────────────────────────────    ║
║                                                                        ║
║  tradeApp: 8 of 9 layers (missing trailing SL)                        ║
║  Quantman: 3 of 9 (L1 + L2 + L3) — best among alternatives          ║
║  Tradetron: 2 of 9 (L2 + partial L4)                                  ║
║  Stratzy: 2 of 9 (L1 + L2)                                            ║
║  SquareOff: 2 of 9 (L1 + L2)                                          ║
║  KEEV: 1 of 9 (L1)                                                     ║
╚══════════════════════════════════════════════════════════════════════════╝
```

---

### Trade Engine Scorecard

```
╔══════════════════════════════════════════════════════════════════════════╗
║              TRADE ENGINE SCORECARD (Execution Engines Only)           ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                        ║
║  Dimension (weight)     tradeApp  SquareOff  Tradetron  Quantman  Stratzy ║
║  ─────────────────      ────────  ─────────  ─────────  ────────  ─────── ║
║  Entry Intelligence       9         2          3          6         7    ║
║  (25%)                                                                   ║
║                                                                        ║
║  Position Management     10         5          3          5         3    ║
║  (25%)                                                                   ║
║                                                                        ║
║  Portfolio Risk Mgmt      10        0          1          0         0    ║
║  (20%)                                                                   ║
║                                                                        ║
║  Execution Quality        7         4          5          5         4    ║
║  (10%)                                                                   ║
║                                                                        ║
║  Broker Coverage          3         2          10         8         4    ║
║  (10%)                                                                   ║
║                                                                        ║
║  Transparency/Control     9         3          3          7         6    ║
║  (10%)                                                                   ║
║  ─────────────────      ────────  ─────────  ─────────  ────────  ─────── ║
║                                                                        ║
║  WEIGHTED TOTAL           8.9       2.8        3.5        4.8      4.0  ║
║  (/10)                                                                   ║
║                                                                        ║
║  RANKING:                                                              ║
║    #1  tradeApp    8.9/10  — Only true decision engine                 ║
║    #2  Quantman    4.8/10  — Best entry logic + adjustment among       ║
║                              retail engines                             ║
║    #3  Stratzy     4.0/10  — Most diverse alpha signals, but           ║
║                              extreme tail risk (-78% months)            ║
║    #4  Tradetron   3.5/10  — Best platform infrastructure, but         ║
║                              marketplace strategies are simplistic      ║
║    #5  SquareOff   2.8/10  — Pioneer, now paused; adjustment           ║
║                              engine was ahead of its time               ║
║                                                                        ║
╚══════════════════════════════════════════════════════════════════════════╝
```

---

### The Institutional Gap — What Separates tradeApp From Retail Engines

```
╔══════════════════════════════════════════════════════════════════════════╗
║          THE INSTITUTIONAL GAP                                         ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                        ║
║  Indian retail option selling engines (SquareOff, Tradetron,          ║
║  Quantman, Stratzy) share these fundamental limitations:              ║
║                                                                        ║
║  1. ENTRY: Time-based (sell at 9:20 AM at ATM)                        ║
║     tradeApp: Vol-surface-based (sell when IV is objectively rich)     ║
║     → Retail sells every day. tradeApp sells only when there's edge.  ║
║                                                                        ║
║  2. STRIKES: Fixed offset from ATM (ATM±2 or closest premium)        ║
║     tradeApp: Delta-based (16Δ adapts to IV environment)              ║
║     → In high IV, 16Δ is further OTM. In low IV, it's closer.        ║
║     Retail uses same distance regardless of vol.                      ║
║                                                                        ║
║  3. ADJUSTMENT: Binary (hold until SL → exit → re-enter next day)    ║
║     tradeApp: 10 outcomes (roll, close, roll untested, vanna close,   ║
║     gamma exit, etc.) with smoothing + cooldown + classification      ║
║     → Retail has 2 outcomes (hold or exit). tradeApp has 10.          ║
║                                                                        ║
║  4. RISK: Per-strategy SL only (Rs 9,000 or 3% of capital)           ║
║     tradeApp: 7 portfolio-level gates (delta, vega, sector,           ║
║     correlation, position count, drawdown, circuit breaker)            ║
║     → Retail manages risk per-trade. tradeApp manages risk across     ║
║     the entire portfolio.                                              ║
║                                                                        ║
║  5. SIZING: Fixed lots (1x, 2x, 3x multiplier)                       ║
║     tradeApp: Risk-budget-based (2% AUM / max_loss, margin-capped,   ║
║     premium-checked, 500-lot hard cap)                                 ║
║     → Retail bets the same size every time. tradeApp sizes based on   ║
║     the specific risk of each trade.                                   ║
║                                                                        ║
║  6. UNIVERSE: NIFTY + BANKNIFTY only (all retail engines)             ║
║     tradeApp: 4 indices + 103 stocks (full NIFTY100)                  ║
║     → Retail sells the same 2 underlyings as everyone else.           ║
║     tradeApp finds edge wherever IV is rich across 107 symbols.       ║
║                                                                        ║
║  7. DRAWDOWN: Stratzy published -78% single month; Tradetron's       ║
║     top strategy shows 18% drawdown; no circuit breaker on any.       ║
║     tradeApp: 4% monthly drawdown → auto-tighten all gates.          ║
║     → Retail engines can blow up. tradeApp auto-throttles.            ║
║                                                                        ║
║  ══════════════════════════════════════════════════════════════════     ║
║                                                                        ║
║  SUMMARY: The gap is not incremental. It is categorical.              ║
║                                                                        ║
║  Retail engines answer: "How do I sell options automatically?"        ║
║  tradeApp answers: "When should I sell, what should I sell,           ║
║  how much should I sell, and what do I do when it goes wrong?"        ║
║                                                                        ║
║  This is the difference between an auto-trader and a trading system.  ║
║                                                                        ║
╚══════════════════════════════════════════════════════════════════════════╝
```

---

### What tradeApp Should Adopt From Trade Engines

```
╔══════════════════════════════════════════════════════════════════════════╗
║              FEATURES TO ADOPT FROM TRADE ENGINES                      ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                        ║
║  FROM Quantman (Priority: High)                                       ║
║  ──────────────────────────────                                        ║
║  • Premium-based strike selection as an alternative to delta-based    ║
║    (useful when IV data is stale but live premiums are available)     ║
║  • Move-SL-to-cost: after rolling challenged leg, set floor at       ║
║    entry credit to lock in breakeven                                  ║
║  • Re-entry after SL: if a position is stopped out but VRP gate      ║
║    still passes, allow re-entry at new strikes                        ║
║  • Shift rules: auto-move strike to next OTM when SL hits           ║
║    (complements the existing ROLL_CHALLENGED action)                  ║
║                                                                        ║
║  FROM Tradetron (Priority: Medium)                                    ║
║  ─────────────────────────────────                                     ║
║  • Strategy-level trailing stop loss on aggregate MTM                 ║
║    (tradeApp has profit target at 40% but no trailing mechanism)      ║
║  • Delta Neutral quantity keyword concept: compute exact lot count    ║
║    needed to zero net delta and auto-hedge with futures               ║
║  • WhatsApp/SMS/Phone call alerts on adjustment actions               ║
║  • Webhook API for TradingView or external signal integration        ║
║                                                                        ║
║  FROM Stratzy (Priority: Medium)                                      ║
║  ────────────────────────────────                                      ║
║  • Multi-alpha signal gating: require 2+ independent alpha signals   ║
║    to align before entry (reduces false signals)                      ║
║  • Correlation compression signal as an additional VRP gate check    ║
║  • Per-leg capital limit (Rs 5K-10K) as a micro-level risk floor     ║
║  • Trading hour restriction (10:15-14:15) to avoid                   ║
║    opening/closing volatility — could be a configurable scanner      ║
║    parameter                                                          ║
║                                                                        ║
║  FROM SquareOff (Priority: Low — Platform Paused)                    ║
║  ────────────────────────────────                                      ║
║  • Straddle-to-iron-fly conversion: when delta breaches, add wings   ║
║    to convert naked to defined-risk mid-trade                         ║
║  • Re-center logic: if underlying moves far enough, close both       ║
║    sides and re-open fresh straddle at new ATM                        ║
║                                                                        ║
║  FROM Indian PMS/AIF Best Practices (Priority: High)                 ║
║  ──────────────────────────────────                                    ║
║  • VIX-based position scaling (full at VIX<15, half at 15-20,        ║
║    quarter at 20-25, flat above 25) — partially implemented via      ║
║    low_vol_protocol but not as a scaling factor                       ║
║  • Event calendar integration (earnings, RBI, budget) for            ║
║    auto-reducing exposure before known events                         ║
║  • STT trap prevention: auto-close ITM shorts before 3 PM on        ║
║    expiry day (critical for Indian market)                            ║
║  • 50% profit close rule (tastylive consensus) — tradeApp uses      ║
║    40%, consider making configurable                                  ║
║                                                                        ║
╚══════════════════════════════════════════════════════════════════════════╝
```
