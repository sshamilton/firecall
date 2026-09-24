# Firecall

**Firecall** is an automated fire and EMS dispatch monitoring system designed for Orange County, NY (154.205 MHz). It captures live RF audio from a Kenwood TM-D750 radio (via ALSA), detects Motorola Quick-Call II (QCII) paging alert tones, transcribes the voice dispatches using a high-performance GPU-accelerated Whisper model, verifies authentic emergency calls, records everything into a local SQLite database, and pushes real-time notification alerts with audio playback links to Home Assistant.

---

## Features

- **Continuous Audio Capture**: Streams raw audio from ALSA (`plughw:1,0`) at 48 kHz using an auto-recovering `arecord` pipeline.
- **Voice Activity Detection (VAD)**: Utilizes `webrtcvad` with software gain and silence timeout tracking to detect carrier openings and buffer audio.
- **Quick-Call II (QCII) Tone Decoder**: Scans the first 10 seconds of each transmission using FFT spectral analysis to detect paging tones, filtering out CTCSS/PL tones (below 280 Hz) and matching tone pairs against `tones.csv`.
- **GPU-Accelerated Speech-to-Text**: Offloads audio to an OpenAI Whisper API endpoint (`large-v3-turbo`) with customized emergency dispatch domain context prompts.
- **Intelligent Dispatch Classification**: Filters out momentary squelch breaks (< 1.0s active transmission), background RF static, mic tests, and Whisper hallucinations (e.g., `"."`, `"The End"`, `"Music"`), while ensuring genuine calls ending with dispatcher phrases like *"Thank you"* are never dropped.
- **Home Assistant Integration**: Sends rich JSON webhook payloads to Home Assistant containing agency name, tone frequencies, cleaned transcript message, frequency, and direct audio stream URLs.
- **SQLite Database Logging**: Automatically stores all verified dispatches in `firecall.db` with indexed timestamps, tone frequencies, transcripts, and duration metrics.
- **CLI Analytics & Search Tool**: Includes `query_db.py` to search transcripts, filter by department, analyze regional volume, and export to CSV.

---

## Architecture & Data Flow

```
[ Radio / Receiver (154.205 MHz) ]
              │
              ▼
    [ ALSA plughw:1,0 ]
              │
       (48 kHz S16_LE)
              │
    [ arecord Pipe & VAD ] ── (Squelch / Silence Windowing)
              │
    [ Downsample to 16 kHz ]
              │
              ├─────────────────────────────┐
              ▼                             ▼
   [ Tone FFT Detection ]        [ Save 16 kHz Mono WAV ]
   (Matches tones.csv)                      │
              │                             ▼
              │                  [ Whisper GPU STT API ]
              │                  (large-v3-turbo Engine)
              │                             │
              └──────────────┬──────────────┘
                             ▼
               [ Dispatch Classifier ]
         (Validates tones, keywords & duration)
                             │
              ┌──────────────┴──────────────┐
              ▼                             ▼
    [ Local SQLite DB ]          [ Home Assistant Webhook ]
       (firecall.db)               (Push Notification + Audio)
```

---

## File Structure

```
├── firecall.py       # Main service: audio capture, tone detection, STT worker, DB logging
├── query_db.py       # CLI reporting, search, and export tool for firecall.db
├── backfill_db.py    # One-off backfill tool to import historical logs into SQLite
├── tones.csv         # Quick-Call II frequency table (Agency, Tone A Hz, Tone B Hz)
├── firecall.db       # SQLite database (auto-created, gitignored)
├── recordings/       # Timestamped WAV recordings (auto-managed, 7-day retention)
└── README.md         # Documentation
```

---

## Database Schema (`firecall.db`)

Dispatches are stored in the `dispatches` table:

| Column | Type | Description |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY` | Auto-incrementing identifier |
| `timestamp` | `DATETIME` | Time transmission occurred (`YYYY-MM-DD HH:MM:SS`) |
| `agency` | `TEXT` | Matched agency (e.g., `Monroe`, `Walden`, `Standard Voice / Patch`) |
| `tones` | `TEXT` | JSON array of detected tone frequencies (e.g., `[1530.0, 1430.0]`) |
| `message` | `TEXT` | Cleaned transcript sent to Home Assistant |
| `raw_message` | `TEXT` | Unmodified transcription output from Whisper |
| `channel` | `TEXT` | Channel description (`Orange County Fire Paging`) |
| `frequency` | `TEXT` | Radio frequency (`154.205 MHz`) |
| `audio_file` | `TEXT` | Local filesystem path to the saved `.wav` file |
| `audio_url` | `TEXT` | HTTP URL for audio playback in Home Assistant |
| `total_duration_sec` | `REAL` | Total recorded audio duration (seconds) |
| `active_transmission_sec`| `REAL` | Active speech / carrier duration before silence timeout |
| `speech_sec` | `REAL` | Accumulated voice-active frames duration |

---

## CLI Commands & Usage

### 1. View Summary Statistics & Agency Breakdown
Displays total dispatches, counts for the last 24 hours / 7 days, and call distribution across agencies:
```bash
python query_db.py
# or
python firecall.py --stats
```

### 2. View Calls from Today
```bash
python query_db.py --today
```

### 3. Filter by Department / Agency
```bash
python query_db.py --agency "Monroe"
python query_db.py --agency "Walden"
python query_db.py --agency "Chester"
```

### 4. Search Transcripts for Incident Types
Search for specific emergency keywords, street names, or incident categories:
```bash
python query_db.py --search "gas leak"
python query_db.py --search "motor vehicle accident"
python query_db.py --search "structure fire"
python query_db.py --search "automatic alarm"
```

### 5. Control Number of Results
Adjust the maximum number of results displayed with `--limit` or `-n`:
```bash
python query_db.py --agency "Warwick" --limit 25
```

### 6. Export to CSV
Export all historical dispatches to a CSV file for spreadsheets or external analysis:
```bash
python query_db.py --export dispatches.csv
# or
python firecall.py --export-csv dispatches.csv
```

---

## Service Management

Firecall runs as a background systemd service under `fire-dispatch.service`:

```bash
# Check service status
systemctl status fire-dispatch.service

# View live monitoring and transcription logs
tail -f /var/log/firecall/dispatch.log

# Gracefully restart the service
kill -SIGINT $(pgrep -f firecall.py)
# (systemd will automatically restart the process with updated code)
```

---

## Configuration

Settings can be customized near the top of `firecall.py`:

```python
HA_URL = "http://192.168.1.95:8123/api/webhook/ham_radio_message"
WHISPER_GPU_URL = "http://192.168.1.22:8001/v1/audio/transcriptions"
AUDIO_BASE_URL = "http://192.168.1.21:8085"
ALSA_DEVICE = "plughw:1,0"
SILENCE_TIMEOUT_SEC = 2.8
MAX_DISPATCH_SEC = 75.0
```
