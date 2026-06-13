#!/usr/bin/env python3
"""
derivatives_data.py — Crypto Derivatives Confluence Analyzer

Analyzes:
  - Funding Rate: direction, extremes, contrarian signals
  - Open Interest Kinematics: 4-state matrix (price vs OI direction)
  - Funding cost projection for trade hold duration
  - Session timing detection (Asia/London/NY/Cooldown)

This provides Gate 4 (Derivatives Confluence) and Gate 5 (PO3 Temporal)
data for the SMC 5-gate filter system.

Usage:
    python3 derivatives_data.py <PAIR>
"""

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from binance.client import Client
except ImportError:
    Client = None

API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

# Funding rate thresholds
FR_EXTREME_POSITIVE = 0.0003  # 0.03% per 8h — overcrowded longs
FR_EXTREME_NEGATIVE = -0.0003  # -0.03% per 8h — overcrowded shorts
FR_WARNING = 0.0005  # 0.05% — funding cost warning threshold

# Sessions (UTC)
SESSIONS = {
    "asia": (0, 8, "Asian Session (Accumulation — MONITOR ONLY)"),
    "london": (8, 16, "London Session (Manipulation — HUNT for sweeps)"),
    "newyork": (13, 21, "New York Session (Distribution — EXECUTE/CONTINUE)"),
    "cooldown": (21, 24, "Global Cooldown (NO NEW ENTRIES)"),
}


def get_client():
    if Client is None:
        print("ERROR: python-binance not installed")
        sys.exit(1)
    return Client(API_KEY, API_SECRET)


def get_current_session():
    """Determine current trading session and PO3 phase."""
    now = dt.datetime.utcnow()
    hour = now.hour

    session_name = "unknown"
    session_label = "Unknown"
    po3_phase = "unknown"
    can_trade = False

    if 0 <= hour < 8:
        session_name = "asia"
        session_label = SESSIONS["asia"][2]
        po3_phase = "ACCUMULATION"
        can_trade = False
    elif 8 <= hour < 13:
        session_name = "london"
        session_label = SESSIONS["london"][2]
        po3_phase = "MANIPULATION"
        can_trade = True
    elif 13 <= hour < 21:
        session_name = "newyork"
        session_label = SESSIONS["newyork"][2]
        po3_phase = "DISTRIBUTION"
        can_trade = True
    else:
        session_name = "cooldown"
        session_label = SESSIONS["cooldown"][2]
        po3_phase = "COOLDOWN"
        can_trade = False

    # London-NY overlap (13:00-16:00 UTC) = peak volume
    is_overlap = 13 <= hour < 16

    return {
        "session": session_name,
        "label": session_label,
        "po3_phase": po3_phase,
        "can_trade": can_trade,
        "is_overlap": is_overlap,
        "utc_time": now.strftime("%Y-%m-%d %H:%M UTC"),
        "hour": hour,
    }


