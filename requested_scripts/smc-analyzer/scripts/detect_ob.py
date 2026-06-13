#!/usr/bin/env python3
"""
detect_ob.py — Order Block Detection Engine

Identifies:
  - Order Blocks (OB): Last opposing candle before a displacement + BOS
  - Breaker Blocks (BB): Failed OBs that swept liquidity before breaking
  - Mitigation Blocks (MB): Failed OBs without preceding liquidity sweep
  - Validates each OB against 4 structural criteria:
    1. Displacement (impulsive move)
    2. Imbalance (FVG present)
    3. Structural break (BOS or CHoCH)
    4. Mitigated vs unmitigated status

Usage:
    python3 detect_ob.py <PAIR> [--timeframe 4h] [--limit 100]
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

# Order Block validation thresholds
OB_MIN_DISPLACEMENT_PCT = 0.3  # Min body size of displacement candle (%)
OB_MIN_MOVE_CANDLES = 2  # Min candles of follow-through after OB


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


def is_bearish_candle(candle):
    """Return True if candle closed lower than it opened."""
    return candle["close"] < candle["open"]


def is_bullish_candle(candle):
    """Return True if candle closed higher than it opened."""
    return candle["close"] > candle["open"]


def body_size_pct(candle):
    """Return the body size as a percentage of the candle's open price."""
    if candle["open"] == 0:
        return 0
    return abs(candle["close"] - candle["open"]) / candle["open"] * 100


def has_fvg_after(candles, start_idx, direction, lookforward=3):
    """
    Check if a Fair Value Gap exists in the candles following the OB.
    Returns the FVG details if found, None otherwise.
    """
    for i in range(start_idx + 1, min(start_idx + lookforward + 1, len(candles) - 1)):
        if i < 2:
            continue
        c1 = candles[i - 2]
        c3 = candles[i]

        if direction == "bullish" and c1["high"] < c3["low"]:
            return {"top": c3["low"], "bottom": c1["high"], "index": i - 1}
        if direction == "bearish" and c1["low"] > c3["high"]:
            return {"top": c1["low"], "bottom": c3["high"], "index": i - 1}

    return None


def has_structure_break(candles, start_idx, direction, lookforward=5):
    """
    Check if the displacement from the OB resulted in a Break of Structure.
    Looks for a body close beyond the nearest swing point.
    """
    # Find the nearest swing to break
    if direction == "bullish":
        # Find the highest high before the OB
        lookback_start = max(0, start_idx - 10)
        prev_high = max(c["high"] for c in candles[lookback_start:start_idx])

        # Check if any candle after OB closes above that high
        for i in range(start_idx + 1, min(start_idx + lookforward + 1, len(candles))):
            if candles[i]["close"] > prev_high:
                return True
    else:
        lookback_start = max(0, start_idx - 10)
        prev_low = min(c["low"] for c in candles[lookback_start:start_idx])

        for i in range(start_idx + 1, min(start_idx + lookforward + 1, len(candles))):
            if candles[i]["close"] < prev_low:
                return True

    return False


