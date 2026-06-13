#!/usr/bin/env python3
"""
full_scan.py — Complete SMC Multi-Timeframe Scan Orchestrator

Runs ALL analysis scripts in the correct sequence for a full 5-layer SMC scan.
This is the script OpenClaw calls when the user says "Scan BTCUSDT for setups"
or when a cron job triggers a scheduled market scan.

The output is structured text that OpenClaw's LLM reads, interprets,
applies the 5-gate filter to, and generates signals from.

Usage:
    python3 full_scan.py <PAIR>
    python3 full_scan.py --watchlist            # Scan all watchlist pairs
    python3 full_scan.py <PAIR> --quick         # Quick scan (skip LTF)
"""

import argparse
import datetime as dt
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MARKET_DATA_SCRIPT = os.path.join(
    SCRIPT_DIR, "..", "..", "smc-market-data", "scripts", "fetch_binance.py"
)

# If market data script is in the same directory (flat install), fall back
if not os.path.exists(MARKET_DATA_SCRIPT):
    MARKET_DATA_SCRIPT = os.path.join(SCRIPT_DIR, "fetch_binance.py")

DEFAULT_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]


def run_script(script_name, args_list, label=""):
    """Run a Python script and capture its output."""
    script_path = os.path.join(SCRIPT_DIR, script_name)
    if not os.path.exists(script_path):
        print(f"⚠️  Script not found: {script_path}")
        return ""

    cmd = [sys.executable, script_path, *args_list]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env={**os.environ})
        output = result.stdout
        if result.stderr:
            output += f"\n[STDERR]: {result.stderr}"
        return output
    except subprocess.TimeoutExpired:
        return f"⚠️  {label} timed out after 60 seconds"
    except Exception as e:
        return f"⚠️  Error running {label}: {e}"