def analyze_funding_rate(client, pair):
    """Fetch and analyze funding rate data."""
    result = {
        "current_rate": 0,
        "current_pct": 0,
        "direction": "NEUTRAL",
        "extreme": False,
        "extreme_type": None,
        "interpretation": "",
        "history": [],
        "projected_daily_cost_pct": 0,
    }

    try:
        funding = client.futures_funding_rate(symbol=pair, limit=30)
        if not funding:
            result["interpretation"] = "No funding rate data available"
            return result

        current_fr = float(funding[-1]["fundingRate"])
        result["current_rate"] = current_fr
        result["current_pct"] = current_fr * 100
        result["projected_daily_cost_pct"] = abs(current_fr) * 3 * 100  # 3 funding periods per day

        # Direction
        if current_fr > 0:
            result["direction"] = "POSITIVE (Longs pay Shorts)"
        elif current_fr < 0:
            result["direction"] = "NEGATIVE (Shorts pay Longs)"
        else:
            result["direction"] = "NEUTRAL"

        # Extreme detection
        if current_fr > FR_EXTREME_POSITIVE:
            result["extreme"] = True
            result["extreme_type"] = "EXTREME_POSITIVE"
            result["interpretation"] = (
                "⚠️ EXTREME POSITIVE — Retail longs are massively overcrowded. "
                "High probability of a liquidation cascade to sweep sell-side liquidity. "
                "BEARISH confluence: favors SHORT setups."
            )
        elif current_fr < FR_EXTREME_NEGATIVE:
            result["extreme"] = True
            result["extreme_type"] = "EXTREME_NEGATIVE"
            result["interpretation"] = (
                "⚠️ EXTREME NEGATIVE — Retail shorts are massively overcrowded. "
                "High probability of a short squeeze to sweep buy-side liquidity. "
                "BULLISH confluence: favors LONG setups."
            )
        elif current_fr > 0.0001:
            result["interpretation"] = (
                "📈 Mildly positive — Longs dominant but not extreme. Healthy in uptrend."
            )
        elif current_fr < -0.0001:
            result["interpretation"] = (
                "📉 Mildly negative — Shorts dominant but not extreme. Healthy in downtrend."
            )
        else:
            result["interpretation"] = "⚖️ Neutral — No significant directional skew in funding."

        # History for trend analysis
        for f in funding[-10:]:
            result["history"].append(
                {
                    "rate": float(f["fundingRate"]),
                    "pct": float(f["fundingRate"]) * 100,
                    "time": dt.datetime.utcfromtimestamp(int(f["fundingTime"]) / 1000).strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                }
            )

    except Exception as e:
        result["interpretation"] = f"Error fetching funding rate: {e}"

    return result


def analyze_open_interest(client, pair):
    """Fetch and analyze Open Interest kinematics."""
    result = {
        "current_oi": 0,
        "oi_change_4h": 0,
        "oi_change_pct": 0,
        "price_change_pct": 0,
        "kinematic_state": "UNKNOWN",
        "interpretation": "",
    }

    try:
        # Current OI
        oi_data = client.futures_open_interest(symbol=pair)
        result["current_oi"] = float(oi_data["openInterest"])

        # OI history (5-minute intervals)
        oi_hist = client.futures_open_interest_hist(symbol=pair, period="5m", limit=48)

        if oi_hist and len(oi_hist) >= 2:
            oldest = float(oi_hist[0]["sumOpenInterest"])
            newest = float(oi_hist[-1]["sumOpenInterest"])
            result["oi_change_4h"] = newest - oldest
            result["oi_change_pct"] = ((newest - oldest) / oldest * 100) if oldest > 0 else 0

        # Get recent price change for kinematic state
        ticker = client.futures_ticker(symbol=pair)
        result["price_change_pct"] = float(ticker["priceChangePercent"])

        # Determine the 4-state kinematic matrix
        price_up = result["price_change_pct"] > 0
        oi_up = result["oi_change_pct"] > 1  # Use 1% threshold to filter noise

        if price_up and oi_up:
            result["kinematic_state"] = "PRICE_UP_OI_UP"
            result["interpretation"] = (
                "✅ TRUE BULLISH MOMENTUM — New capital entering long positions. "
                "Validates uptrend continuation. Bullish BOS setups are HIGH confidence."
            )
        elif price_up and not oi_up:
            result["kinematic_state"] = "PRICE_UP_OI_DOWN"
            result["interpretation"] = (
                "⚠️ SHORT SQUEEZE RALLY — Price rising but OI falling means "
                "shorts are being forced to cover, not new buyers entering. "
                "Artificial, low-conviction rally. Anticipate reversal at Premium POI."
            )
        elif not price_up and oi_up:
            result["kinematic_state"] = "PRICE_DOWN_OI_UP"
            result["interpretation"] = (
                "✅ TRUE BEARISH MOMENTUM — New capital entering short positions. "
                "Validates downtrend continuation. Bearish BOS setups are HIGH confidence."
            )
        else:  # price down, OI down
            result["kinematic_state"] = "PRICE_DOWN_OI_DOWN"
            result["interpretation"] = (
                "🔥 LONG LIQUIDATION CASCADE — Longs capitulating and being forced out. "
                "Often marks a LOCAL BOTTOM. After capitulation exhausts, "
                "seek LTF bullish CHoCH signals for reversal entries."
            )

    except Exception as e:
        result["interpretation"] = f"Error fetching OI data: {e}"

    return result


