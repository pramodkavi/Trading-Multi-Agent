#!/usr/bin/env python3
"""
fetch_binance.py — Binance Futures market data fetcher for SMC analysis.

Usage:
    python3 fetch_binance.py klines <PAIR> <TIMEFRAME> [--limit N]
    python3 fetch_binance.py derivatives <PAIR>
    python3 fetch_binance.py mtf <PAIR>
    python3 fetch_binance.py depth <PAIR> [--limit N]

All output is structured text designed for LLM interpretation.
Uses READ-ONLY API keys — cannot place orders.
"""

import argparse
import datetime as dt
import json
import os
import sys

# Add parent directory so we can import shared config
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from binance.client import Client
    from binance.exceptions import BinanceAPIException
except ImportError:
    print("ERROR: python-binance not installed. Run: pip install python-binance")
    sys.exit(1)

# ─── Configuration ───────────────────────────────────────────────────────────
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

TIMEFRAME_MAP = {
    "1m": Client.KLINE_INTERVAL_1MINUTE,
    "3m": Client.KLINE_INTERVAL_3MINUTE,
    "5m": Client.KLINE_INTERVAL_5MINUTE,
    "15m": Client.KLINE_INTERVAL_15MINUTE,
    "30m": Client.KLINE_INTERVAL_30MINUTE,
    "1h": Client.KLINE_INTERVAL_1HOUR,
    "2h": Client.KLINE_INTERVAL_2HOUR,
    "4h": Client.KLINE_INTERVAL_4HOUR,
    "6h": Client.KLINE_INTERVAL_6HOUR,
    "8h": Client.KLINE_INTERVAL_8HOUR,
    "12h": Client.KLINE_INTERVAL_12HOUR,
    "1d": Client.KLINE_INTERVAL_1DAY,
    "3d": Client.KLINE_INTERVAL_3DAY,
    "1w": Client.KLINE_INTERVAL_1WEEK,
    "1M": Client.KLINE_INTERVAL_1MONTH,
}

MTF_TIMEFRAMES = {
    "1d": {"limit": 60, "label": "Daily (HTF Macro)"},
    "4h": {"limit": 100, "label": "4-Hour (HTF Refinement)"},
    "1h": {"limit": 100, "label": "1-Hour (MTF Bias)"},
    "15m": {"limit": 100, "label": "15-Min (MTF POI)"},
    "5m": {"limit": 100, "label": "5-Min (LTF Trigger)"},
}


def get_client():
    """Initialize Binance client."""
    if not API_KEY or not API_SECRET:
        print("WARNING: No API keys found. Using public endpoints only.")
        print("Set BINANCE_API_KEY and BINANCE_API_SECRET environment variables.")
        return Client("", "")
    return Client(API_KEY, API_SECRET)


def format_price(val):
    """Format price string appropriately."""
    f = float(val)
    if f >= 1000:
        return f"{f:.2f}"
    elif f >= 1:
        return f"{f:.4f}"
    else:
        return f"{f:.6f}"


def format_volume(val):
    """Format volume with commas."""
    f = float(val)
    if f >= 1000:
        return f"{f:,.1f}"
    return f"{f:.4f}"


def ts_to_str(ts_ms):
    """Convert millisecond timestamp to UTC datetime string."""
    return dt.datetime.utcfromtimestamp(int(ts_ms) / 1000).strftime("%Y-%m-%d %H:%M")


# ─── Command: klines ─────────────────────────────────────────────────────────
def cmd_klines(client, pair, timeframe, limit):
    """Fetch kline/candlestick data."""
    pair = pair.upper()
    if timeframe not in TIMEFRAME_MAP:
        print(f"ERROR: Invalid timeframe '{timeframe}'.")
        print(f"Valid timeframes: {', '.join(TIMEFRAME_MAP.keys())}")
        return

    try:
        klines = client.futures_klines(symbol=pair, interval=TIMEFRAME_MAP[timeframe], limit=limit)
    except BinanceAPIException as e:
        print(f"ERROR: Binance API error: {e.message}")
        return
    except Exception as e:
        print(f"ERROR: Failed to fetch klines: {e}")
        return

    if not klines:
        print(f"No kline data returned for {pair} {timeframe}")
        return

    print(f"=== {pair} {timeframe.upper()} (Last {len(klines)} candles) ===")
    print(
        f"{'Idx':<5} {'Time (UTC)':<18} {'Open':>12} {'High':>12} "
        f"{'Low':>12} {'Close':>12} {'Volume':>14}"
    )
    print("─" * 95)

    for i, k in enumerate(klines):
        open_time = ts_to_str(k[0])
        o = format_price(k[1])
        h = format_price(k[2])
        low = format_price(k[3])
        c = format_price(k[4])
        v = format_volume(k[5])
        print(f"[{i:<3}] {open_time:<18} {o:>12} {h:>12} {low:>12} {c:>12} {v:>14}")

    # Summary stats
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    current = closes[-1]
    period_high = max(highs)
    period_low = min(lows)
    avg_volume = sum(volumes) / len(volumes)

    print("\n--- Summary ---")
    print(f"Current Close: {format_price(current)}")
    print(f"Period High:   {format_price(period_high)}")
    print(f"Period Low:    {format_price(period_low)}")
    range_abs = format_price(period_high - period_low)
    range_pct = (period_high - period_low) / period_low * 100
    print(f"Period Range:  {range_abs} ({range_pct:.2f}%)")
    print(f"Avg Volume:    {format_volume(avg_volume)}")

    # Output raw data as JSON for script piping
    print("\n--- RAW_JSON_START ---")
    raw = []
    for k in klines:
        raw.append(
            {
                "open_time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "close_time": int(k[6]),
            }
        )
    print(json.dumps(raw))
    print("--- RAW_JSON_END ---")


