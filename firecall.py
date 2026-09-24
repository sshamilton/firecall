import collections
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

def match_department(tones):
    if len(tones) >= 2:
        for entry in ORANGE_COUNTY_TONES:
            a_match = abs(tones[0] - entry["tone_a"]) / entry["tone_a"] < 0.02
            b_match = abs(tones[1] - entry["tone_b"]) / entry["tone_b"] < 0.02
            if a_match and b_match:
                return entry["agency"]
        return f"Unknown Station ({tones[0]} Hz / {tones[1]} Hz)"
    elif len(tones) == 1:
        return f"Single Alert Tone ({tones[0]} Hz)"
    return "Standard Voice / Patch"

def detect_alert_tones(audio_16k_bytes, sample_rate=16000):
    """
    Scans the first 10 seconds for sustained Quick-Call II paging tones.
    Filters out CTCSS/PL tone hum below 250 Hz.
    """
    try:
        samples = np.frombuffer(audio_16k_bytes, dtype=np.int16).astype(np.float32)
        # Scan first 10 seconds (standard QCII window)
        max_scan_samples = min(len(samples), sample_rate * 10)
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
        sustained = [round(tone[0], 1) for tone in consolidated_tones if tone[1] >= 3]
        
        # Deduplicate consecutive tones of the same frequency
        final_tones = []
        for t in sustained:
            if not final_tones or abs(t - final_tones[-1]) > 20.0:
                final_tones.append(t)

        return final_tones
    except Exception as e:
        print(f"\n[DEBUG ERROR in detect_alert_tones]: {e}")
        return []

def downsample_to_16k(audio_frames):
    raw_data = b"".join(audio_frames)
    samples = np.frombuffer(raw_data, dtype=np.int16)
    return samples[::DOWNSAMPLE_FACTOR].tobytes()

# ==========================================
#              INITIALIZATION
# ==========================================
vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)

# Direct arecord capture pipe
arecord_cmd = [
    "arecord",
    "-D", ALSA_DEVICE,
    "-f", "S16_LE",
    "-r", str(NATIVE_RATE),
    "-c", "1",
    "-t", "raw",
    "-q"
]

print(f"[DEBUG] Spawning arecord pipe: {' '.join(arecord_cmd)}")
audio_proc = subprocess.Popen(
    arecord_cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    bufsize=CHUNK_BYTES * 10
)


# ==========================================
#          WORKER: WHISPER & TONES
# ==========================================
WHISPER_GPU_URL = "http://192.168.1.22:8001/v1/audio/transcriptions"

def worker_transcribe():
    print("[DEBUG Worker] Background transcription thread started.")
    counter = 0
    while running.is_set():
        try:
            audio_frames = transcription_queue.get(timeout=1.0)
        except queue.Empty:
            continue

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

            hallucinations = ["thank you", "subtitles", "watching", "mbc"]
            if text and not any(h in text.lower() for h in hallucinations):
                print(f"\n[DECODED DISPATCH]: {text}\n")
                audio_url = f"{AUDIO_BASE_URL}/{filename}"
                payload = {
                    "agency": agency_name,
                    "tones": tones_found,
                    "message": text,
                    "frequency": "154.205 MHz",
                    "channel": "Orange County Fire Paging",
                    "audio_file": str(save_path),
                    "audio_url": audio_url
                }

                # 5. Webhook Post with strict timeout
                try:
                    print(f"[DEBUG Worker #{counter}] Posting to Home Assistant: {HA_URL}...")
                    resp = requests.post(HA_URL, json=payload, timeout=(2.0, 4.0))
                    print(f" -> Sent alert to Home Assistant (Status: {resp.status_code})")
                except requests.exceptions.RequestException as req_err:
                    print(f" -> Home Assistant webhook error/timeout: {req_err}")
            else:
                print(f"[DEBUG Worker #{counter}] Squelch tail / artifact ignored: '{text}'")

        except Exception as e:
            print(f"\n[CRITICAL ERROR in worker_transcribe]: {e}")
            traceback.print_exc()
        finally:
            transcription_queue.task_done()
            print(f"[DEBUG Worker #{counter}] Finished processing. Ready for next call.\n")
        cleanup_old_recordings(max_days=7)

threading.Thread(target=worker_transcribe, daemon=True).start()


# ==========================================
#         CLEAN SHUTDOWN HANDLER
# ==========================================
def shutdown_handler(sig, frame):
    print("\n\n[Shutting down cleanly via SIGINT...]")
    running.clear()
    try:
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

audio_proc = spawn_arecord()

# ==========================================
#              MAIN AUDIO LOOP
# ==========================================
print(f"\nMonitoring Orange County Fire (154.205 MHz via {ALSA_DEVICE})... (Ctrl+C to exit)")

pre_buffer = collections.deque(maxlen=15)
recording = False
voiced_frames = []
last_speech_time = 0.0
record_start_time = 0.0
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
                audo_proc.wait(timeout=2.0) 
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
            print("\n[--> Squelch Open: Recording Dispatch...]")
            voiced_frames = list(pre_buffer)
            pre_buffer.clear()
    else:
        voiced_frames.append(boosted_frame)
        if is_speech:
            last_speech_time = current_time

        silence_duration = current_time - last_speech_time
        total_duration = current_time - record_start_time

        if silence_duration >= SILENCE_TIMEOUT_SEC or total_duration >= MAX_DISPATCH_SEC:
            recording = False
            print(f"\n[--< Squelch Closed: Queuing {round(total_duration, 1)}s of audio for processing...]")
            if len(voiced_frames) > 33:
                try:
                    transcription_queue.put_nowait(list(voiced_frames))
                except queue.Full:
                    print("\n[WARNING]: Transcription queue is full! Dropping chunk.")
            voiced_frames = []
            pre_buffer.clear()