def evaluate_gate4(funding, oi, trade_direction):
    """
    Evaluate Gate 4 (Derivatives Confluence) for a proposed trade direction.
    Returns pass/fail and confidence modifier.
    """
    score = 0
    notes = []

    # Funding rate analysis
    if trade_direction == "LONG":
        if funding["extreme_type"] == "EXTREME_NEGATIVE":
            score += 2
            notes.append("✅ Extreme negative funding — overcrowded shorts = bullish catalyst")
        elif funding["current_rate"] < 0:
            score += 1
            notes.append("✅ Negative funding aligns with long entry")
        elif funding["extreme_type"] == "EXTREME_POSITIVE":
            score -= 2
            notes.append("❌ Extreme positive funding OPPOSES long — longs overcrowded")
        else:
            notes.append("⚖️ Funding neutral for longs")

    elif trade_direction == "SHORT":
        if funding["extreme_type"] == "EXTREME_POSITIVE":
            score += 2
            notes.append("✅ Extreme positive funding — overcrowded longs = bearish catalyst")
        elif funding["current_rate"] > 0:
            score += 1
            notes.append("✅ Positive funding aligns with short entry")
        elif funding["extreme_type"] == "EXTREME_NEGATIVE":
            score -= 2
            notes.append("❌ Extreme negative funding OPPOSES short — shorts overcrowded")
        else:
            notes.append("⚖️ Funding neutral for shorts")

    # OI kinematic analysis
    if trade_direction == "LONG":
        if oi["kinematic_state"] == "PRICE_DOWN_OI_DOWN":
            score += 2
            notes.append("✅ Long liquidation cascade — capitulation often marks bottom")
        elif oi["kinematic_state"] == "PRICE_UP_OI_UP":
            score += 1
            notes.append("✅ True bullish momentum confirms long bias")
        elif oi["kinematic_state"] == "PRICE_UP_OI_DOWN":
            score -= 1
            notes.append("⚠️ Short squeeze rally — caution on longs")
    elif trade_direction == "SHORT":
        if oi["kinematic_state"] == "PRICE_UP_OI_DOWN":
            score += 2
            notes.append("✅ Artificial rally (short squeeze) — reversal likely")
        elif oi["kinematic_state"] == "PRICE_DOWN_OI_UP":
            score += 1
            notes.append("✅ True bearish momentum confirms short bias")
        elif oi["kinematic_state"] == "PRICE_DOWN_OI_DOWN":
            score -= 1
            notes.append("⚠️ Capitulation phase — caution on new shorts")

    passed = score > 0
    if score >= 3:
        confidence = "STRONG"
    elif score > 0:
        confidence = "MODERATE"
    elif score == 0:
        confidence = "NEUTRAL"
    else:
        confidence = "OPPOSING"

    return {
        "passed": passed,
        "score": score,
        "confidence": confidence,
        "notes": notes,
    }


def evaluate_gate5(session_info, trade_direction, daily_open=None, current_price=None):
    """Evaluate Gate 5 (PO3 Temporal Confluence)."""
    notes = []

    if not session_info["can_trade"]:
        return {
            "passed": False,
            "notes": [f"❌ Current session: {session_info['label']} — NO NEW ENTRIES allowed"],
        }

    notes.append(f"✅ Trading allowed — {session_info['label']}")

    if session_info["is_overlap"]:
        notes.append("🔥 London-NY overlap — PEAK volume and volatility")

    if session_info["po3_phase"] == "MANIPULATION" and daily_open and current_price:
        if trade_direction == "LONG" and current_price < daily_open:
            notes.append(
                "✅ PO3 ALIGNED — Price below daily open during Manipulation = buying opportunity"
            )
        elif trade_direction == "SHORT" and current_price > daily_open:
            notes.append(
                "✅ PO3 ALIGNED — Price above daily open during Manipulation = selling opportunity"
            )
        elif trade_direction == "LONG" and current_price > daily_open:
            notes.append(
                "⚠️ PO3 CAUTION — Price above daily open during London. "
                "Wait for manipulation sweep below."
            )
        elif trade_direction == "SHORT" and current_price < daily_open:
            notes.append(
                "⚠️ PO3 CAUTION — Price below daily open during London. "
                "Wait for manipulation sweep above."
            )

    return {
        "passed": True,
        "notes": notes,
    }