# ─── Command: derivatives ────────────────────────────────────────────────────
def cmd_derivatives(client, pair):
    """Fetch derivatives data: funding rate, OI, mark price."""
    pair = pair.upper()

    print(f"=== {pair} DERIVATIVES DATA ===\n")

    # Funding rate
    try:
        funding = client.futures_funding_rate(symbol=pair, limit=10)
        if funding:
            current_fr = float(funding[-1]["fundingRate"])
            prev_fr = float(funding[-2]["fundingRate"]) if len(funding) > 1 else 0
            fr_time = ts_to_str(funding[-1]["fundingTime"])

            print("── Funding Rate ──")
            print(f"Current Rate:     {current_fr:.6f} ({current_fr * 100:.4f}%)")
            print(f"Previous Rate:    {prev_fr:.6f} ({prev_fr * 100:.4f}%)")
            print(f"Last Funding At:  {fr_time} UTC")

            if current_fr > 0.0003:
                print("⚠️  EXTREME POSITIVE — Longs overcrowded, paying shorts heavily")
            elif current_fr > 0.0001:
                print("📈 Positive — Longs dominant, healthy in uptrend")
            elif current_fr < -0.0003:
                print("⚠️  EXTREME NEGATIVE — Shorts overcrowded, paying longs heavily")
            elif current_fr < -0.0001:
                print("📉 Negative — Shorts dominant, healthy in downtrend")
            else:
                print("⚖️  Neutral — No significant directional skew")

            # Recent funding history
            print(f"\nRecent Funding History (last {len(funding)}):")
            for f in funding:
                rate = float(f["fundingRate"])
                ftime = ts_to_str(f["fundingTime"])
                bar = "+" * int(abs(rate) * 10000) if rate > 0 else "-" * int(abs(rate) * 10000)
                print(f"  {ftime}  {rate:+.6f}  {bar}")
    except Exception as e:
        print(f"  Error fetching funding rate: {e}")

    # Open Interest
    try:
        oi_data = client.futures_open_interest(symbol=pair)
        oi_hist = client.futures_open_interest_hist(symbol=pair, period="5m", limit=48)

        current_oi = float(oi_data["openInterest"])

        print("\n── Open Interest ──")
        print(f"Current OI:       {current_oi:,.2f} contracts")

        if oi_hist and len(oi_hist) >= 2:
            oldest_oi = float(oi_hist[0]["sumOpenInterest"])
            newest_oi = float(oi_hist[-1]["sumOpenInterest"])
            oi_change = newest_oi - oldest_oi
            oi_pct = (oi_change / oldest_oi * 100) if oldest_oi > 0 else 0
            print(f"4H OI Change:     {oi_change:+,.2f} ({oi_pct:+.2f}%)")

            if oi_pct > 5:
                print("📈 OI RISING significantly — New positions being opened")
            elif oi_pct < -5:
                print("📉 OI FALLING significantly — Positions being closed/liquidated")
            else:
                print("⚖️  OI relatively stable")
    except Exception as e:
        print(f"  Error fetching OI: {e}")

    # Mark price and price data
    try:
        mark = client.futures_mark_price(symbol=pair)
        ticker = client.futures_ticker(symbol=pair)

        print("\n── Price Data ──")
        print(f"Mark Price:       {format_price(mark['markPrice'])}")
        print(f"Index Price:      {format_price(mark['indexPrice'])}")
        print(f"Last Price:       {format_price(mark['lastFundingRate'])}")
        print(f"24H High:         {format_price(ticker['highPrice'])}")
        print(f"24H Low:          {format_price(ticker['lowPrice'])}")
        print(f"24H Volume:       {format_volume(ticker['volume'])}")
        print(f"24H Quote Vol:    ${float(ticker['quoteVolume']):,.0f}")
        print(f"24H Price Change: {float(ticker['priceChangePercent']):+.2f}%")
    except Exception as e:
        print(f"  Error fetching price data: {e}")