def full_scan(pair, quick=False):
    """Execute full 5-layer SMC analysis on a single pair."""
    pair = pair.upper()
    now = dt.datetime.utcnow()

    print("")
    print(f"╔{'═' * 70}╗")
    print(f"║{'':^70}║")
    print(f"║{'🏦  FULL SMC ANALYSIS SCAN':^70}║")
    print(f"║{pair:^70}║")
    print(f"║{now.strftime('%Y-%m-%d %H:%M UTC'):^70}║")
    print(f"║{'':^70}║")
    print(f"╚{'═' * 70}╝")

    # ─── LAYER 1: Data Acquisition ───────────────────────────────────────
    print(f"\n{'━' * 72}")
    print("  LAYER 1: DATA ACQUISITION")
    print(f"{'━' * 72}")

    # Fetch derivatives data first (fastest)
    print("\n[1/5] Fetching derivatives data...")
    deriv_output = run_script("derivatives_data.py", [pair], "Derivatives")
    print(deriv_output)

    # ─── LAYER 2: HTF State Machine ─────────────────────────────────────
    print(f"\n{'━' * 72}")
    print("  LAYER 2: HTF STATE MACHINE (Daily + 4H)")
    print(f"{'━' * 72}")

    print("\n[2/5] Analyzing market structure (HTF)...")
    # Run structure on Daily and 4H
    struct_daily = run_script(
        "detect_structure.py", [pair, "--timeframe", "1d", "--limit", "60"], "Structure Daily"
    )
    print(struct_daily)

    struct_4h = run_script(
        "detect_structure.py", [pair, "--timeframe", "4h", "--limit", "100"], "Structure 4H"
    )
    print(struct_4h)

    # ─── LAYER 3: MTF POI Validation ────────────────────────────────────
    print(f"\n{'━' * 72}")
    print("  LAYER 3: MTF POI VALIDATION (1H + 15m)")
    print(f"{'━' * 72}")

    print("\n[3/5] Scanning for Fair Value Gaps...")
    fvg_4h = run_script("detect_fvg.py", [pair, "--timeframe", "4h"], "FVG 4H")
    print(fvg_4h)

    fvg_1h = run_script("detect_fvg.py", [pair, "--timeframe", "1h"], "FVG 1H")
    print(fvg_1h)

    if not quick:
        fvg_15m = run_script("detect_fvg.py", [pair, "--timeframe", "15m"], "FVG 15m")
        print(fvg_15m)

    print("\n[4/5] Identifying Order Blocks...")
    ob_4h = run_script("detect_ob.py", [pair, "--timeframe", "4h"], "OB 4H")
    print(ob_4h)

    ob_1h = run_script("detect_ob.py", [pair, "--timeframe", "1h"], "OB 1H")
    print(ob_1h)

    print("\n[5/5] Mapping liquidity pools...")
    liq_4h = run_script("detect_liquidity.py", [pair, "--timeframe", "4h"], "Liquidity 4H")
    print(liq_4h)

    liq_1h = run_script("detect_liquidity.py", [pair, "--timeframe", "1h"], "Liquidity 1H")
    print(liq_1h)

    # ─── LAYER 4: LTF Execution Status ──────────────────────────────────
    if not quick:
        print(f"\n{'━' * 72}")
        print("  LAYER 4: LTF TRIGGER STATUS (5m)")
        print(f"{'━' * 72}")

        struct_5m = run_script(
            "detect_structure.py", [pair, "--timeframe", "5m", "--limit", "100"], "Structure 5m"
        )
        print(struct_5m)

        fvg_5m = run_script("detect_fvg.py", [pair, "--timeframe", "5m"], "FVG 5m")
        print(fvg_5m)

    # ─── Summary ─────────────────────────────────────────────────────────
    print(f"\n{'━' * 72}")
    print(f"  SCAN COMPLETE — {pair}")
    print(f"{'━' * 72}")
    print("""
┌─────────────────────────────────────────────────────────────────────┐
│ IMPORTANT: The raw structural data above is for the LLM to         │
│ interpret. Apply the 5-Gate Filter to generate signals:             │
│                                                                     │
│  Gate 1: Does the POI contain a valid OB or Breaker Block?         │
│  Gate 2: Is there an FVG confirming displacement?                  │
│  Gate 3: Was liquidity swept before the POI formed?                │
│  Gate 4: Do funding rate & OI kinematics align?                    │
│  Gate 5: Is this during London/NY session with PO3 alignment?      │
│                                                                     │
│  ALL 5 gates must PASS for a valid signal.                         │
│  Minimum 1:3 R:R required.                                        │
│  Max 1% equity risk per trade.                                     │
└─────────────────────────────────────────────────────────────────────┘
""")


def main():
    parser = argparse.ArgumentParser(description="Full SMC Scan Orchestrator")
    parser.add_argument("pair", nargs="?", help="Trading pair (e.g., BTCUSDT)")
    parser.add_argument("--watchlist", action="store_true", help="Scan all watchlist pairs")
    parser.add_argument(
        "--quick", action="store_true", help="Quick scan — skip LTF analysis (faster)"
    )
    parser.add_argument("--pairs", nargs="+", help="Custom list of pairs to scan")

    args = parser.parse_args()

    pairs = []
    if args.watchlist:
        pairs = DEFAULT_WATCHLIST
    elif args.pairs:
        pairs = [p.upper() for p in args.pairs]
    elif args.pair:
        pairs = [args.pair.upper()]
    else:
        parser.print_help()
        print("\nExamples:")
        print("  python3 full_scan.py BTCUSDT")
        print("  python3 full_scan.py --watchlist")
        print("  python3 full_scan.py --pairs BTCUSDT ETHUSDT SOLUSDT")
        print("  python3 full_scan.py BTCUSDT --quick")
        return

    for pair in pairs:
        full_scan(pair, quick=args.quick)
        if len(pairs) > 1:
            print(f"\n{'═' * 72}")
            print(f"{'═' * 72}\n")


if __name__ == "__main__":
    main()
