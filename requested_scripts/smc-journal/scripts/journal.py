#!/usr/bin/env python3
"""
journal.py — SMC Signal Journal & Performance Tracker

Logs every signal, records trade outcomes, and provides performance analytics.

Usage:
    python3 journal.py log --pair BTCUSDT --direction LONG --entry 96500 ...
    python3 journal.py outcome --signal-id <ID> --result WIN_TP2
    python3 journal.py stats [--period 7d]
    python3 journal.py list [--limit 10]
    python3 journal.py review
"""

import argparse
import datetime as dt
import json
import os
import uuid
from pathlib import Path

# Data storage
WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE", Path.home() / ".openclaw" / "workspace"))
DATA_DIR = WORKSPACE / "data"
JOURNAL_FILE = DATA_DIR / "smc_journal.json"


def ensure_data():
    """Create data directory and journal file if they don't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not JOURNAL_FILE.exists():
        JOURNAL_FILE.write_text(json.dumps({"signals": [], "settings": {"version": 1}}, indent=2))


def load_journal():
    """Load journal data from file."""
    ensure_data()
    try:
        return json.loads(JOURNAL_FILE.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return {"signals": [], "settings": {"version": 1}}


def save_journal(data):
    """Save journal data to file."""
    ensure_data()
    JOURNAL_FILE.write_text(json.dumps(data, indent=2, default=str))


# ─── Command: log ────────────────────────────────────────────────────────────
def cmd_log(args):
    """Log a new signal."""
    journal = load_journal()

    signal_id = str(uuid.uuid4())[:8]
    timestamp = dt.datetime.utcnow().isoformat() + "Z"

    signal = {
        "id": signal_id,
        "timestamp": timestamp,
        "pair": args.pair.upper(),
        "direction": args.direction.upper(),
        "entry": args.entry,
        "stop_loss": args.sl,
        "tp1": args.tp1,
        "tp2": args.tp2,
        "rr": args.rr,
        "confidence": args.confidence.upper() if args.confidence else "MEDIUM",
        "gates": args.gates if args.gates else "",
        "reasoning": args.reasoning if args.reasoning else "",
        "session": args.session if args.session else "",
        "result": None,  # Filled when outcome is recorded
        "actual_rr": None,
        "closed_at": None,
        "notes": "",
    }

    journal["signals"].append(signal)
    save_journal(journal)

    print("✅ Signal logged successfully")
    print(f"   ID:        {signal_id}")
    print(f"   Pair:      {signal['pair']}")
    print(f"   Direction: {signal['direction']}")
    print(f"   Entry:     ${signal['entry']}")
    print(f"   SL:        ${signal['stop_loss']}")
    print(f"   TP1:       ${signal['tp1']}")
    print(f"   TP2:       ${signal['tp2']}")
    print(f"   R:R:       {signal['rr']}")
    print(f"   Timestamp: {timestamp}")


# ─── Command: outcome ────────────────────────────────────────────────────────
def cmd_outcome(args):
    """Record the outcome of a signal."""
    journal = load_journal()

    # Find signal by ID or most recent for the pair
    target = None
    if args.signal_id:
        for s in journal["signals"]:
            if s["id"] == args.signal_id:
                target = s
                break
    else:
        # Find most recent signal without an outcome
        for s in reversed(journal["signals"]):
            if s["result"] is None and (not args.pair or s["pair"] == args.pair.upper()):
                target = s
                break

    if not target:
        print("❌ No matching signal found.")
        print("   Use --signal-id <ID> or ensure there's an open signal.")
        return

    target["result"] = args.result.upper()
    target["actual_rr"] = args.actual_rr if args.actual_rr else ""
    target["closed_at"] = dt.datetime.utcnow().isoformat() + "Z"
    target["notes"] = args.notes if args.notes else ""

    save_journal(journal)

    emoji = {"WIN_TP1": "✅", "WIN_TP2": "🏆", "LOSS": "❌", "BREAKEVEN": "⚖️", "SKIPPED": "⏭️"}.get(
        target["result"], "📝"
    )

    print(f"{emoji} Outcome recorded for signal {target['id']}")
    print(f"   Pair:      {target['pair']} {target['direction']}")
    print(f"   Result:    {target['result']}")
    print(f"   Actual RR: {target['actual_rr']}")


# ─── Command: stats ──────────────────────────────────────────────────────────
def cmd_stats(args):
    """Show performance statistics."""
    journal = load_journal()
    signals = journal["signals"]

    # Filter by period
    period_days = 9999
    if args.period:
        if args.period.endswith("d"):
            period_days = int(args.period[:-1])
        elif args.period == "all":
            period_days = 9999

    cutoff = dt.datetime.utcnow() - dt.timedelta(days=period_days)
    filtered = [s for s in signals if s["timestamp"] >= cutoff.isoformat()]

    total = len(filtered)
    with_result = [s for s in filtered if s["result"] is not None]
    wins = [s for s in with_result if s["result"] in ("WIN_TP1", "WIN_TP2")]
    losses = [s for s in with_result if s["result"] == "LOSS"]
    breakeven = [s for s in with_result if s["result"] == "BREAKEVEN"]
    skipped = [s for s in with_result if s["result"] == "SKIPPED"]
    pending = [s for s in filtered if s["result"] is None]

    taken = [s for s in with_result if s["result"] != "SKIPPED"]
    win_rate = (len(wins) / len(taken) * 100) if taken else 0

    print("\n╔══════════════════════════════════════════════════════════════╗")
    print(f"║  PERFORMANCE REPORT — Last {args.period or 'all time':<35}    ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    print("\n── Overall ──")
    print(f"   Total Signals:     {total}")
    print(f"   Taken:             {len(taken)}")
    print(f"   Skipped:           {len(skipped)}")
    print(f"   Pending:           {len(pending)}")
    print(f"   Wins (TP1+TP2):    {len(wins)}")
    print(f"   Losses:            {len(losses)}")
    print(f"   Breakeven:         {len(breakeven)}")
    print(f"   Win Rate:          {win_rate:.1f}%")

    # By pair
    pairs = set(s["pair"] for s in filtered)
    if pairs:
        print("\n── By Pair ──")
        for pair in sorted(pairs):
            p_taken = [s for s in taken if s["pair"] == pair]
            p_wins = [s for s in wins if s["pair"] == pair]
            p_wr = (len(p_wins) / len(p_taken) * 100) if p_taken else 0
            print(
                f"   {pair:<10} │ {len(p_wins)}W / {len(p_taken) - len(p_wins)}L │ "
                f"{p_wr:.0f}% win rate"
            )

    # By direction
    for direction in ["LONG", "SHORT"]:
        d_taken = [s for s in taken if s["direction"] == direction]
        d_wins = [s for s in wins if s["direction"] == direction]
        d_wr = (len(d_wins) / len(d_taken) * 100) if d_taken else 0
        if d_taken:
            print(f"\n── {direction} Signals ──")
            print(f"   Taken: {len(d_taken)} │ Wins: {len(d_wins)} │ Win Rate: {d_wr:.0f}%")

    # By confidence
    for conf in ["HIGH", "MEDIUM", "LOW"]:
        c_taken = [s for s in taken if s.get("confidence") == conf]
        c_wins = [s for s in wins if s.get("confidence") == conf]
        c_wr = (len(c_wins) / len(c_taken) * 100) if c_taken else 0
        if c_taken:
            print(f"\n── {conf} Confidence Signals ──")
            print(f"   Taken: {len(c_taken)} │ Wins: {len(c_wins)} │ Win Rate: {c_wr:.0f}%")

    # Losing pattern analysis
    if losses:
        print("\n── ❌ Losing Signal Analysis ──")
        loss_pairs = {}
        for loss in losses:
            loss_pairs[loss["pair"]] = loss_pairs.get(loss["pair"], 0) + 1
        for pair, count in sorted(loss_pairs.items(), key=lambda x: x[1], reverse=True):
            print(f"   {pair}: {count} losses")

        loss_sessions = {}
        for loss in losses:
            sess = loss.get("session", "unknown")
            loss_sessions[sess] = loss_sessions.get(sess, 0) + 1
        if loss_sessions:
            print(f"   By session: {loss_sessions}")

    # Recommendations
    print("\n── 💡 Recommendations ──")
    if win_rate >= 60:
        print(
            f"   ✅ Win rate is healthy ({win_rate:.0f}%). Consider maintaining current approach."
        )
    elif win_rate >= 40:
        print(
            f"   ⚠️  Win rate is borderline ({win_rate:.0f}%). "
            "Review losing setups for common failure patterns."
        )
    elif taken:
        print(
            f"   ❌ Win rate is low ({win_rate:.0f}%). "
            "Consider tightening gate validation criteria."
        )

    consecutive_losses = 0
    for s in reversed(taken):
        if s["result"] == "LOSS":
            consecutive_losses += 1
        else:
            break
    if consecutive_losses >= 3:
        print(
            f"   🛑 {consecutive_losses} CONSECUTIVE LOSSES — "
            "System should be paused per risk management rules."
        )


# ─── Command: list ───────────────────────────────────────────────────────────
def cmd_list(args):
    """List recent signals."""
    journal = load_journal()
    signals = journal["signals"]
    limit = args.limit or 10

    print(f"\n── Recent Signals (Last {limit}) ──\n")
    print(f"{'ID':<10} {'Time':<22} {'Pair':<10} {'Dir':<6} {'Entry':>10} {'Result':<12}")
    print(f"{'─' * 75}")

    for s in signals[-limit:]:
        result = s["result"] or "PENDING"
        emoji = {
            "WIN_TP1": "✅",
            "WIN_TP2": "🏆",
            "LOSS": "❌",
            "BREAKEVEN": "⚖️",
            "SKIPPED": "⏭️",
            "PENDING": "⏳",
        }.get(result, "")
        print(
            f"{s['id']:<10} {s['timestamp'][:19]:<22} {s['pair']:<10} "
            f"{s['direction']:<6} ${s['entry']:>9} {emoji} {result:<12}"
        )


# ─── Command: review ─────────────────────────────────────────────────────────
def cmd_review(args):
    """Generate a weekly review analysis."""
    print("\n╔══════════════════════════════════════════════════════════════╗")
    print("║  WEEKLY REVIEW — SMC Signal Performance                     ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    # Run stats for 7 days
    args.period = "7d"
    cmd_stats(args)

    journal = load_journal()
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=7)
    recent = [
        s
        for s in journal["signals"]
        if s["timestamp"] >= cutoff.isoformat() and s["result"] is not None
    ]

    if recent:
        print("\n── This Week's Signals Detail ──")
        for s in recent:
            emoji = {
                "WIN_TP1": "✅",
                "WIN_TP2": "🏆",
                "LOSS": "❌",
                "BREAKEVEN": "⚖️",
                "SKIPPED": "⏭️",
            }.get(s["result"], "")
            print(f"\n   {emoji} {s['pair']} {s['direction']} — {s['result']}")
            print(
                f"      Entry: ${s['entry']} │ SL: ${s['stop_loss']} │ "
                f"TP1: ${s['tp1']} │ TP2: ${s['tp2']}"
            )
            if s.get("reasoning"):
                print(f"      Reasoning: {s['reasoning'][:80]}...")
            if s.get("notes"):
                print(f"      Notes: {s['notes']}")


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="SMC Signal Journal")
    subparsers = parser.add_subparsers(dest="command")

    # log
    p_log = subparsers.add_parser("log", help="Log a new signal")
    p_log.add_argument("--pair", required=True)
    p_log.add_argument("--direction", required=True)
    p_log.add_argument("--entry", type=float, required=True)
    p_log.add_argument("--sl", type=float, required=True)
    p_log.add_argument("--tp1", type=float, required=True)
    p_log.add_argument("--tp2", type=float, required=True)
    p_log.add_argument("--rr", default="")
    p_log.add_argument("--confidence", default="MEDIUM")
    p_log.add_argument("--gates", default="")
    p_log.add_argument("--reasoning", default="")
    p_log.add_argument("--session", default="")

    # outcome
    p_out = subparsers.add_parser("outcome", help="Record signal outcome")
    p_out.add_argument("--signal-id", default="")
    p_out.add_argument("--pair", default="")
    p_out.add_argument(
        "--result", required=True, choices=["WIN_TP1", "WIN_TP2", "LOSS", "BREAKEVEN", "SKIPPED"]
    )
    p_out.add_argument("--actual-rr", default="")
    p_out.add_argument("--notes", default="")

    # stats
    p_stats = subparsers.add_parser("stats", help="Show performance stats")
    p_stats.add_argument("--period", default="30d", help="Period: 7d, 30d, 90d, all")

    # list
    p_list = subparsers.add_parser("list", help="List recent signals")
    p_list.add_argument("--limit", type=int, default=10)

    # review
    subparsers.add_parser("review", help="Weekly performance review")

    args = parser.parse_args()

    if args.command == "log":
        cmd_log(args)
    elif args.command == "outcome":
        cmd_outcome(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "review":
        cmd_review(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