# ─── Command: mtf (Multi-Timeframe) ─────────────────────────────────────────
def cmd_mtf(client, pair):
    """Fetch kline data for ALL SMC timeframes in a single call."""
    pair = pair.upper()
    print("╔══════════════════════════════════════════════════════════╗")
    print(f"║  MULTI-TIMEFRAME DATA — {pair:<30}       ║")
    print("╚══════════════════════════════════════════════════════════╝\n")

    all_data = {}

    for tf, cfg in MTF_TIMEFRAMES.items():
        try:
            klines = client.futures_klines(
                symbol=pair, interval=TIMEFRAME_MAP[tf], limit=cfg["limit"]
            )

            if not klines:
                print(f"  No data for {tf}")
                continue

            print(f"━━━ {cfg['label']} ({tf}) — Last {len(klines)} candles ━━━")

            # Print last 10 candles for readability
            display_count = min(10, len(klines))
            print(
                f"{'Idx':<5} {'Time (UTC)':<18} {'Open':>12} {'High':>12} "
                f"{'Low':>12} {'Close':>12} {'Volume':>12}"
            )

            for i in range(len(klines) - display_count, len(klines)):
                k = klines[i]
                print(
                    f"[{i:<3}] {ts_to_str(k[0]):<18} {format_price(k[1]):>12} "
                    f"{format_price(k[2]):>12} {format_price(k[3]):>12} "
                    f"{format_price(k[4]):>12} {format_volume(k[5]):>12}"
                )

            # Summary
            closes = [float(k[4]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            print(
                f"  Range: {format_price(min(lows))} — {format_price(max(highs))} "
                f"| Current: {format_price(closes[-1])}\n"
            )

            # Store for JSON output
            all_data[tf] = [
                {
                    "open_time": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                }
                for k in klines
            ]

        except BinanceAPIException as e:
            print(f"  ERROR ({tf}): {e.message}")
        except Exception as e:
            print(f"  ERROR ({tf}): {e}")

    # Also fetch derivatives
    print("\n━━━ Derivatives Snapshot ━━━")
    cmd_derivatives(client, pair)

    # Raw JSON output for script piping
    print("\n--- MTF_RAW_JSON_START ---")
    print(json.dumps(all_data))
    print("--- MTF_RAW_JSON_END ---")


# ─── Command: depth ──────────────────────────────────────────────────────────
def cmd_depth(client, pair, limit):
    """Fetch order book depth."""
    pair = pair.upper()

    try:
        depth = client.futures_order_book(symbol=pair, limit=limit)
    except Exception as e:
        print(f"ERROR: {e}")
        return

    print(f"=== {pair} ORDER BOOK (Top {limit} levels) ===\n")

    print(f"{'Price':>14} {'Quantity':>14}   │   {'Price':>14} {'Quantity':>14}")
    print(f"{'═══ BIDS (Buy) ═══':>30}   │   {'═══ ASKS (Sell) ═══':>30}")

    bids = depth.get("bids", [])[:limit]
    asks = depth.get("asks", [])[:limit]

    max_rows = max(len(bids), len(asks))
    for i in range(max_rows):
        bid_str = (
            f"{format_price(bids[i][0]):>14} {format_volume(bids[i][1]):>14}"
            if i < len(bids)
            else " " * 30
        )
        ask_str = (
            f"{format_price(asks[i][0]):>14} {format_volume(asks[i][1]):>14}"
            if i < len(asks)
            else ""
        )
        print(f"{bid_str}   │   {ask_str}")

    # Identify large walls
    if bids:
        max_bid = max(bids, key=lambda x: float(x[1]))
        print(
            f"\n🟢 Largest Bid Wall: {format_price(max_bid[0])} — "
            f"{format_volume(max_bid[1])} contracts"
        )
    if asks:
        max_ask = max(asks, key=lambda x: float(x[1]))
        print(
            f"🔴 Largest Ask Wall: {format_price(max_ask[0])} — "
            f"{format_volume(max_ask[1])} contracts"
        )


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Binance Futures Data Fetcher for SMC")
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # klines
    p_klines = subparsers.add_parser("klines", help="Fetch candlestick data")
    p_klines.add_argument("pair", help="Trading pair (e.g., BTCUSDT)")
    p_klines.add_argument("timeframe", help="Timeframe (1m,5m,15m,1h,4h,1d,...)")
    p_klines.add_argument("--limit", type=int, default=100, help="Number of candles (default: 100)")

    # derivatives
    p_deriv = subparsers.add_parser("derivatives", help="Fetch funding rate, OI, etc.")
    p_deriv.add_argument("pair", help="Trading pair")

    # mtf
    p_mtf = subparsers.add_parser("mtf", help="Fetch all SMC timeframes at once")
    p_mtf.add_argument("pair", help="Trading pair")

    # depth
    p_depth = subparsers.add_parser("depth", help="Fetch order book depth")
    p_depth.add_argument("pair", help="Trading pair")
    p_depth.add_argument("--limit", type=int, default=20, help="Number of levels (default: 20)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    client = get_client()

    if args.command == "klines":
        cmd_klines(client, args.pair, args.timeframe, args.limit)
    elif args.command == "derivatives":
        cmd_derivatives(client, args.pair)
    elif args.command == "mtf":
        cmd_mtf(client, args.pair)
    elif args.command == "depth":
        cmd_depth(client, args.pair, args.limit)


if __name__ == "__main__":
    main()
