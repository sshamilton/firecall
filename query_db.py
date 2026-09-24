#!/usr/bin/env python3
"""
CLI Query and Reporting Utility for firecall.db
Usage:
    python query_db.py                      # Print summary stats and breakdown by agency
    python query_db.py --today              # List all dispatches from today
    python query_db.py --agency "Monroe"    # Filter dispatches by agency name
    python query_db.py --search "gas leak"  # Search message transcripts for keywords
    python query_db.py --limit 20           # Show last N dispatches (default: 10)
    python query_db.py --export output.csv  # Export all records to CSV
"""

import sys
import csv
import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "firecall" / "firecall.db"

def get_connection():
    if not DB_PATH.exists():
        print(f"[ERROR]: Database not found at {DB_PATH}")
        sys.exit(1)
    return sqlite3.connect(DB_PATH)

def print_stats():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM dispatches")
        total = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM dispatches WHERE timestamp >= datetime('now', '-24 hours')")
        last_24h = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM dispatches WHERE timestamp >= datetime('now', '-7 days')")
        last_7d = cursor.fetchone()[0]

        print("=" * 68)
        print(f"         ORANGE COUNTY FIRE DISPATCH STATS ({DB_PATH.name})")
        print("=" * 68)
        print(f"Total Logged: {total:<8} | Last 24h: {last_24h:<6} | Last 7 Days: {last_7d}")
        print("-" * 68)
        print(f"{'Agency / Department':<40} | {'Count':<6} | {'Share'}")
        print("-" * 68)

        cursor.execute("""
            SELECT agency, COUNT(*) as cnt
            FROM dispatches
            GROUP BY agency
            ORDER BY cnt DESC
        """)
        for agency, count in cursor.fetchall():
            pct = (count / total * 100) if total > 0 else 0
            print(f"{agency:<40} | {count:<6} | {pct:>5.1f}%")

        print("=" * 68)
        print()

def list_dispatches(where_clause="1=1", params=(), limit=10):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT id, timestamp, agency, message, total_duration_sec, audio_url
            FROM dispatches
            WHERE {where_clause}
            ORDER BY timestamp DESC
            LIMIT ?
        """, params + (limit,))
        rows = cursor.fetchall()

        if not rows:
            print("No dispatches matching criteria.")
            return

        print(f"Showing up to {len(rows)} matching dispatch(es):")
        print("-" * 75)
        for row_id, ts, agency, msg, dur, url in rows:
            print(f"#{row_id:3d} | {ts} | {agency} ({dur:.0f}s)")
            print(f"     \"{msg}\"")
            if url:
                print(f"     Audio: {url}")
            print()

def export_csv(target_file="dispatches.csv"):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, timestamp, agency, tones, message, channel, frequency,
                   total_duration_sec, active_transmission_sec, audio_url
            FROM dispatches
            ORDER BY id ASC
        """)
        rows = cursor.fetchall()
        with open(target_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "id", "timestamp", "agency", "tones", "message",
                "channel", "frequency", "total_duration_sec",
                "active_transmission_sec", "audio_url"
            ])
            writer.writerows(rows)
        print(f"[EXPORT] Successfully saved {len(rows)} rows to {target_file}")

def main():
    args = sys.argv[1:]
    limit = 10
    for i, a in enumerate(args):
        if a in ("--limit", "-n") and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                pass

    if "--help" in args or "-h" in args:
        print(__doc__)
        return

    if "--export" in args:
        idx = args.index("--export")
        filename = args[idx + 1] if idx + 1 < len(args) else "dispatches.csv"
        export_csv(filename)
        return

    if "--today" in args:
        print("Dispatches from Today:")
        list_dispatches("timestamp >= date('now', 'start of day')", limit=limit)
        return

    if "--agency" in args:
        idx = args.index("--agency")
        if idx + 1 < len(args):
            ag = args[idx + 1]
            print(f"Dispatches for Agency matching '{ag}':")
            list_dispatches("agency LIKE ?", (f"%{ag}%",), limit=limit)
            return

    if "--search" in args:
        idx = args.index("--search")
        if idx + 1 < len(args):
            term = args[idx + 1]
            print(f"Search results for transcript matching '{term}':")
            list_dispatches("message LIKE ?", (f"%{term}%",), limit=limit)
            return

    # Default action: print stats and recent dispatches
    print_stats()
    print("Recent Dispatches:")
    list_dispatches(limit=5)

if __name__ == "__main__":
    main()
