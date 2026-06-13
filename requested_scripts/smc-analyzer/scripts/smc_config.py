"""
smc_config.py — Shared configuration and utility functions for all SMC scripts.
Place this file alongside the other scripts in each skill's scripts/ directory,
OR in a shared location and symlink. Recommended: place in smc-analyzer/scripts/
and import from there.
"""

import datetime as dt
import os
from pathlib import Path

# ─── Binance API Credentials ─────────────────────────────────────────────────
# These are read from environment variables set by OpenClaw's skill config.
# They are READ-ONLY keys. They CANNOT place orders.
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

# ─── Default Pairs Watchlist ─────────────────────────────────────────────────
DEFAULT_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]

# ─── Timeframe Mappings ─────────────────────────────────────────────────────
# Maps human-readable timeframe strings to Binance API interval constants
TIMEFRAME_MAP = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "8h": "8h",
    "12h": "12h",
    "1d": "1d",
    "3d": "3d",
    "1w": "1w",
    "1M": "1M",
}

# Multi-timeframe config for SMC top-down analysis
MTF_CONFIG = {
    "1d": {"limit": 60, "label": "Daily (HTF Macro)"},
    "4h": {"limit": 100, "label": "4-Hour (HTF Refinement)"},
    "1h": {"limit": 100, "label": "1-Hour (MTF Bias)"},
    "15m": {"limit": 100, "label": "15-Min (MTF POI)"},
    "5m": {"limit": 100, "label": "5-Min (LTF Trigger)"},
}

# ─── SMC Analysis Parameters ────────────────────────────────────────────────
# Swing detection: minimum candles on each side to qualify as swing point
SWING_LOOKBACK = 3

# FVG minimum size as percentage of price (filters out micro-gaps)
FVG_MIN_SIZE_PCT = 0.05  # 0.05% of price

# OB: minimum displacement candle body size as percentage of price
OB_MIN_DISPLACEMENT_PCT = 0.3  # 0.3% body size for displacement

# Liquidity: tolerance for "equal" highs/lows (percentage)
EQUAL_LEVEL_TOLERANCE_PCT = 0.05  # 0.05% — levels within this are "equal"

# ─── Session Times (UTC) ────────────────────────────────────────────────────
SESSIONS = {
    "asia": {"start": 0, "end": 8, "label": "Asian Session (Accumulation)"},
    "london": {"start": 8, "end": 16, "label": "London Session (Manipulation)"},
    "newyork": {"start": 13, "end": 21, "label": "New York Session (Distribution)"},
    "cooldown": {"start": 21, "end": 24, "label": "Global Cooldown (No Trades)"},
}

# ─── Paths ──────────────────────────────────────────────────────────────────
WORKSPACE_DIR = Path(os.environ.get("OPENCLAW_WORKSPACE", Path.home() / ".openclaw" / "workspace"))
DATA_DIR = WORKSPACE_DIR / "data"
JOURNAL_FILE = DATA_DIR / "smc_journal.json"


def get_current_session() -> dict:
    """Return the current trading session based on UTC hour."""
    hour = dt.datetime.utcnow().hour
    for name, s in SESSIONS.items():
        if name == "cooldown":
            if hour >= s["start"] or hour < 0:
                return {"name": name, **s}
        elif s["start"] <= hour < s["end"]:
            return {"name": name, **s}
    return {"name": "unknown", "start": 0, "end": 0, "label": "Unknown"}


def format_price(price: float, pair: str = "") -> str:
    """Format price with appropriate decimal places."""
    if price >= 1000:
        return f"{price:.2f}"
    elif price >= 1:
        return f"{price:.4f}"
    else:
        return f"{price:.6f}"


def pct_distance(a: float, b: float) -> float:
    """Calculate percentage distance between two prices."""
    if a == 0:
        return 0.0
    return abs(a - b) / a * 100


def ensure_data_dir():
    """Create the data directory if it doesn't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