def print_derivatives_report(pair, funding, oi, session):
    """Format and print the full derivatives analysis report."""
    print("\n╔══════════════════════════════════════════════════════════════╗")
    print(f"║  DERIVATIVES CONFLUENCE — {pair:<36}  ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    # Session info
    print("\n── ⏰ SESSION & PO3 TIMING ──")
    print(f"   UTC Time:     {session['utc_time']}")
    print(f"   Session:      {session['label']}")
    print(f"   PO3 Phase:    {session['po3_phase']}")
    print(f"   Can Trade:    {'✅ YES' if session['can_trade'] else '❌ NO — Wait for London/NY'}")
    if session["is_overlap"]:
        print("   🔥 LONDON-NY OVERLAP — Peak global volume")

    # Funding rate
    print("\n── 💰 FUNDING RATE ──")
    print(f"   Current:      {funding['current_rate']:+.6f} ({funding['current_pct']:+.4f}%)")
    print(f"   Direction:    {funding['direction']}")
    print(f"   Daily Cost:   {funding['projected_daily_cost_pct']:.4f}% (projected)")
    print(f"   Assessment:   {funding['interpretation']}")

    if funding["history"]:
        print("\n   Recent Funding History:")
        for h in funding["history"][-5:]:
            bar_len = int(abs(h["rate"]) * 20000)
            bar = ("+" * bar_len) if h["rate"] > 0 else ("-" * bar_len)
            print(f"     {h['time']}  {h['pct']:+.4f}%  {bar}")

    # Open Interest
    print("\n── 📊 OPEN INTEREST KINEMATICS ──")
    print(f"   Current OI:   {oi['current_oi']:,.2f}")
    print(f"   4H OI Change: {oi['oi_change_pct']:+.2f}%")
    print(f"   Price Change:  {oi['price_change_pct']:+.2f}%")
    print(f"   State:        {oi['kinematic_state']}")
    print(f"   Assessment:   {oi['interpretation']}")

    # Gate evaluations for both directions
    for direction in ["LONG", "SHORT"]:
        g4 = evaluate_gate4(funding, oi, direction)
        print(f"\n── Gate 4 Evaluation for {direction} ──")
        print(
            f"   Result: {'✅ PASS' if g4['passed'] else '❌ FAIL'} │ "
            f"Confidence: {g4['confidence']} │ Score: {g4['score']}"
        )
        for note in g4["notes"]:
            print(f"     {note}")

    g5 = evaluate_gate5(session, "LONG")
    print("\n── Gate 5 Evaluation (Temporal) ──")
    print(f"   Result: {'✅ PASS' if g5['passed'] else '❌ FAIL'}")
    for note in g5["notes"]:
        print(f"     {note}")

    # JSON output
    print("\n--- DERIVATIVES_JSON_START ---")
    output = {
        "pair": pair,
        "session": session,
        "funding": {
            "rate": funding["current_rate"],
            "pct": funding["current_pct"],
            "direction": funding["direction"],
            "extreme": funding["extreme"],
            "extreme_type": funding["extreme_type"],
        },
        "oi": {
            "current": oi["current_oi"],
            "change_pct": oi["oi_change_pct"],
            "kinematic_state": oi["kinematic_state"],
        },
        "gate4_long": evaluate_gate4(funding, oi, "LONG"),
        "gate4_short": evaluate_gate4(funding, oi, "SHORT"),
        "gate5": {"can_trade": session["can_trade"], "session": session["session"]},
    }
    print(json.dumps(output, default=str))
    print("--- DERIVATIVES_JSON_END ---")


def main():
    parser = argparse.ArgumentParser(description="SMC Derivatives Confluence Analyzer")
    parser.add_argument("pair", help="Trading pair (e.g., BTCUSDT)")

    args = parser.parse_args()
    pair = args.pair.upper()

    client = get_client()
    session = get_current_session()
    funding = analyze_funding_rate(client, pair)
    oi = analyze_open_interest(client, pair)

    print_derivatives_report(pair, funding, oi, session)


if __name__ == "__main__":
    main()
