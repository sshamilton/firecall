import re
import collections
import json
import sqlite3
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import traceback
import wave
import numpy as np
import requests
import csv
from datetime import datetime
from pathlib import Path
from scipy.signal import find_peaks

try:
    import webrtcvad
except ModuleNotFoundError:
    raise SystemExit("Run: pip install setuptools webrtcvad-wheels")

# ==========================================
#              CONFIGURATION
# ==========================================
HA_URL = "http://192.168.1.95:8123/api/webhook/ham_radio_message"
MODEL_SIZE = "medium.en"

# Hardware target: Card 1, Device 0 (Verified Kenwood TM-D750)
ALSA_DEVICE = "plughw:1,0"

VAD_AGGRESSIVENESS = 1
SOFTWARE_GAIN = 3.0

NATIVE_RATE = 48000
WHISPER_RATE = 16000
DOWNSAMPLE_FACTOR = NATIVE_RATE // WHISPER_RATE  # 3

CHUNK_DURATION_MS = 30
CHUNK_SIZE = int(NATIVE_RATE * CHUNK_DURATION_MS / 1000)  # 1440 samples
CHUNK_BYTES = CHUNK_SIZE * 2                             # 2880 bytes

SILENCE_TIMEOUT_SEC = 2.8
MAX_DISPATCH_SEC = 75.0

RECORDINGS_DIR = Path.home() / "firecall" / "recordings"
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

# Set your host/VM IP to serve audio to Home Assistant
AUDIO_BASE_URL = "http://192.168.1.21:8085"

def cleanup_old_recordings(max_days=7):
    """Deletes audio files older than max_days to prevent filling disk."""
    now = time.time()
    cutoff = now - (max_days * 86400)
    for f in RECORDINGS_DIR.glob("*.wav"):
        if f.stat().st_mtime < cutoff:
            f.unlink()

# ==========================================
#              DATABASE STORAGE
# ==========================================
DB_PATH = Path.home() / "firecall" / "firecall.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                agency TEXT,
                tones TEXT,
                message TEXT,
                raw_message TEXT,
                channel TEXT,
                frequency TEXT,
                audio_file TEXT,
                audio_url TEXT,
                total_duration_sec REAL,
                active_transmission_sec REAL,
                speech_sec REAL,
                agencies TEXT
            )
        """)
        try:
            cursor.execute("ALTER TABLE dispatches ADD COLUMN agencies TEXT")
        except sqlite3.OperationalError:
            pass
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_dispatches_agency ON dispatches(agency)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_dispatches_timestamp ON dispatches(timestamp)")
        conn.commit()

def log_dispatch_to_db(agency, tones, message, raw_message, channel, frequency, audio_file, audio_url, total_duration_sec, active_transmission_sec, speech_sec, timestamp=None, agencies=None):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            tones_json = json.dumps(tones) if tones else "[]"
            if agencies is not None:
                agencies_list = agencies
            elif agency and " / " in agency:
                agencies_list = [ag.strip() for ag in agency.split(" / ")]
            elif agency and agency != "Standard Voice / Patch" and not agency.startswith("Unknown") and not agency.startswith("Single Alert"):
                agencies_list = [agency.strip()]
            else:
                agencies_list = []
            agencies_json = json.dumps(agencies_list)

            if timestamp:
                cursor.execute("""
                    INSERT INTO dispatches (
                        timestamp, agency, tones, message, raw_message,
                        channel, frequency, audio_file, audio_url,
                        total_duration_sec, active_transmission_sec, speech_sec, agencies
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    timestamp, agency, tones_json, message, raw_message,
                    channel, frequency, str(audio_file), audio_url,
                    total_duration_sec, active_transmission_sec, speech_sec, agencies_json
                ))
            else:
                cursor.execute("""
                    INSERT INTO dispatches (
                        agency, tones, message, raw_message,
                        channel, frequency, audio_file, audio_url,
                        total_duration_sec, active_transmission_sec, speech_sec, agencies
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    agency, tones_json, message, raw_message,
                    channel, frequency, str(audio_file), audio_url,
                    total_duration_sec, active_transmission_sec, speech_sec, agencies_json
                ))
            conn.commit()
            print(f"[DB] Logged dispatch #{cursor.lastrowid} ({agency}) to {DB_PATH.name}")
    except Exception as e:
        print(f"[ERROR DB]: Failed to insert dispatch into sqlite: {e}")

def print_stats():
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM dispatches")
        total = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM dispatches WHERE timestamp >= datetime('now', '-24 hours')")
        last_24h = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM dispatches WHERE timestamp >= datetime('now', '-7 days')")
        last_7d = cursor.fetchone()[0]

        print("=" * 65)
        print(f"       ORANGE COUNTY FIRE DISPATCH STATS ({DB_PATH.name})")
        print("=" * 65)
        print(f"Total Dispatches: {total:<8} | Last 24 Hours: {last_24h:<6} | Last 7 Days: {last_7d}")
        print("-" * 65)
        print(f"{'Agency / Department':<38} | {'Count':<6} | {'Share'}")
        print("-" * 65)

        cursor.execute("SELECT agency FROM dispatches WHERE agency IS NOT NULL")
        from collections import Counter
        agency_counts = Counter()
        for (ag,) in cursor.fetchall():
            if ag:
                if ag == "Standard Voice / Patch" or ag.startswith("Unknown Station") or ag.startswith("Single Alert"):
                    parts = [ag]
                else:
                    parts = [p.strip() for p in ag.split(" / ")]
                for p in parts:
                    agency_counts[p] += 1

        for agency, count in agency_counts.most_common():
            pct = (count / total * 100) if total > 0 else 0
            print(f"{agency:<38} | {count:<6} | {pct:>5.1f}%")

        print("=" * 65)
        print("\nRecent 5 Dispatches:")
        cursor.execute("""
            SELECT timestamp, agency, message
            FROM dispatches
            ORDER BY id DESC LIMIT 5
        """)
        for ts, ag, msg in cursor.fetchall():
            excerpt = (msg[:65] + "...") if len(msg) > 65 else msg
            print(f" [{ts}] {ag}: {excerpt}")
        print()

def export_to_csv(output_path="dispatches.csv"):
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, timestamp, agency, tones, message, channel, frequency, total_duration_sec, active_transmission_sec, audio_url
            FROM dispatches
            ORDER BY id ASC
        """)
        rows = cursor.fetchall()
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "timestamp", "agency", "tones", "message", "channel", "frequency", "total_duration_sec", "active_transmission_sec", "audio_url"])
            writer.writerows(rows)
        print(f"[EXPORT] Successfully exported {len(rows)} records to {output_path}")

