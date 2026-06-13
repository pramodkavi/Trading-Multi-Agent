#!/usr/bin/env python3
"""
detect_fvg.py — Fair Value Gap (FVG) Detection Engine

Identifies market inefficiencies using the 3-candle geometric rule:
  - Bullish FVG: Candle 1 HIGH < Candle 3 LOW (gap between them)
  - Bearish FVG: Candle 1 LOW > Candle 3 HIGH (gap between them)

Also tracks:
  - FVG fill status (has price retraced to fill the gap?)
  - FVG size relative to price (filters out noise)
  - Confluence with displacement (large impulsive candle 2)

Usage:
    python3 detect_fvg.py <PAIR> [--timeframe 4h] [--limit 100]
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

# Minimum FVG size as percentage of price to filter micro-gaps
FVG_MIN_SIZE_PCT = 0.05
# Minimum displacement body size percentage for "impulsive" classification
DISPLACEMENT_MIN_PCT = 0.3


def fetch_klines(pair, timeframe, limit):
    """Fetch klines from Binance Futures."""
    if Client is None:
        print("ERROR: python-binance not installed")
        sys.exit(1)
    client = Client(API_KEY, API_SECRET)
    raw = client.futures_klines(symbol=pair.upper(), interval=timeframe, limit=limit)
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


def detect_fvgs(candles, min_size_pct=FVG_MIN_SIZE_PCT):
    """
    Scan candle data for Fair Value Gaps.

    Bullish FVG: In a 3-candle sequence, Candle 1's HIGH does not overlap
    with Candle 3's LOW. The gap between them is the FVG.

    Bearish FVG: Candle 1's LOW does not overlap with Candle 3's HIGH.
    """
    fvgs = []

    for i in range(2, len(candles)):
        c1 = candles[i - 2]  # First candle
        c2 = candles[i - 1]  # Middle candle (the displacement candle)
        c3 = candles[i]  # Third candle

        # ─── Bullish FVG ─────────────────────────────────────────────────
        # Gap exists when Candle 1's high is BELOW Candle 3's low
        if c1["high"] < c3["low"]:
            gap_top = c3["low"]
            gap_bottom = c1["high"]
            gap_size = gap_top - gap_bottom
            gap_pct = (gap_size / c2["close"]) * 100

            if gap_pct >= min_size_pct:
                # Measure displacement (candle 2 body size)
                body_size = abs(c2["close"] - c2["open"])
                body_pct = (body_size / c2["open"]) * 100
                is_displacement = body_pct >= DISPLACEMENT_MIN_PCT

                # Check if FVG has been filled by subsequent candles
                filled = False
                fill_index = None
                for j in range(i + 1, len(candles)):
                    if candles[j]["low"] <= gap_bottom:
                        filled = True
                        fill_index = j
                        break

                fvgs.append(
                    {
                        "type": "BULLISH_FVG",
                        "top": gap_top,
                        "bottom": gap_bottom,
                        "midpoint": (gap_top + gap_bottom) / 2,
                        "size": gap_size,
                        "size_pct": gap_pct,
                        "candle_index": i - 1,  # Index of the displacement candle
                        "time": c2["time_str"],
                        "displacement": is_displacement,
                        "displacement_pct": body_pct,
                        "filled": filled,
                        "fill_index": fill_index,
                        "c2_close": c2["close"],
                        "c2_volume": c2["volume"],
                    }
                )

        # ─── Bearish FVG ─────────────────────────────────────────────────
        # Gap exists when Candle 1's low is ABOVE Candle 3's high
        if c1["low"] > c3["high"]:
            gap_top = c1["low"]
            gap_bottom = c3["high"]
            gap_size = gap_top - gap_bottom
            gap_pct = (gap_size / c2["close"]) * 100

            if gap_pct >= min_size_pct:
                body_size = abs(c2["close"] - c2["open"])
                body_pct = (body_size / c2["open"]) * 100
                is_displacement = body_pct >= DISPLACEMENT_MIN_PCT

                filled = False
                fill_index = None
                for j in range(i + 1, len(candles)):
                    if candles[j]["high"] >= gap_top:
                        filled = True
                        fill_index = j
                        break

                fvgs.append(
                    {
                        "type": "BEARISH_FVG",
                        "top": gap_top,
                        "bottom": gap_bottom,
                        "midpoint": (gap_top + gap_bottom) / 2,
                        "size": gap_size,
                        "size_pct": gap_pct,
                        "candle_index": i - 1,
                        "time": c2["time_str"],
                        "displacement": is_displacement,
                        "displacement_pct": body_pct,
                        "filled": filled,
                        "fill_index": fill_index,
                        "c2_close": c2["close"],
                        "c2_volume": c2["volume"],
                    }
                )

    return fvgs


def print_fvg_report(pair, timeframe, candles, fvgs):
    """Format and print FVG analysis report."""
    current_price = candles[-1]["close"]

    print("\n╔══════════════════════════════════════════════════════════════╗")
    print(f"║  FAIR VALUE GAPS — {pair} {timeframe.upper():<38}  ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"   Current Price: ${current_price:,.2f}")
    print(f"   Total FVGs Found: {len(fvgs)}")

    # Separate into categories
    unfilled_bullish = [f for f in fvgs if f["type"] == "BULLISH_FVG" and not f["filled"]]
    unfilled_bearish = [f for f in fvgs if f["type"] == "BEARISH_FVG" and not f["filled"]]
    filled = [f for f in fvgs if f["filled"]]

    print(f"   Unfilled Bullish: {len(unfilled_bullish)}")
    print(f"   Unfilled Bearish: {len(unfilled_bearish)}")
    print(f"   Already Filled:   {len(filled)}")

    # Active (unfilled) FVGs — these are the ones that matter for entries
    if unfilled_bullish:
        print("\n── 🟢 UNFILLED BULLISH FVGs (Support/Entry Zones for Longs) ──")
        for f in sorted(unfilled_bullish, key=lambda x: x["bottom"], reverse=True):
            disp = "💥 DISPLACEMENT" if f["displacement"] else "   standard"
            dist = ((current_price - f["midpoint"]) / current_price) * 100
            proximity = "🎯 NEARBY" if abs(dist) < 1.0 else ""

            print(
                f"   ${f['bottom']:>12,.2f} — ${f['top']:>12,.2f}  │  "
                f"Size: {f['size_pct']:.3f}%  │  {disp}  │  "
                f"Mid: ${f['midpoint']:,.2f}  │  {dist:+.2f}% away  {proximity}"
            )
            print(
                f"   └─ Formed: {f['time']}  │  Body: {f['displacement_pct']:.2f}%  │  "
                f"idx:{f['candle_index']}"
            )

    if unfilled_bearish:
        print("\n── 🔴 UNFILLED BEARISH FVGs (Resistance/Entry Zones for Shorts) ──")
        for f in sorted(unfilled_bearish, key=lambda x: x["top"]):
            disp = "💥 DISPLACEMENT" if f["displacement"] else "   standard"
            dist = ((f["midpoint"] - current_price) / current_price) * 100
            proximity = "🎯 NEARBY" if abs(dist) < 1.0 else ""

            print(
                f"   ${f['bottom']:>12,.2f} — ${f['top']:>12,.2f}  │  "
                f"Size: {f['size_pct']:.3f}%  │  {disp}  │  "
                f"Mid: ${f['midpoint']:,.2f}  │  {dist:+.2f}% away  {proximity}"
            )
            print(
                f"   └─ Formed: {f['time']}  │  Body: {f['displacement_pct']:.2f}%  │  "
                f"idx:{f['candle_index']}"
            )

    # Nearest FVGs to current price
    all_unfilled = unfilled_bullish + unfilled_bearish
    if all_unfilled:
        nearest = min(all_unfilled, key=lambda f: abs(current_price - f["midpoint"]))
        print("\n── 🎯 NEAREST UNFILLED FVG ──")
        print(f"   Type: {nearest['type']}")
        print(f"   Zone: ${nearest['bottom']:,.2f} — ${nearest['top']:,.2f}")
        dist = abs(current_price - nearest["midpoint"]) / current_price * 100
        print(f"   Distance: {dist:.2f}% from current price")
        print(f"   Displacement: {'YES 💥' if nearest['displacement'] else 'No (weaker)'}")

    # JSON output
    print("\n--- FVG_JSON_START ---")
    output = {
        "pair": pair,
        "timeframe": timeframe,
        "current_price": current_price,
        "unfilled_bullish": [
            {
                "top": f["top"],
                "bottom": f["bottom"],
                "midpoint": f["midpoint"],
                "size_pct": f["size_pct"],
                "displacement": f["displacement"],
                "time": f["time"],
                "candle_index": f["candle_index"],
            }
            for f in unfilled_bullish
        ],
        "unfilled_bearish": [
            {
                "top": f["top"],
                "bottom": f["bottom"],
                "midpoint": f["midpoint"],
                "size_pct": f["size_pct"],
                "displacement": f["displacement"],
                "time": f["time"],
                "candle_index": f["candle_index"],
            }
            for f in unfilled_bearish
        ],
        "total_found": len(fvgs),
    }
    print(json.dumps(output, default=str))
    print("--- FVG_JSON_END ---")


def main():
    parser = argparse.ArgumentParser(description="SMC Fair Value Gap Detector")
    parser.add_argument("pair", help="Trading pair (e.g., BTCUSDT)")
    parser.add_argument("--timeframe", "-tf", default="4h", help="Timeframe (default: 4h)")
    parser.add_argument("--limit", type=int, default=100, help="Number of candles (default: 100)")
    parser.add_argument(
        "--min-size",
        type=float,
        default=FVG_MIN_SIZE_PCT,
        help=f"Minimum FVG size %% (default: {FVG_MIN_SIZE_PCT})",
    )
    parser.add_argument("--all-tf", action="store_true", help="Scan ALL SMC timeframes")

    args = parser.parse_args()

    if args.all_tf:
        timeframes = [("1d", 60), ("4h", 100), ("1h", 100), ("15m", 100), ("5m", 100)]
    else:
        timeframes = [(args.timeframe, args.limit)]

    for tf, lim in timeframes:
        candles = fetch_klines(args.pair, tf, lim)
        if len(candles) < 3:
            print("ERROR: Need at least 3 candles for FVG detection")
            continue

        fvgs = detect_fvgs(candles, args.min_size)
        print_fvg_report(args.pair, tf, candles, fvgs)


if __name__ == "__main__":
    main()
