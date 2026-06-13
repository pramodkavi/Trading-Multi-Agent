#!/usr/bin/env python3
"""
detect_structure.py — Market Structure Detection Engine

Identifies:
  - Swing Highs and Swing Lows (fractal pivots)
  - Break of Structure (BOS) — trend continuation
  - Change of Character (CHoCH) / Market Structure Shift (MSS) — reversal warning
  - Current market phase: UPTREND / DOWNTREND / CONSOLIDATION
  - Premium / Discount zones with OTE calculation

Usage:
    python3 detect_structure.py <PAIR> [--data-file FILE]

If --data-file is provided, reads cached JSON kline data.
Otherwise, fetches fresh data from Binance.
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

# ─── Config ──────────────────────────────────────────────────────────────────
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
SWING_LOOKBACK = 3  # Candles on each side to confirm a swing point


# ─── Data Loading ────────────────────────────────────────────────────────────
def fetch_klines(pair, timeframe, limit):
    """Fetch klines from Binance Futures API."""
    if Client is None:
        print("ERROR: python-binance not installed")
        sys.exit(1)
    client = Client(API_KEY, API_SECRET)
    tf_map = {
        "1m": "1m",
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "1h": "1h",
        "4h": "4h",
        "1d": "1d",
    }
    raw = client.futures_klines(
        symbol=pair.upper(), interval=tf_map.get(timeframe, timeframe), limit=limit
    )
    candles = []
    for k in raw:
        candles.append(
            {
                "open_time": int(k[0]),
                "time_str": dt.datetime.utcfromtimestamp(int(k[0]) / 1000).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            }
        )
    return candles


def load_from_file(filepath):
    """Load kline data from cached JSON file."""
    with open(filepath) as f:
        data = json.load(f)
    # Handle both raw list and MTF dict formats
    if isinstance(data, dict):
        return data  # MTF format: {"1d": [...], "4h": [...], ...}
    return data  # Raw list format


# ─── Swing Point Detection ───────────────────────────────────────────────────
def detect_swing_points(candles, lookback=SWING_LOOKBACK):
    """
    Detect swing highs and swing lows using fractal pivot logic.
    A swing high: candle's high is the highest among (lookback) candles on each side.
    A swing low: candle's low is the lowest among (lookback) candles on each side.
    """
    swing_highs = []
    swing_lows = []

    for i in range(lookback, len(candles) - lookback):
        # Check swing high
        is_sh = True
        for j in range(1, lookback + 1):
            if (
                candles[i]["high"] <= candles[i - j]["high"]
                or candles[i]["high"] <= candles[i + j]["high"]
            ):
                is_sh = False
                break
        if is_sh:
            swing_highs.append(
                {
                    "index": i,
                    "price": candles[i]["high"],
                    "time": candles[i]["time_str"],
                    "open_time": candles[i]["open_time"],
                    "type": "swing_high",
                }
            )

        # Check swing low
        is_sl = True
        for j in range(1, lookback + 1):
            if (
                candles[i]["low"] >= candles[i - j]["low"]
                or candles[i]["low"] >= candles[i + j]["low"]
            ):
                is_sl = False
                break
        if is_sl:
            swing_lows.append(
                {
                    "index": i,
                    "price": candles[i]["low"],
                    "time": candles[i]["time_str"],
                    "open_time": candles[i]["open_time"],
                    "type": "swing_low",
                }
            )

    return swing_highs, swing_lows


# ─── Market Structure Analysis ───────────────────────────────────────────────
def analyze_structure(swing_highs, swing_lows, candles):
    """
    Determine market phase and detect BOS / CHoCH events.

    BOS (Break of Structure) = trend continuation:
      - Bullish BOS: candle body closes ABOVE previous swing high in an uptrend
      - Bearish BOS: candle body closes BELOW previous swing low in a downtrend

    CHoCH (Change of Character) = reversal warning:
      - Bullish CHoCH: in a downtrend, price breaks above the last lower high
      - Bearish CHoCH: in an uptrend, price breaks below the last higher low
    """
    # Merge and sort all swing points chronologically
    all_swings = sorted(swing_highs + swing_lows, key=lambda x: x["index"])

    if len(all_swings) < 4:
        return {
            "phase": "INSUFFICIENT_DATA",
            "events": [],
            "swings": all_swings,
        }

    events = []
    # Track sequence of HH, HL, LH, LL
    labeled_swings = []

    for i, sw in enumerate(all_swings):
        if sw["type"] == "swing_high":
            # Compare to previous swing high
            prev_sh = None
            for j in range(i - 1, -1, -1):
                if all_swings[j]["type"] == "swing_high":
                    prev_sh = all_swings[j]
                    break
            if prev_sh:
                if sw["price"] > prev_sh["price"]:
                    sw["label"] = "HH"  # Higher High
                else:
                    sw["label"] = "LH"  # Lower High
            else:
                sw["label"] = "SH"  # First swing high

        elif sw["type"] == "swing_low":
            # Compare to previous swing low
            prev_sl = None
            for j in range(i - 1, -1, -1):
                if all_swings[j]["type"] == "swing_low":
                    prev_sl = all_swings[j]
                    break
            if prev_sl:
                if sw["price"] > prev_sl["price"]:
                    sw["label"] = "HL"  # Higher Low
                else:
                    sw["label"] = "LL"  # Lower Low
            else:
                sw["label"] = "SL"  # First swing low

        labeled_swings.append(sw)

    # Detect BOS and CHoCH events by scanning candles against swing levels
    recent_highs = [s for s in labeled_swings if s["type"] == "swing_high"][-5:]
    recent_lows = [s for s in labeled_swings if s["type"] == "swing_low"][-5:]

    for i, candle in enumerate(candles):
        # Check for bullish BOS: body close above previous swing high
        for sh in recent_highs:
            if sh["index"] < i and candle["close"] > sh["price"] and candle["open"] < sh["price"]:
                # Displacement check: body must close beyond, not just wick
                body_top = max(candle["open"], candle["close"])
                if body_top > sh["price"]:
                    events.append(
                        {
                            "type": "BOS_BULLISH",
                            "index": i,
                            "time": candle["time_str"],
                            "level_broken": sh["price"],
                            "close_price": candle["close"],
                            "swing_label": sh.get("label", "SH"),
                            "description": (
                                f"Bullish BOS — Body close above "
                                f"{sh.get('label', 'SH')} at {sh['price']:.2f}"
                            ),
                        }
                    )

        # Check for bearish BOS: body close below previous swing low
        for sl in recent_lows:
            if sl["index"] < i and candle["close"] < sl["price"] and candle["open"] > sl["price"]:
                body_bottom = min(candle["open"], candle["close"])
                if body_bottom < sl["price"]:
                    events.append(
                        {
                            "type": "BOS_BEARISH",
                            "index": i,
                            "time": candle["time_str"],
                            "level_broken": sl["price"],
                            "close_price": candle["close"],
                            "swing_label": sl.get("label", "SL"),
                            "description": (
                                f"Bearish BOS — Body close below "
                                f"{sl.get('label', 'SL')} at {sl['price']:.2f}"
                            ),
                        }
                    )

    # Detect CHoCH: a structural break AGAINST the prevailing trend
    # If we've been making LH/LL and price breaks above a LH → Bullish CHoCH
    # If we've been making HH/HL and price breaks below a HL → Bearish CHoCH
    for ev in events:
        if ev["type"] == "BOS_BULLISH" and ev["swing_label"] == "LH":
            ev["type"] = "CHOCH_BULLISH"
            ev["description"] = (
                f"🔄 Bullish CHoCH — Price broke above Lower High at {ev['level_broken']:.2f}"
            )
        elif ev["type"] == "BOS_BEARISH" and ev["swing_label"] == "HL":
            ev["type"] = "CHOCH_BEARISH"
            ev["description"] = (
                f"🔄 Bearish CHoCH — Price broke below Higher Low at {ev['level_broken']:.2f}"
            )

    # Deduplicate events (keep first occurrence at each level)
    seen_levels = set()
    unique_events = []
    for ev in events:
        key = (ev["type"], round(ev["level_broken"], 2))
        if key not in seen_levels:
            seen_levels.add(key)
            unique_events.append(ev)

    # Determine current market phase from recent swing labels
    recent_labels = [s.get("label", "") for s in labeled_swings[-6:]]
    hh_count = recent_labels.count("HH")
    hl_count = recent_labels.count("HL")
    lh_count = recent_labels.count("LH")
    ll_count = recent_labels.count("LL")

    if hh_count >= 1 and hl_count >= 1 and ll_count == 0:
        phase = "UPTREND"
    elif ll_count >= 1 and lh_count >= 1 and hh_count == 0:
        phase = "DOWNTREND"
    else:
        phase = "CONSOLIDATION"

    # Check for very recent CHoCH that might override
    recent_events = [e for e in unique_events if e["index"] >= len(candles) - 15]
    for ev in recent_events:
        if ev["type"] == "CHOCH_BULLISH" and phase == "DOWNTREND":
            phase = "CHOCH_TO_BULLISH"
        elif ev["type"] == "CHOCH_BEARISH" and phase == "UPTREND":
            phase = "CHOCH_TO_BEARISH"

    return {
        "phase": phase,
        "events": unique_events[-10:],  # Last 10 structural events
        "swings": labeled_swings,
    }


# ─── Premium / Discount Calculator ──────────────────────────────────────────
def calculate_premium_discount(swing_highs, swing_lows, current_price):
    """
    Calculate the dealing range, equilibrium, premium/discount zones, and OTE.
    Uses the most recent significant structural leg.
    """
    if not swing_highs or not swing_lows:
        return None

    # Use the most recent swing high and swing low to define the dealing range
    last_sh = swing_highs[-1]["price"]
    last_sl = swing_lows[-1]["price"]

    # Ensure we have a valid range
    range_high = max(last_sh, last_sl)
    range_low = min(last_sh, last_sl)

    if range_high == range_low:
        return None

    equilibrium = (range_high + range_low) / 2
    range_size = range_high - range_low

    # OTE Zone: 61.8% to 78.6% retracement
    # For a bullish leg (low to high): OTE is in the lower portion (discount)
    # For a bearish leg (high to low): OTE is in the upper portion (premium)
    ote_618 = range_high - (range_size * 0.618)
    ote_786 = range_high - (range_size * 0.786)

    # Determine if price is in premium or discount
    is_premium = current_price > equilibrium
    zone = "PREMIUM" if is_premium else "DISCOUNT"

    # Calculate exact position within the range
    position_pct = (current_price - range_low) / range_size * 100

    return {
        "range_high": range_high,
        "range_low": range_low,
        "equilibrium": equilibrium,
        "ote_618": ote_618,
        "ote_786": ote_786,
        "zone": zone,
        "position_pct": position_pct,
        "current_price": current_price,
    }


# ─── Output Formatting ──────────────────────────────────────────────────────
def print_structure_report(pair, timeframe, candles, structure, pd_zones):
    """Format and print the full structure analysis report."""
    phase = structure["phase"]
    events = structure["events"]
    swings = structure["swings"]

    phase_emoji = {
        "UPTREND": "🟢",
        "DOWNTREND": "🔴",
        "CONSOLIDATION": "🟡",
        "CHOCH_TO_BULLISH": "🔄🟢",
        "CHOCH_TO_BEARISH": "🔄🔴",
        "INSUFFICIENT_DATA": "⚠️",
    }

    print("\n╔══════════════════════════════════════════════════════════════╗")
    print(f"║  MARKET STRUCTURE — {pair} {timeframe.upper():<38}  ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    print(f"\n{phase_emoji.get(phase, '❓')} MARKET PHASE: {phase}")

    if phase == "UPTREND":
        print("   Institutional bias: BULLISH — Higher Highs and Higher Lows in sequence")
        print("   AI Directive: Seek LONG entries ONLY in Discount zones")
    elif phase == "DOWNTREND":
        print("   Institutional bias: BEARISH — Lower Lows and Lower Highs in sequence")
        print("   AI Directive: Seek SHORT entries ONLY in Premium zones")
    elif phase == "CONSOLIDATION":
        print("   Institutional bias: NEUTRAL — Range-bound accumulation/distribution")
        print("   AI Directive: WAIT for structural break before committing directionally")
    elif "CHOCH" in phase:
        print("   ⚠️  REGIME CHANGE DETECTED — Awaiting confirmation")

    # Swing Points
    recent_swings = swings[-10:]
    if recent_swings:
        print("\n── Recent Swing Points ──")
        for s in recent_swings:
            label = s.get("label", s["type"][:2].upper())
            emoji = "🔺" if "high" in s["type"] else "🔻"
            print(f"   {emoji} {label:>3} │ ${s['price']:<12.2f} │ {s['time']} │ idx:{s['index']}")

    # Structural Events
    if events:
        print("\n── Structural Events (BOS / CHoCH) ──")
        for ev in events[-8:]:
            emoji = "✅" if "BOS" in ev["type"] else "🔄"
            print(f"   {emoji} {ev['description']}")
            print(f"      Time: {ev['time']} │ Close: ${ev['close_price']:.2f}")

    # Premium / Discount
    if pd_zones:
        print("\n── Premium / Discount Array ──")
        print(f"   Dealing Range:  ${pd_zones['range_low']:.2f} — ${pd_zones['range_high']:.2f}")
        print(f"   Equilibrium:    ${pd_zones['equilibrium']:.2f} (50%)")
        print(
            f"   OTE Zone:       ${pd_zones['ote_786']:.2f} — "
            f"${pd_zones['ote_618']:.2f} (78.6% — 61.8%)"
        )
        print(f"   Current Price:  ${pd_zones['current_price']:.2f}")
        print(f"   Current Zone:   {pd_zones['zone']} ({pd_zones['position_pct']:.1f}% of range)")

        if pd_zones["zone"] == "PREMIUM":
            print("   ⚠️  Price in PREMIUM — LONGS FORBIDDEN. Shorts only in this zone.")
        else:
            print("   ✅ Price in DISCOUNT — SHORTS FORBIDDEN. Longs only in this zone.")

    # JSON output for script piping
    print("\n--- STRUCTURE_JSON_START ---")
    output = {
        "pair": pair,
        "timeframe": timeframe,
        "phase": phase,
        "swing_highs": [
            {"price": s["price"], "time": s["time"], "label": s.get("label", "")}
            for s in swings
            if s["type"] == "swing_high"
        ][-5:],
        "swing_lows": [
            {"price": s["price"], "time": s["time"], "label": s.get("label", "")}
            for s in swings
            if s["type"] == "swing_low"
        ][-5:],
        "events": events[-5:],
        "premium_discount": pd_zones,
    }
    print(json.dumps(output, default=str))
    print("--- STRUCTURE_JSON_END ---")


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="SMC Market Structure Detector")
    parser.add_argument("pair", help="Trading pair (e.g., BTCUSDT)")
    parser.add_argument("--timeframe", "-tf", default="4h", help="Timeframe (default: 4h)")
    parser.add_argument("--limit", type=int, default=100, help="Number of candles (default: 100)")
    parser.add_argument("--data-file", help="Path to cached JSON kline data")
    parser.add_argument(
        "--all-tf",
        action="store_true",
        help="Run structure analysis on ALL SMC timeframes (1d, 4h, 1h, 15m, 5m)",
    )

    args = parser.parse_args()

    if args.all_tf:
        timeframes = ["1d", "4h", "1h", "15m", "5m"]
        limits = [60, 100, 100, 100, 100]
    else:
        timeframes = [args.timeframe]
        limits = [args.limit]

    for tf, lim in zip(timeframes, limits, strict=False):
        if args.data_file:
            data = load_from_file(args.data_file)
            if isinstance(data, dict):
                candles = data.get(tf, [])
                # Convert raw dicts to our format if needed
                if candles and "time_str" not in candles[0]:
                    for c in candles:
                        c["time_str"] = dt.datetime.utcfromtimestamp(
                            c["open_time"] / 1000
                        ).strftime("%Y-%m-%d %H:%M")
            else:
                candles = data
        else:
            candles = fetch_klines(args.pair, tf, lim)

        if not candles or len(candles) < SWING_LOOKBACK * 3:
            print(f"ERROR: Not enough candle data for {tf} (need at least {SWING_LOOKBACK * 3})")
            continue

        swing_highs, swing_lows = detect_swing_points(candles, SWING_LOOKBACK)
        structure = analyze_structure(swing_highs, swing_lows, candles)

        current_price = candles[-1]["close"]
        pd_zones = calculate_premium_discount(swing_highs, swing_lows, current_price)

        print_structure_report(args.pair, tf, candles, structure, pd_zones)


if __name__ == "__main__":
    main()