init_db()

running = threading.Event()
running.set()
transcription_queue = queue.Queue(maxsize=10)

# ==========================================
#        ORANGE COUNTY NY QCII DIRECTORY
# ==========================================
ORANGE_COUNTY_TONES = []
csv_path = Path("tones.csv")

if csv_path.exists():
    with open(csv_path, mode="r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for line_num, row in enumerate(reader, start=1):
            # Skip empty lines or comment lines
            if not row or row[0].strip().startswith("#"):
                continue
            try:
                agency = row[0].strip()
                tone_a = float(row[1].strip())
                tone_b = float(row[2].strip())
                ORANGE_COUNTY_TONES.append({
                    "agency": agency,
                    "tone_a": tone_a,
                    "tone_b": tone_b
                })
            except (IndexError, ValueError) as err:
                print(f"[WARN] Skipping invalid line {line_num} in tones.csv: {row} ({err})")

    # Display startup load count
    count = len(ORANGE_COUNTY_TONES)
    print("=" * 55)
    print(f"[QCII DIRECTORY] Successfully loaded {count} agencies from {csv_path.name}")
    print("=" * 55)
else:
    print(f"[WARN] {csv_path.name} not found! Starting with an empty tone directory.")

def match_department(tones, tolerance=0.022):
    """
    Matches detected audio tones against the QCII directory.
    Detects single-department calls, pager + siren sequences, and multi-agency
    mutual aid calls with multiple tone pairs (e.g. Agency A followed by Agency B).
    Returns a combined string of matched agencies separated by ' / '.
    """
    if not tones:
        return "Standard Voice / Patch"

    matched_agencies = []
    i = 0
    while i < len(tones) - 1:
        found = False
        for entry in ORANGE_COUNTY_TONES:
            a_match = abs(tones[i] - entry["tone_a"]) / entry["tone_a"] < tolerance
            b_match = abs(tones[i+1] - entry["tone_b"]) / entry["tone_b"] < tolerance
            if a_match and b_match:
                ag = entry["agency"]
                if ag not in matched_agencies:
                    matched_agencies.append(ag)
                i += 2
                found = True
                break
        if not found:
            i += 1

    if matched_agencies:
        return " / ".join(matched_agencies)
    elif len(tones) >= 2:
        return f"Unknown Station ({tones[0]} Hz / {tones[1]} Hz)"
    elif len(tones) == 1:
        return f"Single Alert Tone ({tones[0]} Hz)"
    return "Standard Voice / Patch"

def detect_alert_tones(audio_16k_bytes, sample_rate=16000):
    """
    Scans the first 25 seconds for sustained Quick-Call II paging tones.
    Captures multi-department dispatch sequences and pager + siren pairs.
    Filters out CTCSS/PL tone hum below 280 Hz.
    """
    try:
        samples = np.frombuffer(audio_16k_bytes, dtype=np.int16).astype(np.float32)
        # Scan first 25 seconds (covers multi-department sequential tone dispatches)
        max_scan_samples = min(len(samples), sample_rate * 25)
        scan_audio = samples[:max_scan_samples]

        # 200ms window with 50% overlap for high frequency resolution (~5 Hz bins)
        window_size = int(sample_rate * 0.20)
        hop_size = int(sample_rate * 0.10)
        detected_tones = []

        for start in range(0, len(scan_audio) - window_size, hop_size):
            chunk = scan_audio[start:start + window_size]
            windowed = chunk * np.hanning(len(chunk))
            fft_data = np.abs(np.fft.rfft(windowed))
            freqs = np.fft.rfftfreq(len(chunk), 1.0 / sample_rate)

            # Ignore everything below 280 Hz to wipe out the 123.0 Hz PL tone
            valid_idx = np.where((freqs >= 280) & (freqs <= 3200))[0]
            if len(valid_idx) == 0:
                continue

            band_fft = fft_data[valid_idx]
            band_freqs = freqs[valid_idx]

            peak_idx = np.argmax(band_fft)
            peak_amp = band_fft[peak_idx]
            peak_freq = band_freqs[peak_idx]

            total_energy = np.sum(fft_data[valid_idx])  # Energy inside the voice/tone band only
            
            # Purity check: lowered to 0.18 to allow for FM receiver audio distortion
            if total_energy > 0 and (peak_amp / total_energy) > 0.18:
                detected_tones.append(round(peak_freq, 1))

        # Group consecutive identical frequencies
        consolidated_tones = []
        for freq in detected_tones:
            if not consolidated_tones:
                consolidated_tones.append([freq, 1])
            else:
                # If within 15 Hz of the previous window, count as same sustained tone
                if abs(freq - consolidated_tones[-1][0]) < 15.0:
                    consolidated_tones[-1][1] += 1
                else:
                    consolidated_tones.append([freq, 1])

        # Must sustain for at least ~250ms (3 consecutive 100ms analysis hops)
        sustained = [round(float(tone[0]), 1) for tone in consolidated_tones if tone[1] >= 3]
        
        # Deduplicate consecutive tones of the same frequency
        final_tones = []
        for t in sustained:
            if not final_tones or abs(t - final_tones[-1]) > 20.0:
                final_tones.append(float(t))

        return final_tones
    except Exception as e:
        print(f"\n[DEBUG ERROR in detect_alert_tones]: {e}")
        return []

def downsample_to_16k(audio_frames):
    raw_data = b"".join(audio_frames)
    samples = np.frombuffer(raw_data, dtype=np.int16)
    return samples[::DOWNSAMPLE_FACTOR].tobytes()

# ==========================================
#        DISPATCH CLASSIFICATION RULES
# ==========================================
KNOWN_AGENCIES = {
    'goshen', 'middletown', 'washington heights', 'vails gate', 'port jervis',
    'florida', 'bullville', 'warwick', 'goodwill', 'monroe', 'orange lake',
    'mechanicstown', 'new hampton', 'south blooming grove', 'central woodbury',
    'woodbury', 'walden', 'highland falls', 'cornwall', 'new windsor',
    'greenville', 'silver lake', 'circleville', 'pocatello', 'chester',
    'cronomer valley', 'washingtonville', 'coldenham', 'montgomery',
    'salisbury mills', 'cuddebackville', 'west point', 'fort montgomery',
    'maybrook', 'middle hope', 'slate hill', 'johnson', 'unionville',
    'otisville', 'marlboro', 'minisink', 'pine bush', 'vales gate',
    'winona lake', 'campbell hall', 'tuxedo', 'greenwood lake', 'pine island',
    'sparrow bush', 'howells', 'harriman', 'lakeside', 'huguenot'
}

DISPATCH_KEYWORDS = {
    # Emergency / incident types
    'fire', 'alarm', 'smoke', 'odor', 'gas', 'leak', 'mva', 'accident',
    'rollover', 'wires', 'burning', 'medical', 'ems', 'ambulance',
    'rescue', 'structural', 'appliance', 'detector', 'activation',
    'hazard', 'spill', 'co alarm', 'police', 'sirens', 'thruway',
    # Operations & dispatch commands
    'respond', 'responding', 'response', 'resound', 'resounded', 'resounding',
    'department', 'fd', 'engine', 'ladder', 'truck', 'tanker', 'squad',
    'mutual aid', 'automatic response', 'standby', 'cover', 'relocate',
    'cancel', 'canceled', 'investigators', 'fire control', 'duty chief',
    'chief', 'battalion', 'fire police', 'service', 'clear', 'time out',
    'timed out', 'time', 'box', 'cross', 'county 911', 'dispatch',
    'dispatched', 'last call', 'emergency',
    # Road / location designators
    'road', 'street', 'avenue', 'lane', 'drive', 'route', 'court', 'way',
    'highway', 'trail', 'parkway', 'turnpike'
}

HALLUCINATIONS_EXACT = {
    'the end', 'the end.', 'the end of the day',
    'music', 'music.', 'outro', 'outro.',
    'oh', 'oh.', 'gosh', 'gosh.', 'what', 'what?', 'h',
    'thank you', 'thank you.', 'thank you very much', 'thank you very much.',
    'subtitles', 'subtitles by', 'watching', 'thanks for watching',
    'beep', 'phone ringing', 'bell rings', 'loud noise', 'punch', 'shs'
}

def clean_dispatch_text(text):
    if not text:
        return ""
    # Strip bracketed/parenthesized/asterisk sound tags (e.g. *phone rings*, [music])
    cleaned = re.sub(r'[*\[(][^*\])]*[*\])]', ' ', text)
    # Strip non-ASCII / foreign script hallucinations (e.g. Chinese characters)
    cleaned = re.sub(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u0400-\u04ff]', ' ', cleaned)
    # Strip leading punctuation/symbols
    cleaned = re.sub(r'^[\s.\-_:,;]+', '', cleaned)
    # Normalize whitespace
    return re.sub(r'\s+', ' ', cleaned).strip()

def is_real_fire_call(text, tones=None, agency_name=None, transmission_sec=None, speech_sec=None):
    """
    Evaluates whether decoded transmission is an authentic fire/EMS dispatch
    versus a squelch break, noise, test, or one-off transmission.
    """
    has_known_agency = bool(
        agency_name
        and agency_name != 'Standard Voice / Patch'
        and not agency_name.startswith('Unknown Station')
    )
    has_tones = bool(tones and len(tones) >= 2)

    # 1. Transmission duration check (< 1.0s is a squelch break or blip, unless known agency tone matched)
    if transmission_sec is not None and transmission_sec < 1.0 and not has_known_agency:
        return False, f"transmission < 1.0s ({transmission_sec:.2f}s squelch break)"

    if not text or not text.strip():
        return False, "empty transcription"

    cleaned = clean_dispatch_text(text)
    lower_cleaned = cleaned.lower()

    # 2. Check if alphanumeric content exists
    if not re.search(r'[a-zA-Z0-9]', lower_cleaned):
        return False, f"no alphanumeric content ({repr(text)})"

    # 3. Exact noise / hallucination check
    if lower_cleaned in HALLUCINATIONS_EXACT:
        return False, f"hallucination/noise phrase ({repr(lower_cleaned)})"

    words = re.findall(r'[a-zA-Z0-9]+', lower_cleaned)
    if len(words) < 2:
        return False, f"too few words ({len(words)}): {words}"

    # 4. Repetition hallucination check (e.g. 'beep beep beep' or 'fire and the fire and the fire')
    if len(words) >= 4:
        unique_ratio = len(set(words)) / len(words)
        if unique_ratio < 0.25:
            return False, f"repetitive hallucination (ratio {unique_ratio:.2f})"

    # 5. One-off radio test transmission check
    if 'this is a test' in lower_cleaned or 'radio test' in lower_cleaned or 'testing 1' in lower_cleaned:
        return False, "radio test transmission"

    # Count keywords & agency hits
    kw_hits = [kw for kw in DISPATCH_KEYWORDS if re.search(r'\b' + re.escape(kw) + r'\b', lower_cleaned)]
    agency_hits = [ag for ag in KNOWN_AGENCIES if re.search(r'\b' + re.escape(ag) + r'\b', lower_cleaned)]
    total_hits = len(kw_hits) + len(agency_hits)

    # 6. Evaluation based on tone and keywords
    if has_known_agency:
        if total_hits >= 1 or len(words) >= 6:
            return True, f"agency tone ({agency_name}) + hits={total_hits}"
        return False, f"agency tone ({agency_name}) but no dispatch keywords: {lower_cleaned[:40]}"

    if has_tones:
        if total_hits >= 1 or len(words) >= 8:
            return True, f"alert tones detected + hits={total_hits}"
        return False, f"alert tones detected but no dispatch keywords: {lower_cleaned[:40]}"

    # Standard Voice / Patch (no tones detected)
    if total_hits >= 2:
        return True, f"voice dispatch hits={total_hits} ({kw_hits[:2]} {agency_hits[:1]})"
    if total_hits == 1 and len(words) >= 8:
        return True, f"voice dispatch with 1 keyword and {len(words)} words"

    return False, f"not a fire call (hits={total_hits}, words={len(words)}): {lower_cleaned[:40]}"

# ==========================================
#              INITIALIZATION
# ==========================================
vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
audio_proc = None


# ==========================================
#          WORKER: WHISPER & TONES
# ==========================================
WHISPER_GPU_URL = "http://192.168.1.22:8001/v1/audio/transcriptions"

def worker_transcribe():
    print("[DEBUG Worker] Background transcription thread started.")
    counter = 0
    while running.is_set():
        try:
            queue_item = transcription_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        if isinstance(queue_item, tuple):
            audio_frames, meta = queue_item
        elif isinstance(queue_item, dict):
            audio_frames = queue_item.get("frames", [])
            meta = queue_item
        else:
            audio_frames = queue_item
            meta = {}

        counter += 1
        t_start = time.time()
        print(f"\n[DEBUG Worker #{counter}] Dequeued audio buffer ({len(audio_frames)} chunks)...")

        try:
            # 1. Generate clean timestamped filename: YYYYMMDD_HHMMSS.wav
            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"dispatch_{timestamp_str}.wav"
            save_path = RECORDINGS_DIR / filename
            audio_16k = downsample_to_16k(audio_frames)

            # 2. Tone analysis
            tones_found = detect_alert_tones(audio_16k)
            agency_name = match_department(tones_found)

            print("\n" + "="*50)
            print(f"[TONE ANALYSIS]: Detected Tones: {tones_found}")
            print(f"[AGENCY MATCH]:  {agency_name}")
            print("="*50)

            # 3. Save WAV (16kHz mono)
            with wave.open(str(save_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(WHISPER_RATE)
                wf.writeframes(audio_16k)

            # 4. Whisper STT (Offloaded to Vulkan GPU endpoint)
            print(f"[DEBUG Worker #{counter}] Offloading Whisper STT to GPU server...")
            prompt_context = (
                "Orange County 911 fire and EMS dispatch. Respond for a structural fire, "
                "gas line struck, ambulance, MVA, mutual aid. Engine, Ladder, Tanker. "
                "Silver Lake, Middletown, Wallkill, Goshen, Newburgh, Warwick, Port Jervis, Vails Gate, "
                "Salisbury Mills, Monroe, Washingtonville, Cornwall, Cornwall-on-Hudson"
            )

            text = ""
            try:
                with open(str(save_path), "rb") as f:
                    files = {"file": (filename, f, "audio/wav")}
                    data = {
                        "model": "large-v3-turbo",
                        "prompt": prompt_context,
                        "temperature": "0.0",
                        "response_format": "json"
                    }
                    # Send audio to the whisper-vulkan container
                    resp = requests.post(WHISPER_GPU_URL, files=files, data=data, timeout=15.0)

                if resp.status_code == 200:
                    text = resp.json().get("text", "").strip()
                else:
                    print(f"[ERROR Worker #{counter}] Whisper GPU server returned HTTP {resp.status_code}: {resp.text}")

            except requests.exceptions.RequestException as net_err:
                print(f"[ERROR Worker #{counter}] Failed to connect to Whisper GPU endpoint: {net_err}")

            t_elapsed = round(time.time() - t_start, 2)
            print(f"[DEBUG Worker #{counter}] Transcription completed in {t_elapsed}s.")

            transmission_sec = meta.get("transmission_sec")
            speech_sec = meta.get("speech_sec")
            is_fire_call, reason = is_real_fire_call(
                text,
                tones=tones_found,
                agency_name=agency_name,
                transmission_sec=transmission_sec,
                speech_sec=speech_sec
            )

            if is_fire_call:
                cleaned_text = clean_dispatch_text(text)
                print(f"\n[DECODED DISPATCH]: {cleaned_text}\n")
                audio_url = f"{AUDIO_BASE_URL}/{filename}"
                if " / " in agency_name:
                    agencies_list = [ag.strip() for ag in agency_name.split(" / ")]
                elif agency_name != "Standard Voice / Patch" and not agency_name.startswith("Unknown") and not agency_name.startswith("Single Alert"):
                    agencies_list = [agency_name.strip()]
                else:
                    agencies_list = []

                payload = {
                    "agency": agency_name,
                    "agencies": agencies_list,
                    "tones": [float(t) for t in tones_found],
                    "message": cleaned_text,
                    "raw_message": text,
                    "frequency": "154.205 MHz",
                    "channel": "Orange County Fire Paging",
                    "audio_file": str(save_path),
                    "audio_url": audio_url
                }

                # 5. Log to SQLite Database
                log_dispatch_to_db(
                    agency=agency_name,
                    tones=[float(t) for t in tones_found],
                    message=cleaned_text,
                    raw_message=text,
                    channel="Orange County Fire Paging",
                    frequency="154.205 MHz",
                    audio_file=str(save_path),
                    audio_url=audio_url,
                    total_duration_sec=round(len(audio_16k) / (WHISPER_RATE * 2), 1),
                    active_transmission_sec=round(transmission_sec, 2) if transmission_sec is not None else None,
                    speech_sec=round(speech_sec, 2) if speech_sec is not None else None,
                    agencies=agencies_list
                )

                # 6. Webhook Post with strict timeout
                try:
                    print(f"[DEBUG Worker #{counter}] Posting to Home Assistant: {HA_URL}...")
                    resp = requests.post(HA_URL, json=payload, timeout=(2.0, 4.0))
                    print(f" -> Sent alert to Home Assistant (Status: {resp.status_code})")
                except requests.exceptions.RequestException as req_err:
                    print(f" -> Home Assistant webhook error/timeout: {req_err}")
            else:
                print(f"[DEBUG Worker #{counter}] Squelch break / artifact ignored ({reason}): '{text}'")

        except Exception as e:
            print(f"\n[CRITICAL ERROR in worker_transcribe]: {e}")
            traceback.print_exc()
        finally:
            transcription_queue.task_done()
            print(f"[DEBUG Worker #{counter}] Finished processing. Ready for next call.\n")
        cleanup_old_recordings(max_days=7)

# ==========================================
#         CLEAN SHUTDOWN HANDLER
# ==========================================
def shutdown_handler(sig, frame):
    print("\n\n[Shutting down cleanly via SIGINT...]")
    running.clear()
    try:
        if audio_proc is not None:
            audio_proc.terminate()
            audio_proc.kill()
    except Exception:
        pass
    print("[Exited successfully]")
    os._exit(0)

signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)


def spawn_arecord():
    """Spawns or restarts the arecord capture process."""
    cmd = [
        "arecord",
        "-D", ALSA_DEVICE,
        "-f", "S16_LE",
        "-r", str(NATIVE_RATE),
        "-c", "1",
        "-t", "raw",
        "-q",
        "--buffer-size=192000"  # Expanded hardware ring buffer to prevent EIO drops
    ]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=CHUNK_BYTES * 20
    )

def main():
    global audio_proc
    threading.Thread(target=worker_transcribe, daemon=True).start()

    audio_proc = spawn_arecord()

    print(f"\nMonitoring Orange County Fire (154.205 MHz via {ALSA_DEVICE})... (Ctrl+C to exit)")

    pre_buffer = collections.deque(maxlen=15)
    recording = False
    voiced_frames = []
    last_speech_time = 0.0
    record_start_time = 0.0
    speech_frames_count = 0
    last_heartbeat = time.time()
    frame_counter = 0

    while running.is_set():
        raw_frame = audio_proc.stdout.read(CHUNK_BYTES)
        
        # Handle arecord death or I/O error automatically
        if not raw_frame or len(raw_frame) < CHUNK_BYTES:
            retcode = audio_proc.poll()
            if retcode is not None:
                err_msg = audio_proc.stderr.read().decode('utf-8', errors='ignore').strip()
                print(f"\n[WARNING]: arecord dropped ({err_msg}). Reconnecting audio interface in 1s...")
                try:
                    audio_proc.terminate()
                    audio_proc.kill()
                    audio_proc.wait(timeout=2.0) 
                except Exception:
                    pass

                subprocess.run(["killall", "-9", "arecord"], stderr=subprocess.DEVNULL)
                time.sleep(1.0)
                audio_proc = spawn_arecord()
                print("[RECONNECTED]: Audio capture stream restored.")
                continue
            time.sleep(0.005)
            continue

        frame_counter += 1
        current_time = time.time()

        if current_time - last_heartbeat > 30.0:
            last_heartbeat = current_time
            q_size = transcription_queue.qsize()
            # print(f"\n[Audio Loop Heartbeat] Active | Frames: {frame_counter} | Pending in Queue: {q_size}")

        samples = np.frombuffer(raw_frame, dtype=np.int16).astype(np.float32)

        if SOFTWARE_GAIN != 1.0:
            samples = samples * SOFTWARE_GAIN
            samples = np.clip(samples, -32768, 32767)

        boosted_frame = samples.astype(np.int16).tobytes()
        rms = int(np.sqrt(np.mean(samples**2)))
        is_speech = vad.is_speech(boosted_frame, NATIVE_RATE)

        if rms > 50:
            status = "SIGNAL DETECTED" if is_speech else "STATIC/NOISE"
            print(f"\r[Live Monitor] Level: {rms:<6} | VAD State: {status:<15}", end="", flush=True)

        if not recording:
            pre_buffer.append(boosted_frame)
            if is_speech:
                recording = True
                record_start_time = current_time
                last_speech_time = current_time
                speech_frames_count = 1
                print("\n[--> Squelch Open: Recording Dispatch...]")
                voiced_frames = list(pre_buffer)
                pre_buffer.clear()
        else:
            voiced_frames.append(boosted_frame)
            if is_speech:
                last_speech_time = current_time
                speech_frames_count += 1

            silence_duration = current_time - last_speech_time
            total_duration = current_time - record_start_time

            if silence_duration >= SILENCE_TIMEOUT_SEC or total_duration >= MAX_DISPATCH_SEC:
                recording = False
                transmission_duration = max(0.0, last_speech_time - record_start_time)
                speech_duration = speech_frames_count * (CHUNK_DURATION_MS / 1000.0)
                print(f"\n[--< Squelch Closed: Queuing {round(total_duration, 1)}s of audio (active: {round(transmission_duration, 1)}s) for processing...]")
                if len(voiced_frames) > 15:
                    try:
                        meta = {
                            "transmission_sec": transmission_duration,
                            "speech_sec": speech_duration,
                            "total_sec": total_duration
                        }
                        transcription_queue.put_nowait((list(voiced_frames), meta))
                    except queue.Full:
                        print("\n[WARNING]: Transcription queue is full! Dropping chunk.")
                voiced_frames = []
                pre_buffer.clear()
                speech_frames_count = 0

if __name__ == "__main__":
    if "--stats" in sys.argv:
        print_stats()
        sys.exit(0)

    if "--export-csv" in sys.argv:
        csv_file = sys.argv[2] if len(sys.argv) > 2 else "dispatches.csv"
        export_to_csv(csv_file)
        sys.exit(0)

    main()