def detect_order_blocks(candles):
    """
    Detect Order Blocks with full validation.

    Bullish OB: The last BEARISH candle before a sharp bullish displacement
    that results in a BOS and leaves an FVG.

    Bearish OB: The last BULLISH candle before a sharp bearish displacement
    that results in a BOS and leaves an FVG.
    """
    order_blocks = []

    for i in range(1, len(candles) - 2):
        current = candles[i]
        next_candle = candles[i + 1]

        # ─── Bullish Order Block ─────────────────────────────────────────
        # Current candle is bearish (the OB), next candle is a strong bullish displacement
        if is_bearish_candle(current) and is_bullish_candle(next_candle):
            disp_pct = body_size_pct(next_candle)
            if disp_pct >= OB_MIN_DISPLACEMENT_PCT:
                # Validate: check for FVG in the displacement
                fvg = has_fvg_after(candles, i, "bullish")
                # Validate: check for structural break
                bos = has_structure_break(candles, i, "bullish")

                # Check if OB has been mitigated (price returned to OB zone)
                ob_top = max(current["open"], current["close"])
                ob_bottom = min(current["open"], current["close"])
                mitigated = False
                mitigated_idx = None

                for j in range(i + 2, len(candles)):
                    if candles[j]["low"] <= ob_top:
                        mitigated = True
                        mitigated_idx = j
                        break

                # Determine if this is a standard OB, Breaker, or Mitigation block
                ob_type = "ORDER_BLOCK"
                # Will be reclassified as Breaker or Mitigation in post-processing

                # Quality score based on validations
                quality = 0
                validators = []
                if disp_pct >= OB_MIN_DISPLACEMENT_PCT:
                    quality += 1
                    validators.append(f"displacement:{disp_pct:.2f}%")
                if fvg:
                    quality += 1
                    validators.append(f"FVG:{fvg['bottom']:.2f}-{fvg['top']:.2f}")
                if bos:
                    quality += 1
                    validators.append("BOS_confirmed")
                if not mitigated:
                    quality += 1
                    validators.append("UNMITIGATED")

                order_blocks.append(
                    {
                        "direction": "BULLISH",
                        "type": ob_type,
                        "index": i,
                        "time": current["time_str"],
                        "ob_high": ob_top,  # Top of the OB zone
                        "ob_low": ob_bottom,  # Bottom of the OB zone (aggressive entry)
                        "ob_body_top": max(current["open"], current["close"]),
                        "ob_body_bottom": min(current["open"], current["close"]),
                        "wick_low": current["low"],  # Extreme of OB (conservative SL)
                        "displacement_pct": disp_pct,
                        "has_fvg": fvg is not None,
                        "fvg_details": fvg,
                        "has_bos": bos,
                        "mitigated": mitigated,
                        "mitigated_idx": mitigated_idx,
                        "quality": quality,
                        "validators": validators,
                    }
                )

        # ─── Bearish Order Block ─────────────────────────────────────────
        if is_bullish_candle(current) and is_bearish_candle(next_candle):
            disp_pct = body_size_pct(next_candle)
            if disp_pct >= OB_MIN_DISPLACEMENT_PCT:
                fvg = has_fvg_after(candles, i, "bearish")
                bos = has_structure_break(candles, i, "bearish")

                ob_top = max(current["open"], current["close"])
                ob_bottom = min(current["open"], current["close"])
                mitigated = False
                mitigated_idx = None

                for j in range(i + 2, len(candles)):
                    if candles[j]["high"] >= ob_bottom:
                        mitigated = True
                        mitigated_idx = j
                        break

                quality = 0
                validators = []
                if disp_pct >= OB_MIN_DISPLACEMENT_PCT:
                    quality += 1
                    validators.append(f"displacement:{disp_pct:.2f}%")
                if fvg:
                    quality += 1
                    validators.append(f"FVG:{fvg['bottom']:.2f}-{fvg['top']:.2f}")
                if bos:
                    quality += 1
                    validators.append("BOS_confirmed")
                if not mitigated:
                    quality += 1
                    validators.append("UNMITIGATED")

                order_blocks.append(
                    {
                        "direction": "BEARISH",
                        "type": "ORDER_BLOCK",
                        "index": i,
                        "time": current["time_str"],
                        "ob_high": ob_top,
                        "ob_low": ob_bottom,
                        "ob_body_top": ob_top,
                        "ob_body_bottom": ob_bottom,
                        "wick_high": current["high"],
                        "displacement_pct": disp_pct,
                        "has_fvg": fvg is not None,
                        "fvg_details": fvg,
                        "has_bos": bos,
                        "mitigated": mitigated,
                        "mitigated_idx": mitigated_idx,
                        "quality": quality,
                        "validators": validators,
                    }
                )

    return order_blocks


def classify_breakers_mitigations(order_blocks, candles):
    """
    Post-process order blocks to identify Breaker Blocks and Mitigation Blocks.

    Breaker Block: An OB that was broken through by price AFTER sweeping
    external liquidity — very high probability when price returns.

    Mitigation Block: An OB where the move failed to make a new extreme
    (no liquidity sweep) — moderate probability.
    """
    for ob in order_blocks:
        idx = ob["index"]
        direction = ob["direction"]

        # Check if this OB was violated (price traded through it)
        violated = False
        if direction == "BULLISH":
            for j in range(idx + 2, len(candles)):
                if candles[j]["close"] < ob["ob_low"]:
                    violated = True
                    break
        else:
            for j in range(idx + 2, len(candles)):
                if candles[j]["close"] > ob["ob_high"]:
                    violated = True
                    break

        if violated:
            # Check if liquidity was swept before the violation
            # Look for a new extreme (higher high for bullish / lower low for bearish)
            # created between the OB formation and its violation
            made_new_extreme = False
            lookback_start = max(0, idx - 15)

            if direction == "BULLISH":
                prev_high = max(c["high"] for c in candles[lookback_start:idx])
                for j in range(idx + 1, min(idx + 10, len(candles))):
                    if candles[j]["high"] > prev_high:
                        made_new_extreme = True
                        break
            else:
                prev_low = min(c["low"] for c in candles[lookback_start:idx])
                for j in range(idx + 1, min(idx + 10, len(candles))):
                    if candles[j]["low"] < prev_low:
                        made_new_extreme = True
                        break

            if made_new_extreme:
                ob["type"] = "BREAKER_BLOCK"
                ob["quality"] += 1
                ob["validators"].append("BREAKER:swept_liquidity_before_failing")
            else:
                ob["type"] = "MITIGATION_BLOCK"
                ob["validators"].append("MITIGATION:failed_without_sweep")

    return order_blocks


