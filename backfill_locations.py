#!/usr/bin/env python3
"""
CLI Utility to backfill and upgrade location/incident geocoding in firecall.db.

Usage:
    python backfill_locations.py                       # Fast pass: Regex + Station coordinates for all rows, then LLM + Nominatim for latest 50
    python backfill_locations.py --mode llm --limit 50 # Run LLM + Nominatim on the 50 most recent un-geocoded (or regex-only) rows
    python backfill_locations.py --mode regex          # Run Regex + Nominatim on recent rows
    python backfill_locations.py --fast-station-all    # Instantly populate all un-geocoded rows with Regex + Station coords (0 network delay)
    python backfill_locations.py --upgrade-llm 100     # Upgrade up to 100 regex-parsed rows to LLM + Nominatim
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from geocoder import DB_PATH, ensure_schema, process_dispatch


def fast_backfill_all_stations(conn: sqlite3.Connection):
    """
    Instantly populates any row missing latitude/longitude with Regex extraction + Fire Station fallback
    (skip_nominatim=True) so every single historical call in firecall.db is immediately mappable and categorized.
    """
    cursor = conn.cursor()
    rows = cursor.execute(
        "SELECT id, agency, message FROM dispatches WHERE latitude IS NULL OR incident_type IS NULL ORDER BY id DESC"
    ).fetchall()
    if not rows:
        print("[FAST BACKFILL] All rows already have initial coordinates and incident types.")
        return 0

    print(f"[FAST BACKFILL] Populating {len(rows)} historical rows with Regex incident classification & station coordinates...")
    t0 = time.time()
    updates = []
    for r_id, ag, msg in rows:
        res = process_dispatch(
            message=msg or "",
            agency=ag or "",
            mode="regex",
            conn=conn,
            rate_limit_sec=0.0,
            skip_nominatim=True,
        )
        # Mark parse_method as 'regex_fast' so we know Nominatim/LLM hasn't run on it yet
        updates.append((
            res["incident_type"],
            res["extracted_address"],
            res["municipality"],
            res["cross_streets"],
            res["latitude"],
            res["longitude"],
            res["location_precision"],
            "regex_fast",
            r_id,
        ))

    cursor.executemany(
        """
        UPDATE dispatches
        SET incident_type = ?,
            extracted_address = ?,
            municipality = ?,
            cross_streets = ?,
            latitude = ?,
            longitude = ?,
            location_precision = ?,
            parse_method = ?
        WHERE id = ?
        """,
        updates,
    )
    conn.commit()
    elapsed = round(time.time() - t0, 2)
    print(f"[FAST BACKFILL] Completed {len(updates)} rows in {elapsed}s.")
    return len(updates)


def geocode_rows(conn: sqlite3.Connection, mode: str = "auto", limit: int = 50, force_reparse: bool = False):
    """Runs full extraction (LLM or Regex) + OpenStreetMap Nominatim geocoding on up to `limit` rows."""
    cursor = conn.cursor()
    if force_reparse:
        where = "1=1"
    elif mode in ("auto", "llm"):
        where = "parse_method IS NULL OR parse_method NOT IN ('llm', 'manual')"
    else:
        where = "parse_method IS NULL OR parse_method = 'regex_fast'"

    rows = cursor.execute(
        f"""
        SELECT id, timestamp, agency, message
        FROM dispatches
        WHERE {where}
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        print(f"[GEOCODE] No dispatches needing '{mode}' geocoding found.")
        return

    print(f"[GEOCODE] Processing {len(rows)} dispatches (mode={mode})...")
    for idx, (r_id, ts, ag, msg) in enumerate(rows, start=1):
        res = process_dispatch(
            message=msg or "",
            agency=ag or "",
            mode=mode,
            conn=conn,
            rate_limit_sec=1.0,
            skip_nominatim=False,
        )
        cursor.execute(
            """
            UPDATE dispatches
            SET incident_type = ?,
                extracted_address = ?,
                municipality = ?,
                cross_streets = ?,
                latitude = ?,
                longitude = ?,
                location_precision = ?,
                parse_method = ?
            WHERE id = ?
            """,
            (
                res["incident_type"],
                res["extracted_address"],
                res["municipality"],
                res["cross_streets"],
                res["latitude"],
                res["longitude"],
                res["location_precision"],
                res["parse_method"],
                r_id,
            ),
        )
        conn.commit()
        addr_disp = res["extracted_address"] or "(Station Fallback)"
        print(
            f" [{idx}/{len(rows)}] #{r_id} ({ts}) [{res['parse_method'].upper()}] "
            f"{res['incident_type']} | {addr_disp}, {res['municipality']} -> "
            f"({res['latitude']}, {res['longitude']}) [{res['location_precision']}]"
        )


def main():
    parser = argparse.ArgumentParser(description="Backfill and geocode dispatches in firecall.db")
    parser.add_argument("--mode", choices=["auto", "llm", "regex"], default="auto", help="Extraction mode (default: auto)")
    parser.add_argument("--limit", "-n", type=int, default=40, help="Number of recent dispatches to geocode via LLM/Nominatim (default: 40)")
    parser.add_argument("--fast-station-all", action="store_true", help="Only run instant Regex + Station fallback across all rows")
    parser.add_argument("--upgrade-llm", type=int, metavar="N", help="Upgrade N most recent non-LLM rows using the LLM + Nominatim")
    parser.add_argument("--force", action="store_true", help="Force re-parse even if already parsed by LLM")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"[ERROR] Database not found at {DB_PATH}")
        sys.exit(1)

    with sqlite3.connect(DB_PATH) as conn:
        ensure_schema(conn)
        # Always ensure all rows have at least fast station fallback & incident_type first
        fast_backfill_all_stations(conn)

        if args.fast_station_all:
            return

        if args.upgrade_llm:
            geocode_rows(conn, mode="llm", limit=args.upgrade_llm, force_reparse=args.force)
        else:
            geocode_rows(conn, mode=args.mode, limit=args.limit, force_reparse=args.force)


if __name__ == "__main__":
    main()
