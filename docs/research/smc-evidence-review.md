# SMC / ICT Trading — Evidence Review (Slice 2 Step 2.1 input)

> **Date:** 2026-06-13 · **Method:** deep-research harness (104 agents, 22 primary sources
> fetched, 25 falsifiable claims extracted and adversarially verified by 3-vote; 21 confirmed,
> 4 killed). **Purpose:** ground the Step 2.1 SMC analyzer design in what is actually
> substantiated, not in marketing. Companion to the script critique in the Slice 2 thread.
>
> **Bottom line:** the evidence supports SMC's underlying microstructure *building blocks*
> far more than it supports SMC as a packaged predictive methodology — and **almost none of
> the evidence is in crypto.** Where edges exist they are *small*, *regime-dependent*, and
> *FX/equities, not BTC/ETH perps*. In crypto specifically, the validated signal lives in
> **derivatives positioning (carry/funding/OI), not price patterns.**

---

## 1. Evidence tiers

### 🟢 Substantiated (but mostly FX/equities, not crypto)

| Finding | Strength | Source |
|---|---|---|
| **Round-number clustering** of S/R and of stop/limit orders is massive and real (>70% of published S/R end in 0; 96% in 0/5; orders reject uniform null at AD=74.6 ≫ crit 8.0). This is the mechanism behind SMC "liquidity" and "premium/discount." | High (FX/equities; BTC whole-dollar clustering ~18% vs ~1% noted) | Osler FRBNY EPR 2000; Osler SR125 / J.Finance 2003 |
| **S/R bounce edge:** price bounced off *published* levels 60.8% vs 56.2% off random levels (~4.6pp, sig. at 5% for 13/16 pairs). Real but **small**. | High (FX, year-2000) | Osler 2000 |
| **Stop-loss asymmetry** = the empirical analogue of "stop hunts / liquidity sweeps": stop-loss buys cluster *just above* round numbers, sells *just below*, take-profits *on* them. | High (real RBS bank order book, late-90s FX) | Osler J.IMF 2005 / SR150; SR125 |
| **Price cascades & reversals** at round numbers — continuation edge ~0.007pp over 15 min ("hours, not days"); reversal 59.3% vs 54.8%. Real but **economically tiny**, currency-dependent. | High magnitude-small (2-1 on continuation) | Osler 2005; sim Osler 2001 |
| **Disposition effect** (sell winners / hold losers) is a genuine, costly behavioral bias (~3.4% excess, p≈0.001) — validates *why* liquidity rests at obvious stop levels. | High (US equities 1987-93) | Odean J.Finance 1998 |
| **Crypto carry/basis is uniquely large** (~7% p.a. avg, >40% tails, ~10× S&P) and not explained by rates/storage. | High | BIS WP 1087 (Mgmt Sci 2024/26) |
| **Carry predicts crypto liquidations** — 10% carry rise → short-futures liquidations = 22% of OI next month. Closest thing to "liquidation cascade" evidence. **In-sample predictability, NOT after-cost tradable.** | High (in-sample) | BIS WP 1087 |
| **Funding rates are predictable as a time series** (next funding print beats no-change). | High | Inan SSRN 5576424 (2025) |

### 🟡 Thin / heavily caveated

- **Funding → price.** Funding predicts *the funding series*, **NOT spot price/reversals** (a regression of funding→next-8h return: β −0.087, R² 0.003). Influencer narratives routinely blur this. Use funding as a *crowding/regime* signal, not a directional oracle.
- **Crypto carry trade is NOT risk-free** — at 10× the futures leg would have been liquidated in >half of in-sample months; basis frictions, not fundamentals, drive it (2024 ETF natural experiment compressed basis 36-97%).
- **Funding/basis arbitrage mostly dies after costs** — only ~40% of top opportunities net-positive after fees + spread reversal (35.7M obs, 26 exchanges).

### 🔴 Absent or refuted

- **No component-level SMC evidence exists.** The *only* peer-reviewed paper coding SMC/ICT primitives (Hassan et al., Informatica 2026) is **gold (XAUUSD) H1, never crypto**, bundles all SMC structure together (no per-construct ablation), reports an *in-sample* 46.7%/Sharpe-1.18 hybrid, and concedes "predictive accuracy in isolation remains moderate." So order blocks, FVGs, BOS/CHoCH, **OTE/Fibonacci, Power-of-Three, premium/discount** have **zero** rigorous standalone validation. Absence of evidence is itself the finding.
- **Refuted in verification (do NOT cite/rely on):** VPIN predicting BTC jumps (0-3); funding-arbitrage venue-Sharpe of 23.55 DEX vs negative CEX (0-3); "fragmentation sustained by costs" framing (1-2).

### ⚠️ The data-snooping trap (the most important methodological finding)

A fake edge is nearly **automatic**: expected max in-sample Sharpe from *skill-less* strategies ≈ √(2·ln N). N=10 configs → expected IS Sharpe **1.57** with true OOS Sharpe **0**; 7 binary params (N=128) → expected max IS Sharpe **>2.6**. The paper names the exact trap our architecture risks: *"confluence scoring with many parameters and multi-timeframe confirmation inflate N and manufacture false confidence."* (Bailey, Borwein, López de Prado, Zhu — Notices of the AMS, 2014.)

---

## 2. What this means for our analyzer (design implications)

1. **Calibration over confidence.** No "4-star 95%" setups. Confidence must be *earned* by forward-tested calibration (do "70%" signals win ~70%?), never asserted by counting gates.
2. **Weight by evidence, not by SMC orthodoxy.**
   - **Highest weight / most defensible:** premium-discount + liquidity resting at *obvious* levels (round numbers, equal highs/lows, prior-day/-week H/L, swing pools) — the one SMC idea with a real microstructure mechanism.
   - **Derivatives = regime/risk filter, not direction:** funding extremes + OI kinematics + carry signal *crowding & liquidation risk*. Use to size down / veto / time, not to call direction.
   - **Demote to "context, low weight":** OTE/Fibonacci, Power-of-Three session theatre — least evidence.
3. **As-of correctness is non-negotiable** (no look-ahead/repainting — the #1 failure mode, and pervasive in the current scripts).
4. **Don't inflate N.** Pre-register the gate count and thresholds; derive thresholds from *volatility/ATR*, not hand-tuning per symbol (hand-tuning = snooping). Resist "add another gate."
5. **Forward-test is the real validation.** Backtesting discretionary SMC is snooping-prone, so the journal/Historian/Critic must enable honest *out-of-sample, forward* evaluation — the system proves itself on signals it emits going forward, tracked for calibration & deflated performance.
6. **Volatility/regime normalization** everywhere (edges are regime-dependent).

---

## 3. Highest-value validations to run (open questions)

1. Do the FX round-number / stop-cluster / cascade effects **replicate in BTC/ETH perps**? No source tests this — it's the single highest-value experiment before trusting SMC liquidity logic.
2. Does any **individual** SMC construct survive honest OOS + costs? Entirely unestablished.
3. Can the in-sample crypto derivatives signals (carry→liquidations, funding predictability) become a **net-of-cost, deflated-Sharpe-positive, forward-tested** signal — or do they just add parameters that inflate N?

---

*Full machine-readable findings (claims, confidence, vote tallies, verbatim evidence, sources)
are in the deep-research run output. Sources are primary unless noted: FRBNY, BIS, J.Finance,
J.IMF, Notices of the AMS, SSRN, MDPI Mathematics, ScienceDirect.*