def print_ob_report(pair, timeframe, candles, order_blocks):
    """Format and print Order Block analysis report."""
    current_price = candles[-1]["close"]

    print("\n╔══════════════════════════════════════════════════════════════╗")
    print(f"║  ORDER BLOCKS — {pair} {timeframe.upper():<40}  ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"   Current Price: ${current_price:,.2f}")
    print(f"   Total OBs Found: {len(order_blocks)}")

    # Separate by type and quality
    active_bullish = [
        ob
        for ob in order_blocks
        if ob["direction"] == "BULLISH" and not ob["mitigated"] and ob["type"] == "ORDER_BLOCK"
    ]
    active_bearish = [
        ob
        for ob in order_blocks
        if ob["direction"] == "BEARISH" and not ob["mitigated"] and ob["type"] == "ORDER_BLOCK"
    ]
    breakers = [ob for ob in order_blocks if ob["type"] == "BREAKER_BLOCK"]
    mitigations = [ob for ob in order_blocks if ob["type"] == "MITIGATION_BLOCK"]

    print(f"   Active Bullish OBs:  {len(active_bullish)}")
    print(f"   Active Bearish OBs:  {len(active_bearish)}")
    print(f"   Breaker Blocks:      {len(breakers)}")
    print(f"   Mitigation Blocks:   {len(mitigations)}")

    def print_ob_list(title, obs, emoji):
        if not obs:
            return
        print(f"\n── {emoji} {title} ──")
        for ob in sorted(obs, key=lambda x: x["quality"], reverse=True):
            quality_bar = "★" * ob["quality"] + "☆" * (4 - ob["quality"])
            dist = (
                (ob["ob_high"] - current_price) / current_price * 100
                if ob["direction"] == "BEARISH"
                else (current_price - ob["ob_low"]) / current_price * 100
            )
            proximity = (
                "🎯 PRICE IN ZONE" if (ob["ob_low"] <= current_price <= ob["ob_high"]) else ""
            )

            print(
                f"   [{quality_bar}] ${ob['ob_low']:>12,.2f} — ${ob['ob_high']:>12,.2f}  │  "
                f"{ob['type']:<17} │ {dist:+.2f}% away  {proximity}"
            )
            print(
                f"   └─ Formed: {ob['time']} │ Disp: {ob['displacement_pct']:.2f}% │ "
                f"Validators: {', '.join(ob['validators'])}"
            )

    print_ob_list("BULLISH ORDER BLOCKS (Support — Long Entries)", active_bullish, "🟢")
    print_ob_list("BEARISH ORDER BLOCKS (Resistance — Short Entries)", active_bearish, "🔴")
    print_ob_list("BREAKER BLOCKS (High Probability — Trapped Capital)", breakers, "💥")
    print_ob_list("MITIGATION BLOCKS (Moderate Probability)", mitigations, "⚠️")

    # Nearest OB to current price
    all_active = [ob for ob in order_blocks if not ob["mitigated"] or ob["type"] == "BREAKER_BLOCK"]
    if all_active:
        nearest = min(
            all_active,
            key=lambda ob: min(
                abs(current_price - ob["ob_high"]), abs(current_price - ob["ob_low"])
            ),
        )
        print("\n── 🎯 NEAREST ACTIVE BLOCK ──")
        print(f"   Type:      {nearest['type']} ({nearest['direction']})")
        print(f"   Zone:      ${nearest['ob_low']:,.2f} — ${nearest['ob_high']:,.2f}")
        stars = "★" * nearest["quality"] + "☆" * (4 - nearest["quality"])
        print(f"   Quality:   {stars} ({nearest['quality']}/4)")
        print(f"   Validates: {', '.join(nearest['validators'])}")

    # JSON output
    print("\n--- OB_JSON_START ---")
    output = {
        "pair": pair,
        "timeframe": timeframe,
        "current_price": current_price,
        "order_blocks": [
            {
                "direction": ob["direction"],
                "type": ob["type"],
                "ob_high": ob["ob_high"],
                "ob_low": ob["ob_low"],
                "quality": ob["quality"],
                "validators": ob["validators"],
                "has_fvg": ob["has_fvg"],
                "has_bos": ob["has_bos"],
                "mitigated": ob["mitigated"],
                "time": ob["time"],
                "displacement_pct": ob["displacement_pct"],
            }
            for ob in order_blocks
            if not ob["mitigated"] or ob["type"] in ("BREAKER_BLOCK",)
        ],
    }
    print(json.dumps(output, default=str))
    print("--- OB_JSON_END ---")


def main():
    parser = argparse.ArgumentParser(description="SMC Order Block Detector")
    parser.add_argument("pair", help="Trading pair (e.g., BTCUSDT)")
    parser.add_argument("--timeframe", "-tf", default="4h", help="Timeframe (default: 4h)")
    parser.add_argument("--limit", type=int, default=100, help="Number of candles (default: 100)")
    parser.add_argument("--all-tf", action="store_true", help="Scan all SMC timeframes")

    args = parser.parse_args()

    if args.all_tf:
        timeframes = [("1d", 60), ("4h", 100), ("1h", 100), ("15m", 100), ("5m", 100)]
    else:
        timeframes = [(args.timeframe, args.limit)]

    for tf, lim in timeframes:
        candles = fetch_klines(args.pair, tf, lim)
        if len(candles) < 5:
            print("ERROR: Need at least 5 candles for OB detection")
            continue

        order_blocks = detect_order_blocks(candles)
        order_blocks = classify_breakers_mitigations(order_blocks, candles)
        print_ob_report(args.pair, tf, candles, order_blocks)


if __name__ == "__main__":
    main()
