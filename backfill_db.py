#!/usr/bin/env python3
"""
Backfills firecall.db with historical dispatches parsed from /var/log/firecall/dispatch.log
matched against existing WAV recordings.
"""

import os
import re
import glob
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from firecall import (
    DB_PATH,
    init_db,
    clean_dispatch_text,
    is_real_fire_call,
    AUDIO_BASE_URL
)

def backfill():
    init_db()
    log_path = Path("/var/log/firecall/dispatch.log")
    if not log_path.exists():
        print(f"[ERROR]: {log_path} not found.")
        return

    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        log_text = f.read()

    entries = re.split(r'\[DEBUG Worker #\d+\] Dequeued audio buffer', log_text)[1:]
    
    # Map wav sizes to file paths
    recordings_dir = Path.home() / "firecall" / "recordings"
    size_to_wav = {}
    for p in recordings_dir.glob("*.wav"):
        size_to_wav[p.stat().st_size] = p

    inserted = 0
    skipped_noise = 0
    already_present = 0

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        for idx, entry in enumerate(entries, 1):
            chunks_m = re.search(r'\((\d+)\s+chunks\)', entry)
            agency_m = re.search(r'\[AGENCY MATCH\]:\s*(.*?)\n', entry)
            tones_m = re.search(r'\[TONE ANALYSIS\]:\s*Detected Tones:\s*(\[.*?\])', entry)
            decoded_m = re.search(r'\[DECODED DISPATCH\]:\s*(.*?)(?=\n\s*\[DEBUG Worker|\n\s*-> Sent alert|\Z)', entry, re.DOTALL)
            ignored_m = re.search(r'Squelch tail / artifact ignored:\s*(.*?)(?=\n\s*\[DEBUG Worker|\Z)', entry, re.DOTALL)

            chunks = int(chunks_m.group(1)) if chunks_m else 0
            agency = agency_m.group(1).strip() if agency_m else "Standard Voice / Patch"
            tones_str = tones_m.group(1) if tones_m else ""
            raw_text = decoded_m.group(1).strip() if decoded_m else (ignored_m.group(1).strip() if ignored_m else "")

            # Parse tones into floats
            tones = [float(x) for x in re.findall(r'(\d+(?:\.\d+)?)', tones_str)]

            # Locate WAV file and timestamp
            expected_size = chunks * 960 + 44
            wav_path = size_to_wav.get(expected_size)
            
            timestamp = None
            filename = ""
            audio_url = ""
            if wav_path:
                filename = wav_path.name
                audio_url = f"{AUDIO_BASE_URL}/{filename}"
                # dispatch_20260924_140853.wav -> 2026-09-24 14:08:53
                ts_m = re.search(r'dispatch_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.wav', filename)
                if ts_m:
                    timestamp = f"{ts_m.group(1)}-{ts_m.group(2)}-{ts_m.group(3)} {ts_m.group(4)}:{ts_m.group(5)}:{ts_m.group(6)}"

            if not timestamp:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Check if this dispatch is an authentic fire call
            duration_sec = round(chunks * 0.03, 1)
            is_call, reason = is_real_fire_call(raw_text, tones=tones, agency_name=agency)

            if not is_call:
                skipped_noise += 1
                continue

            # Check if already in database (by audio_file or timestamp+agency)
            cursor.execute("SELECT id FROM dispatches WHERE audio_file = ? OR (timestamp = ? AND agency = ?)", 
                           (str(wav_path) if wav_path else filename, timestamp, agency))
            if cursor.fetchone():
                already_present += 1
                continue

            cleaned_message = clean_dispatch_text(raw_text)

            cursor.execute("""
                INSERT INTO dispatches (
                    timestamp, agency, tones, message, raw_message,
                    channel, frequency, audio_file, audio_url,
                    total_duration_sec, active_transmission_sec, speech_sec
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                timestamp,
                agency,
                json.dumps(tones),
                cleaned_message,
                raw_text,
                "Orange County Fire Paging",
                "154.205 MHz",
                str(wav_path) if wav_path else filename,
                audio_url,
                duration_sec,
                duration_sec,
                duration_sec
            ))
            inserted += 1

        conn.commit()

    print(f"[BACKFILL COMPLETE]")
    print(f" - Inserted into firecall.db: {inserted}")
    print(f" - Already present:          {already_present}")
    print(f" - Filtered out noise/blips:  {skipped_noise}")

if __name__ == "__main__":
    backfill()
