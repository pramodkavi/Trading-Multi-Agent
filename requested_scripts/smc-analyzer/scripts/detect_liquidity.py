#!/usr/bin/env python3
"""
detect_liquidity.py — Liquidity Pool Mapping Engine

Identifies:
  - Buy-Side Liquidity (BSL): Pools above structural highs (retail buy-stops)
  - Sell-Side Liquidity (SSL): Pools below structural lows (retail sell-stops)
  - Equal Highs / Equal Lows: High-priority liquidity magnets
  - Previous Day High/Low (PDH/PDL), Previous Week High/Low (PWH/PWL)
  - Inducement (IDM): Minor structure designed to bait early entries
  - Liquidity Sweep detection: Wicks that grabbed liquidity without body close

Usage:
    python3 detect_liquidity.py <PAIR> [--timeframe 4h] [--limit 100]
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

# Tolerance for "equal" levels — levels within this % are considered equal
EQUAL_TOLERANCE_PCT = 0.08
# Minimum swing lookback for structural levels
SWING_LOOKBACK = 3


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


def detect_swing_points(candles, lookback=SWING_LOOKBACK):
    """Detect swing highs and lows."""
    highs = []
    lows = []
    for i in range(lookback, len(candles) - lookback):
        is_sh = all(
            candles[i]["high"] > candles[i - j]["high"]
            and candles[i]["high"] > candles[i + j]["high"]
            for j in range(1, lookback + 1)
        )
        if is_sh:
            highs.append(
                {
                    "index": i,
                    "price": candles[i]["high"],
                    "time": candles[i]["time_str"],
                    "type": "swing_high",
                }
            )

        is_sl = all(
            candles[i]["low"] < candles[i - j]["low"] and candles[i]["low"] < candles[i + j]["low"]
            for j in range(1, lookback + 1)
        )
        if is_sl:
            lows.append(
                {
                    "index": i,
                    "price": candles[i]["low"],
                    "time": candles[i]["time_str"],
                    "type": "swing_low",
                }
            )
    return highs, lows


def find_equal_levels(points, tolerance_pct=EQUAL_TOLERANCE_PCT):
    """
    Find clusters of equal highs or equal lows.
    Two or more swing points within tolerance% of each other = equal level.
    Triple+ equals = HIGHEST priority liquidity target.
    """
    if len(points) < 2:
        return []

    clusters = []
    used = set()

    for i, p1 in enumerate(points):
        if i in used:
            continue
        cluster = [p1]
        used.add(i)

        for j, p2 in enumerate(points):
            if j in used or j <= i:
                continue
            pct_diff = abs(p1["price"] - p2["price"]) / p1["price"] * 100
            if pct_diff <= tolerance_pct:
                cluster.append(p2)
                used.add(j)

        if len(cluster) >= 2:
            avg_price = sum(p["price"] for p in cluster) / len(cluster)
            clusters.append(
                {
                    "level": avg_price,
                    "count": len(cluster),
                    "points": cluster,
                    "type": cluster[0]["type"],
                    "priority": "EXTREME" if len(cluster) >= 3 else "HIGH",
                    "first_time": cluster[0]["time"],
                    "last_time": cluster[-1]["time"],
                }
            )

    return clusters


def find_previous_day_levels(candles):
    """Find Previous Day High/Low from daily candle data."""
    # Group candles by date
    days = {}
    for c in candles:
        date_key = c["time_str"][:10]  # YYYY-MM-DD
        if date_key not in days:
            days[date_key] = {"high": c["high"], "low": c["low"]}
        else:
            days[date_key]["high"] = max(days[date_key]["high"], c["high"])
            days[date_key]["low"] = min(days[date_key]["low"], c["low"])

    sorted_days = sorted(days.items())
    if len(sorted_days) < 2:
        return None

    prev_day = sorted_days[-2]
    return {
        "date": prev_day[0],
        "pdh": prev_day[1]["high"],
        "pdl": prev_day[1]["low"],
    }


def detect_inducement(swing_highs, swing_lows, candles):
    """
    Detect Inducement (IDM) levels.

    Inducement = a minor structural break that occurs just before a major POI.
    It's designed to bait traders into entering early, then sweep their stops.

    Detection: A small swing that barely breaks a previous level,
    creating a minor structure that will be swept.
    """
    inducements = []

    # Look for minor swing highs that form just below major resistance
    for i in range(1, len(swing_highs)):
        sh = swing_highs[i]
        prev_sh = swing_highs[i - 1]

        # Minor break: current high is only slightly above previous
        if sh["price"] > prev_sh["price"]:
            overshoot_pct = (sh["price"] - prev_sh["price"]) / prev_sh["price"] * 100
            if 0 < overshoot_pct < 0.5:  # Very small overshoot = inducement
                inducements.append(
                    {
                        "type": "IDM_HIGH",
                        "price": sh["price"],
                        "time": sh["time"],
                        "index": sh["index"],
                        "original_level": prev_sh["price"],
                        "overshoot_pct": overshoot_pct,
                        "description": (
                            f"Minor high ${sh['price']:.2f} barely exceeds "
                            f"${prev_sh['price']:.2f} — likely inducement trap"
                        ),
                    }
                )

    # Same for lows
    for i in range(1, len(swing_lows)):
        sl = swing_lows[i]
        prev_sl = swing_lows[i - 1]

        if sl["price"] < prev_sl["price"]:
            overshoot_pct = (prev_sl["price"] - sl["price"]) / prev_sl["price"] * 100
            if 0 < overshoot_pct < 0.5:
                inducements.append(
                    {
                        "type": "IDM_LOW",
                        "price": sl["price"],
                        "time": sl["time"],
                        "index": sl["index"],
                        "original_level": prev_sl["price"],
                        "overshoot_pct": overshoot_pct,
                        "description": (
                            f"Minor low ${sl['price']:.2f} barely undercuts "
                            f"${prev_sl['price']:.2f} — likely inducement trap"
                        ),
                    }
                )

    return inducements


def detect_sweeps(candles, liquidity_levels):
    """
    Detect liquidity sweeps: candles that wicked through a level
    but did NOT close their body beyond it.
    """
    sweeps = []

    for level in liquidity_levels:
        level_price = (
            level["price"]
            if isinstance(level, dict) and "price" in level
            else level.get("level", 0)
        )
        if level_price == 0:
            continue

        for i, c in enumerate(candles):
            # Bullish sweep (sweep of SSL): wick goes below level, body stays above
            if c["low"] < level_price and min(c["open"], c["close"]) > level_price:
                sweeps.append(
                    {
                        "type": "SWEEP_SSL",
                        "level_swept": level_price,
                        "wick_low": c["low"],
                        "body_low": min(c["open"], c["close"]),
                        "index": i,
                        "time": c["time_str"],
                        "description": (
                            f"Wick swept SSL at ${level_price:.2f} but body held above — "
                            "bullish reversal signal"
                        ),
                    }
                )

            # Bearish sweep (sweep of BSL): wick goes above level, body stays below
            if c["high"] > level_price and max(c["open"], c["close"]) < level_price:
                sweeps.append(
                    {
                        "type": "SWEEP_BSL",
                        "level_swept": level_price,
                        "wick_high": c["high"],
                        "body_high": max(c["open"], c["close"]),
                        "index": i,
                        "time": c["time_str"],
                        "description": (
                            f"Wick swept BSL at ${level_price:.2f} but body held below — "
                            "bearish reversal signal"
                        ),
                    }
                )

    # Deduplicate (keep first sweep per level)
    seen = set()
    unique = []
    for s in sweeps:
        key = (s["type"], round(s["level_swept"], 2))
        if key not in seen:
            seen.add(key)
            unique.append(s)

    return unique


def print_liquidity_report(
    pair,
    timeframe,
    candles,
    swing_highs,
    swing_lows,
    equal_highs,
    equal_lows,
    pdl_data,
    inducements,
    sweeps,
):
    """Format and print the full liquidity mapping report."""
    current_price = candles[-1]["close"]

    print("\n╔══════════════════════════════════════════════════════════════╗")
    print(f"║  LIQUIDITY MAP — {pair} {timeframe.upper():<40}    ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"   Current Price: ${current_price:,.2f}")

    # BSL — Liquidity ABOVE price
    bsl_levels = [sh for sh in swing_highs if sh["price"] > current_price]
    print("\n── 🔼 BUY-SIDE LIQUIDITY (Above Price) — Targets for Longs / Traps for Shorts ──")
    if bsl_levels:
        for lvl in sorted(bsl_levels, key=lambda x: x["price"])[:8]:
            dist = (lvl["price"] - current_price) / current_price * 100
            print(f"   BSL │ ${lvl['price']:>12,.2f} │ +{dist:.2f}% away │ {lvl['time']}")
    else:
        print("   No BSL levels above current price in this range")

    if equal_highs:
        print("\n   ⚡ EQUAL HIGHS (High-Priority BSL Magnets):")
        for eq in sorted(equal_highs, key=lambda x: x["level"]):
            if eq["level"] > current_price:
                dist = (eq["level"] - current_price) / current_price * 100
                print(
                    f"   EQH │ ${eq['level']:>12,.2f} │ {eq['count']}x touches │ "
                    f"Priority: {eq['priority']} │ +{dist:.2f}% away"
                )

    # SSL — Liquidity BELOW price
    ssl_levels = [sl for sl in swing_lows if sl["price"] < current_price]
    print("\n── 🔽 SELL-SIDE LIQUIDITY (Below Price) — Targets for Shorts / Traps for Longs ──")
    if ssl_levels:
        for lvl in sorted(ssl_levels, key=lambda x: x["price"], reverse=True)[:8]:
            dist = (current_price - lvl["price"]) / current_price * 100
            print(f"   SSL │ ${lvl['price']:>12,.2f} │ -{dist:.2f}% away │ {lvl['time']}")
    else:
        print("   No SSL levels below current price in this range")

    if equal_lows:
        print("\n   ⚡ EQUAL LOWS (High-Priority SSL Magnets):")
        for eq in sorted(equal_lows, key=lambda x: x["level"], reverse=True):
            if eq["level"] < current_price:
                dist = (current_price - eq["level"]) / current_price * 100
                print(
                    f"   EQL │ ${eq['level']:>12,.2f} │ {eq['count']}x touches │ "
                    f"Priority: {eq['priority']} │ -{dist:.2f}% away"
                )

    # PDH/PDL
    if pdl_data:
        print(f"\n── 📅 PREVIOUS DAY LEVELS ({pdl_data['date']}) ──")
        pdh_dist = (pdl_data["pdh"] - current_price) / current_price * 100
        pdl_dist = (current_price - pdl_data["pdl"]) / current_price * 100
        print(f"   PDH │ ${pdl_data['pdh']:>12,.2f} │ {pdh_dist:+.2f}% away")
        print(f"   PDL │ ${pdl_data['pdl']:>12,.2f} │ {-pdl_dist:+.2f}% away")

    # Inducement
    if inducements:
        print("\n── 🪤 INDUCEMENT LEVELS (Traps for Impatient Traders) ──")
        for idm in inducements[-5:]:
            print(f"   {idm['type']} │ ${idm['price']:>12,.2f} │ {idm['description']}")

    # Sweeps
    recent_sweeps = [s for s in sweeps if s["index"] >= len(candles) - 20]
    if recent_sweeps:
        print("\n── 🧹 RECENT LIQUIDITY SWEEPS ──")
        for s in recent_sweeps:
            print(f"   {s['type']} │ {s['description']}")
            print(f"   └─ Time: {s['time']} │ idx:{s['index']}")

    # Nearest liquidity targets
    all_bsl = [sh["price"] for sh in bsl_levels]
    all_ssl = [sl["price"] for sl in ssl_levels]

    print("\n── 🎯 NEAREST TARGETS ──")
    if all_bsl:
        nearest_bsl = min(all_bsl)
        bsl_dist_pct = (nearest_bsl - current_price) / current_price * 100
        print(f"   Nearest BSL: ${nearest_bsl:,.2f} (+{bsl_dist_pct:.2f}%)")
    if all_ssl:
        nearest_ssl = max(all_ssl)
        ssl_dist_pct = (current_price - nearest_ssl) / current_price * 100
        print(f"   Nearest SSL: ${nearest_ssl:,.2f} (-{ssl_dist_pct:.2f}%)")

    # JSON output
    print("\n--- LIQUIDITY_JSON_START ---")
    output = {
        "pair": pair,
        "timeframe": timeframe,
        "current_price": current_price,
        "bsl": [{"price": sh["price"], "time": sh["time"]} for sh in bsl_levels[:10]],
        "ssl": [{"price": sl["price"], "time": sl["time"]} for sl in ssl_levels[:10]],
        "equal_highs": [
            {"level": eq["level"], "count": eq["count"], "priority": eq["priority"]}
            for eq in equal_highs
        ],
        "equal_lows": [
            {"level": eq["level"], "count": eq["count"], "priority": eq["priority"]}
            for eq in equal_lows
        ],
        "pdh_pdl": pdl_data,
        "inducements": [
            {"type": i["type"], "price": i["price"], "time": i["time"]} for i in inducements[-5:]
        ],
        "recent_sweeps": [
            {"type": s["type"], "level": s["level_swept"], "time": s["time"]} for s in recent_sweeps
        ],
    }
    print(json.dumps(output, default=str))
    print("--- LIQUIDITY_JSON_END ---")


def main():
    parser = argparse.ArgumentParser(description="SMC Liquidity Pool Mapper")
    parser.add_argument("pair", help="Trading pair (e.g., BTCUSDT)")
    parser.add_argument("--timeframe", "-tf", default="4h", help="Timeframe (default: 4h)")
    parser.add_argument("--limit", type=int, default=100, help="Number of candles (default: 100)")
    parser.add_argument("--all-tf", action="store_true", help="Scan all SMC timeframes")

    args = parser.parse_args()

    if args.all_tf:
        timeframes = [("1d", 60), ("4h", 100), ("1h", 100), ("15m", 100)]
    else:
        timeframes = [(args.timeframe, args.limit)]

    for tf, lim in timeframes:
        candles = fetch_klines(args.pair, tf, lim)
        if len(candles) < 10:
            print("ERROR: Need at least 10 candles")
            continue

        swing_highs, swing_lows = detect_swing_points(candles)
        equal_highs = find_equal_levels(swing_highs)
        equal_lows = find_equal_levels(swing_lows)
        pdl_data = find_previous_day_levels(candles)
        inducements = detect_inducement(swing_highs, swing_lows, candles)

        # Build a combined list of key liquidity levels for sweep detection
        key_levels = swing_highs + swing_lows
        for eq in equal_highs + equal_lows:
            key_levels.append({"price": eq["level"], "time": eq["first_time"]})

        sweeps = detect_sweeps(candles, key_levels)

        print_liquidity_report(
            args.pair,
            tf,
            candles,
            swing_highs,
            swing_lows,
            equal_highs,
            equal_lows,
            pdl_data,
            inducements,
            sweeps,
        )


if __name__ == "__main__":
    main()
