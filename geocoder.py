#!/usr/bin/env python3
"""
Hybrid Location & Incident Extractor + OpenStreetMap (Nominatim) Geocoder for Firecall.
Supports:
  1. Primary OpenAI-compatible LLM parser (configured via .env)
  2. Automatic Regex + Whisper-phonetic normalizer fallback
  3. Local SQLite geocode cache + Orange County, NY bounded Nominatim geocoding
  4. Fire Station / Municipality coordinate fallback with deterministic jitter
"""

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
import requests

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "firecall.db"
STATIONS_PATH = BASE_DIR / "stations.json"
ENV_PATH = BASE_DIR / ".env"

# Orange County, NY + adjacent border margin bounding box
# Format for Nominatim viewbox: lon_min,lat_min,lon_max,lat_max
OC_VIEWBOX = "-74.80,41.12,-73.92,41.68"
OC_LAT_MIN, OC_LAT_MAX = 41.10, 41.70
OC_LON_MIN, OC_LON_MAX = -74.82, -73.90

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_HEADERS = {
    "User-Agent": "Firecall-OrangeCountyNY-DispatchMap/1.0 (local-dispatch-monitor)"
}

INCIDENT_CATEGORIES = [
    "Structure Fire",
    "MVA",
    "Automatic Alarm",
    "Gas / CO / Hazmat",
    "Brush / Outside Fire",
    "Medical / EMS",
    "Mutual Aid / Standby",
    "Other",
]


def load_dotenv(env_file: Path = ENV_PATH):
    """Loads key=value pairs from .env into os.environ without external dependencies."""
    if not env_file.exists():
        return
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("\"'")
                if k:
                    os.environ[k] = v
    except Exception as e:
        print(f"[WARN geocoder] Failed to read .env: {e}")


load_dotenv()


def get_llm_config():
    load_dotenv()
    enabled_str = os.getenv("FIRECALL_LLM_ENABLED", "true").lower()
    enabled = enabled_str in ("1", "true", "yes", "on")
    url = os.getenv("FIRECALL_LLM_URL", "").strip()
    model = os.getenv("FIRECALL_LLM_MODEL", "gemma-4-26B-A4B-it").strip()
    api_key = os.getenv("FIRECALL_LLM_API_KEY", "").strip()
    try:
        timeout = float(os.getenv("FIRECALL_LLM_TIMEOUT", "12.0"))
    except ValueError:
        timeout = 12.0
    return {
        "enabled": enabled and bool(url),
        "url": url,
        "model": model,
        "api_key": api_key,
        "timeout": timeout,
    }


def load_stations():
    if STATIONS_PATH.exists():
        with open(STATIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


STATIONS = load_stations()


def ensure_schema(conn: sqlite3.Connection = None):
    """Ensures dispatches table has location/incident columns and geocode_cache exists."""
    close_after = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_after = True

    cursor = conn.cursor()
    new_columns = [
        ("incident_type", "TEXT"),
        ("extracted_address", "TEXT"),
        ("municipality", "TEXT"),
        ("cross_streets", "TEXT"),
        ("latitude", "REAL"),
        ("longitude", "REAL"),
        ("location_precision", "TEXT"),
        ("parse_method", "TEXT"),
    ]
    for col_name, col_type in new_columns:
        try:
            cursor.execute(f"ALTER TABLE dispatches ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError:
            pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS geocode_cache (
            query_key TEXT PRIMARY KEY,
            latitude REAL,
            longitude REAL,
            display_name TEXT,
            precision TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dispatches_incident_type ON dispatches(incident_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dispatches_precision ON dispatches(location_precision)")
    conn.commit()

    if close_after:
        conn.close()


def resolve_station_info(agency: str, message: str = ""):
    """Finds the best matching fire station record from stations.json for an agency string or transcript."""
    if not STATIONS:
        return STATIONS.get("Orange County 911 Center")

    candidates = []
    if agency:
        parts = [p.strip() for p in re.split(r"\s+/\s+(?![^()]*\))", agency)]
        candidates.extend(parts)

    for cand in candidates:
        if cand in STATIONS:
            return STATIONS[cand]
        # Strip Duty Chief / Car / FD suffix and match
        cleaned = re.sub(r"\s+(?:Duty Chief|Fire Chief|Ambulance|Car\s+\d+|Company\s+\d+|FD).*$", "", cand, flags=re.I).strip()
        for key, info in STATIONS.items():
            key_clean = re.sub(r"\s+FD.*$", "", key, flags=re.I).strip()
            if cleaned and (cleaned.lower() == key.lower() or cleaned.lower() == key_clean.lower() or cleaned.lower() in key.lower()):
                return info

    # If agency is Standard Voice / Patch or Unknown, inspect the transcript for a department name
    if message:
        msg_lower = message.lower()
        # Check common city/department names mentioned at start of dispatch
        if "city of newburgh" in msg_lower:
            return {
                "name": "City of Newburgh Fire Department",
                "municipality": "Newburgh",
                "town": "Newburgh",
                "lat": 41.5034,
                "lon": -74.0104,
                "address": "22 Grand St, Newburgh, NY",
            }
        for key, info in STATIONS.items():
            key_clean = re.sub(r"\s+FD.*$", "", key, flags=re.I).strip()
            if len(key_clean) >= 4 and key_clean.lower() in msg_lower:
                return info

    return STATIONS.get("Orange County 911 Center", {
        "name": "Orange County 911 Center",
        "municipality": "Goshen",
        "town": "Goshen",
        "lat": 41.3885,
        "lon": -74.3515,
        "address": "22 Wells Farm Rd, Goshen, NY",
    })


def classify_incident_regex(message: str) -> str:
    """Rule-based classification of fire dispatch transcript into one of 8 incident categories."""
    if not message:
        return "Other"
    m = message.lower()

    # 1. Mutual Aid / Standby / Fill-in / FAST team (check if primary is structure fire vs pure standby)
    if any(k in m for k in ["stand by", "standby", "move up", "relocate to", "cover station"]):
        return "Mutual Aid / Standby"

    # 2. Structure / Appliance / Working Fire
    if any(k in m for k in [
        "structure fire", "structural fire", "second alarm", "third alarm", "working fire",
        "house fire", "building fire", "barn fire", "chimney fire", "dryer fire", "dryer smoking",
        "smoking dryer", "oven fire", "oven smoking", "dishwasher", "appliance fire",
        "electrical fire", "smoke in a residence", "smoking a residence", "smoke in the building",
        "smoke condition", "basement fire", "kitchen fire", "odor of something burning",
        "something burning inside", "fast team", "fasting scene", "fasting to"
    ]):
        return "Structure Fire"

    # 3. Motor Vehicle Accident (MVA)
    if any(k in m for k in [
        "motor vehicle accident", "mva", "auto accident", "vehicle accident", "roll over",
        "rollover", "car into a pole", "into a pole", "into a tree", "pedestrian struck",
        "extrication", "entrapment", "head-on", "rear-end", "tractor trailer"
    ]):
        return "MVA"

    # 4. Gas / CO / Hazmat / Wires
    if any(k in m for k in [
        "co alarm", "carbon monoxide", "gas leak", "odor of gas", "natural gas", "propane",
        "gas line", "hazmat", "fuel spill", "wires down", "transformer", "power line",
        "pole on fire", "utility pole"
    ]):
        return "Gas / CO / Hazmat"

    # 5. Brush / Outside / Vehicle Fire
    if any(k in m for k in [
        "brush fire", "woods fire", "grass fire", "mulch fire", "outside fire", "rubbish fire",
        "dumpster fire", "trash fire", "illegal burn", "unattended burn", "vehicle fire",
        "car fire", "truck fire", "vehicle smoking", "tractor fire"
    ]):
        return "Brush / Outside Fire"

    # 6. Automatic / Commercial / Residential Fire Alarm
    if any(k in m for k in [
        "automatic fire alarm", "fire alarm", "alarm sounding", "commercial fire alarm",
        "residential fire alarm", "general fire alarm", "omac", "omax", "umac",
        "smoke detector", "pull station", "waterflow", "sprinkler"
    ]):
        return "Automatic Alarm"

    # 7. Medical / EMS / Lift Assist / Rescue
    if any(k in m for k in [
        "ems", "medical", "ambulance", "landing zone", "medevac", "helicopter", "lz",
        "lift assist", "cardiac", "unconscious", "difficulty breathing", "water rescue",
        "rope rescue", "elevator rescue", "locked in"
    ]):
        return "Medical / EMS"

    if any(k in m for k in ["mutual aid", "resounded", "re-sounded", "reassounded"]):
        return "Mutual Aid / Standby"

    return "Other"


def normalize_whisper_transcript(text: str) -> str:
    """Fixes common Whisper phonetic mistranscriptions in Orange County 911 dispatches."""
    if not text:
        return ""
    s = text
    # Convert spoken digit sequences like "five zero zero" -> "500"
    digit_words = {
        "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
        "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"
    }
    def replace_spoken_digits(match):
        words = match.group(0).lower().split()
        return "".join(digit_words[w] for w in words if w in digit_words)

    s = re.sub(
        r"\b(?:(?:zero|one|two|three|four|five|six|seven|eight|nine)\s+){1,4}(?:zero|one|two|three|four|five|six|seven|eight|nine)\b",
        replace_spoken_digits,
        s,
        flags=re.I,
    )

    replacements = [
        (r"\bdriving across\b", "Drive, on the cross"),
        (r"\bwatchenacross\b", "on the cross"),
        (r"\bplaceman cross\b", "Place on the cross"),
        (r"\band acrosse\b", "on the cross"),
        (r"\bon across\b", "on the cross"),
        (r"\bcanary route\b", "County Route"),
        (r"\bcounty\s+(\d+)\b", r"County Route \1"),
        (r"\bpeach terrors\b", "Peach Terrace"),
        (r"\bpeace terrace\b", "Peach Terrace"),
        (r"\bnewlytown\b", "Neelytown"),
        (r"\bsparabush\b", "Sparrowbush"),
        (r"\bquebecville\b", "Cuddebackville"),
        (r"\bcuddebeckville\b", "Cuddebackville"),
        (r"\bsouth boone grove\b", "South Blooming Grove"),
        (r"\bcrown river valley\b", "Cronomer Valley"),
        (r"\bcroner valley\b", "Cronomer Valley"),
        (r"\bfasting scene\b", "FAST Team"),
        (r"\bfasting to\b", "FAST Team to"),
        (r"\b(?:omax|umac|omac)\b", "automatic"),
    ]
    for pat, repl in replacements:
        s = re.sub(pat, repl, s, flags=re.I)
    return s


def extract_with_regex(message: str, agency: str) -> dict:
    """Extracts incident_type, street_address, cross_streets, and municipality using normalized regex."""
    station = resolve_station_info(agency, message)
    municipality = station.get("municipality", "Goshen")
    incident_type = classify_incident_regex(message)
    norm = normalize_whisper_transcript(message)

    street_address = None
    cross_streets = None

    # Suffixes for streets/roads
    suffix_pat = (
        r"(?:Road|Rd|Street|St|Avenue|Ave|Drive|Dr|Terrace|Ter|Lane|Ln|Court|Ct|"
        r"Place|Pl|Way|Boulevard|Blvd|Trail|Trl|Turnpike|Tpke|Highway|Hwy|Circle|Cir|"
        r"Parkway|Pkwy|Pike|Extension|Ext|Hill|Loop|Plaza|Run|Path|Heights|Alley)"
    )
    route_pat = r"(?:(?:County|State|US|U\.S\.|NY)\s+)?(?:Route|Highway|Rt)\s+\d+[A-Za-z]?"

    # 1. Look for numbered street address: e.g. "number 9, Peach Terrace" or "500 Neelytown Road" or "626 County Route 22"
    num_addr_re = re.compile(
        rf"(?<!Route\s)(?<!Rt\s)(?:\bnumber\s+#?|\b#)?(\d{{1,5}})\s*,?\s+([A-Z0-9][A-Za-z0-9\.\-']+(?:\s+[A-Z0-9][A-Za-z0-9\.\-']+){{0,3}}\s+{suffix_pat}|{route_pat})\b",
        re.I,
    )
    matches = num_addr_re.findall(norm)
    for num, street in matches:
        # Filter out time/box numbers or years or "12 and Gardnerville Road"
        street_clean = re.sub(r"\s+", " ", street).strip(" ,.")
        if re.match(r"^and\s+", street_clean, re.I):
            continue
        if re.search(r"\b(?:box|alarm|engine|station|company|level|apartment|apt|building|bldg)\b", street_clean, re.I):
            continue
        street_address = f"{num} {street_clean}"
        break

    # 2. Look for intersections: "[Road A] and [Road B]"
    if not street_address:
        inter_re = re.compile(
            rf"\b([A-Z0-9][A-Za-z0-9\.\-']+(?:\s+[A-Za-z0-9\.\-']+){{0,2}}\s+{suffix_pat}|{route_pat})\s+and\s+([A-Z0-9][A-Za-z0-9\.\-']+(?:\s+[A-Za-z0-9\.\-']+){{0,2}}\s+{suffix_pat}|{route_pat})\b",
            re.I,
        )
        m_inter = inter_re.search(norm)
        if m_inter:
            street_address = f"{m_inter.group(1).strip()} & {m_inter.group(2).strip()}"

    # 3. Look for highway / interstate / route mentions: e.g. "17 eastbound ... exit 129" or "I-84"
    if not street_address:
        hwy_re = re.compile(
            r"\b((?:Interstate\s+|I-|Route\s+|US\s+|State\s+Route\s+)?(?:84|87|17[KM]?|9W|6|32|207|208|209|211|94|52|302|284|416)\s*(?:eastbound|westbound|northbound|southbound)?)\b",
            re.I,
        )
        m_hwy = hwy_re.search(norm)
        if m_hwy:
            hwy = m_hwy.group(1).strip()
            if re.match(r"^(?:17|84|87|9W|6|32|208|211)", hwy, re.I) and not re.match(r"^(?:Route|I-|Interstate)", hwy, re.I):
                hwy = f"Route {hwy}"
            street_address = hwy

    # 4. Extract cross streets ("... on the cross")
    cross_re = re.compile(
        rf"([A-Z][A-Za-z0-9\s\.\-&']+?(?:{suffix_pat}|{route_pat}))\s*,?\s*on the cross",
        re.I,
    )
    m_cross = cross_re.search(norm)
    if m_cross:
        cs = m_cross.group(1).strip(" ,.")
        # Take the last 1-2 street names before "on the cross"
        parts = [p.strip() for p in cs.split(",") if p.strip()]
        if parts:
            cross_streets = ", ".join(parts[-2:])

    # Check if standby at a station
    is_standby = incident_type == "Mutual Aid / Standby" and not street_address

    return {
        "incident_type": incident_type,
        "extracted_address": street_address,
        "municipality": municipality,
        "cross_streets": cross_streets,
        "is_standby": is_standby,
        "parse_method": "regex",
    }


LLM_SYSTEM_PROMPT = """You are an expert Orange County, NY 911 Fire/EMS CAD dispatch location parser.
You receive a radio voice transcript (produced by Whisper STT) and the alerted Fire Department agency name.

Important domain rules:
1. Dispatches usually state the call twice (e.g., "Repeating for [Agency]..."). Compare BOTH halves of the transcript to resolve garbled street numbers, street names, or landmarks!
2. Fix common Whisper phonetic errors:
   - "driving across" or "watchenacross" or "placeman cross" -> "Drive, on the cross" / "on the cross"
   - "Canary Route" -> "County Route"
   - "County 12" -> "County Route 12"
   - "Peach Terrors" / "Peace Terrace" -> "Peach Terrace"
   - "Newlytown Road" -> "Neelytown Road"
   - "South Boone Grove" -> "South Blooming Grove"
   - "Crown River Valley" / "Croner Valley" -> "Cronomer Valley"
   - "Bell's Gate" / "Bells Gate" / "Tailgate" / "Vailgate" -> "Vails Gate (New Windsor)"
   - "Sparabush" / "Sparrow Witch" -> "Sparrowbush"
   - "Quebecville" -> "Cuddebackville"
   - "OMAX" / "UMAC" -> "Automatic Fire Alarm"
   - "fasting scene" / "fasting to" -> "FAST Team"
3. Choose `incident_type` from EXACTLY one of:
   - "Structure Fire" (working fires, smoke in residence, dryer/oven/appliance fires, chimney fires, FAST team responses to fires)
   - "MVA" (motor vehicle accidents, rollovers, vehicle into pole/tree, extrication)
   - "Automatic Alarm" (automatic/commercial/residential fire alarms, alarm sounding)
   - "Gas / CO / Hazmat" (CO alarms, carbon monoxide, gas leaks, odor of gas, wires/poles down, hazmat)
   - "Brush / Outside Fire" (brush, grass, woods, mulch, dumpster, vehicle/car fires, vehicle smoking)
   - "Medical / EMS" (EMS, lift assist, landing zone/medevac, water/technical rescue)
   - "Mutual Aid / Standby" (standby at a fire station, cover assignment, relocate)
   - "Other"
4. For `extracted_address`:
   - Return a clean geocodable street address (e.g., "9 Peach Terrace" or "500 Neelytown Road" or "626 County Route 22") OR clean intersection (e.g., "County Route 12 & Gardnerville Road").
   - Strip apartment/unit numbers, building numbers, and business names from `extracted_address` so OpenStreetMap Nominatim can geocode the street.
   - If the call is only a station standby (e.g. "stand by Middle Hope Station 2") or has no street/road at all, set `extracted_address` to null and `is_standby` to true.
5. For `municipality`:
   - Identify the Orange County, NY town, city, or village for the address (e.g., "Newburgh", "Middletown", "Monroe", "Goshen", "Warwick", "Port Jervis", "Wallkill", "Montgomery", "Walden", "Maybrook", "Campbell Hall", "Slate Hill", "Cornwall", "Washingtonville", "Chester", "Harriman", "Kiryas Joel", "Pine Bush", "Sparrow Bush", "Huguenot", "Cuddebackville", "Otisville", "Highland Falls", "West Point", "Florida", "Greenwood Lake", "Tuxedo Park", "Salisbury Mills", "Bullville", "Circleville", "New Windsor").

Return ONLY a valid JSON object matching this exact schema (no markdown blocks, no extra commentary):
{
  "incident_type": "Structure Fire",
  "extracted_address": "9 Peach Terrace",
  "cross_streets": "Brooker Drive",
  "municipality": "Newburgh",
  "is_standby": false
}"""


def extract_with_llm(message: str, agency: str) -> dict | None:
    """Calls the configured OpenAI-compatible LLM endpoint to extract structured location and incident info."""
    cfg = get_llm_config()
    if not cfg["enabled"] or not message:
        return None

    station = resolve_station_info(agency, message)
    default_muni = station.get("municipality", "Goshen")

    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
        headers["x-api-key"] = cfg["api_key"]

    user_prompt = (
        f"Alerted Agency: {agency or 'Unknown'}\n"
        f"Default Agency Municipality: {default_muni}, Orange County, NY\n"
        f"Transcript: \"{message}\""
    )

    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": LLM_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 500,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }

    try:
        resp = requests.post(
            cfg["url"],
            headers=headers,
            json=payload,
            timeout=cfg["timeout"],
        )
        if resp.status_code == 404 and "model_not_found" in resp.text:
            alt_model = f"google/{cfg['model']}" if "/" not in cfg["model"] else cfg["model"].split("/", 1)[1]
            payload["model"] = alt_model
            resp = requests.post(
                cfg["url"],
                headers=headers,
                json=payload,
                timeout=cfg["timeout"],
            )
            if resp.status_code == 200:
                os.environ["FIRECALL_LLM_MODEL"] = alt_model
        if resp.status_code != 200:
            print(f"[WARN LLM] HTTP {resp.status_code} from {cfg['url']}: {resp.text[:200]}")
            return None

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            return None

        msg_obj = choices[0].get("message", {})
        content = (msg_obj.get("content") or "").strip()

        # Strip optional <think>...</think> or <|thought|> blocks if present
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
        content = re.sub(r"<\|thought\|>.*?<\|/thought\|>", "", content, flags=re.S).strip()

        # Extract valid JSON object even if multiple braces exist in text
        parsed = None
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", content):
            start_idx = match.start()
            try:
                obj, _ = decoder.raw_decode(content[start_idx:])
                if isinstance(obj, dict) and "incident_type" in obj:
                    parsed = obj
            except json.JSONDecodeError:
                continue

        if parsed is None:
            return None
        inc_type = parsed.get("incident_type")
        if inc_type not in INCIDENT_CATEGORIES:
            inc_type = classify_incident_regex(message)

        addr = parsed.get("extracted_address")
        if isinstance(addr, str):
            addr = addr.strip()
            if not addr or addr.lower() in ("null", "none", "n/a", "unknown"):
                addr = None
        else:
            addr = None

        cross = parsed.get("cross_streets")
        if isinstance(cross, str):
            cross = cross.strip()
            if not cross or cross.lower() in ("null", "none", "n/a", "unknown"):
                cross = None
        else:
            cross = None

        muni = parsed.get("municipality")
        if isinstance(muni, str) and muni.strip() and muni.strip().lower() not in ("null", "none", "unknown"):
            muni = muni.strip()
        else:
            muni = default_muni

        return {
            "incident_type": inc_type,
            "extracted_address": addr,
            "municipality": muni,
            "cross_streets": cross,
            "is_standby": bool(parsed.get("is_standby", False)),
            "parse_method": "llm",
        }
    except Exception as e:
        print(f"[WARN LLM] Exception calling LLM extractor: {e}")
        return None


def _nominatim_lookup(query: str, conn: sqlite3.Connection, rate_limit_sec: float = 1.05):
    """Queries geocode_cache first, then OpenStreetMap Nominatim within Orange County bounding box."""
    q_key = query.strip().lower()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT latitude, longitude, precision FROM geocode_cache WHERE query_key = ?",
        (q_key,),
    )
    row = cursor.fetchone()
    if row is not None:
        lat, lon, prec = row
        if lat is not None and lon is not None:
            return lat, lon, prec
        return None

    if rate_limit_sec > 0:
        time.sleep(rate_limit_sec)

    params = {
        "q": query,
        "format": "json",
        "limit": 3,
        "viewbox": OC_VIEWBOX,
        "bounded": 1,
        "countrycodes": "us",
    }
    try:
        resp = requests.get(NOMINATIM_URL, params=params, headers=NOMINATIM_HEADERS, timeout=8.0)
        if resp.status_code == 200:
            results = resp.json()
            for r in results:
                lat = float(r["lat"])
                lon = float(r["lon"])
                if OC_LAT_MIN <= lat <= OC_LAT_MAX and OC_LON_MIN <= lon <= OC_LON_MAX:
                    disp = r.get("display_name", "")
                    prec = "exact"
                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO geocode_cache
                        (query_key, latitude, longitude, display_name, precision)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (q_key, lat, lon, disp, prec),
                    )
                    conn.commit()
                    return lat, lon, prec

        # Cache negative result so we don't hammer Nominatim repeatedly for un-geocodable strings
        cursor.execute(
            """
            INSERT OR REPLACE INTO geocode_cache
            (query_key, latitude, longitude, display_name, precision)
            VALUES (?, NULL, NULL, NULL, 'miss')
            """,
            (q_key,),
        )
        conn.commit()
    except Exception as e:
        print(f"[WARN Nominatim] Failed query '{query}': {e}")

    return None


def deterministic_jitter(seed_str: str, max_offset: float = 0.0022):
    """Generates a deterministic (lat_offset, lon_offset) so fallback pins at the same station fan out cleanly."""
    h = hashlib.md5(seed_str.encode("utf-8")).digest()
    u1 = (int.from_bytes(h[:4], "big") / 0xFFFFFFFF) * 2.0 - 1.0
    u2 = (int.from_bytes(h[4:8], "big") / 0xFFFFFFFF) * 2.0 - 1.0
    return round(u1 * max_offset, 6), round(u2 * max_offset, 6)


def geocode_extracted(
    extracted_address: str | None,
    municipality: str | None,
    cross_streets: str | None,
    agency: str,
    message: str,
    conn: sqlite3.Connection,
    rate_limit_sec: float = 1.05,
    skip_nominatim: bool = False,
):
    """
    Resolves (latitude, longitude, location_precision) using:
    1. Exact address + municipality in Orange County, NY
    2. Exact address anywhere in Orange County, NY (fire district vs postal town mismatch)
    3. Street name (without house number) or primary cross street in municipality
    4. Responding Fire Department station coordinates with deterministic jitter
    """
    station = resolve_station_info(agency, message)
    muni = municipality or station.get("municipality", "Goshen")

    if extracted_address and not skip_nominatim:
        addr_clean = extracted_address.strip()
        # Normalize "&" or "and" intersection for Nominatim
        queries = []

        if "&" in addr_clean or " and " in addr_clean.lower():
            parts = [p.strip() for p in re.split(r"\s*(?:&|\band\b)\s*", addr_clean, flags=re.I) if p.strip()]
            if len(parts) >= 2:
                # Try first road in municipality
                queries.append((f"{parts[0]}, {muni}, Orange County, NY", "exact"))
                queries.append((f"{parts[0]}, Orange County, NY", "approximate"))
                queries.append((f"{parts[1]}, {muni}, Orange County, NY", "approximate"))
        else:
            queries.append((f"{addr_clean}, {muni}, NY", "exact"))
            queries.append((f"{addr_clean}, Orange County, NY", "exact"))

            # Also try street without house number if house number wasn't in OSM
            street_only = re.sub(r"^\d+[\-A-Za-z]?\s+", "", addr_clean).strip()
            if street_only and street_only != addr_clean:
                queries.append((f"{street_only}, {muni}, NY", "approximate"))
                queries.append((f"{street_only}, Orange County, NY", "approximate"))

        # Try cross street if main street isn't found
        if cross_streets:
            first_cross = cross_streets.split(",")[0].split("&")[0].strip()
            if first_cross and len(first_cross) > 4:
                queries.append((f"{first_cross}, {muni}, NY", "approximate"))

        seen_q = set()
        for q_str, expected_prec in queries:
            q_low = q_str.lower()
            if q_low in seen_q:
                continue
            seen_q.add(q_low)
            res = _nominatim_lookup(q_str, conn, rate_limit_sec=rate_limit_sec)
            if res is not None:
                lat, lon, _ = res
                # Add very tiny jitter (0.00015 deg ~ 15m) if approximate road match so pins on the same road don't overlap 100%
                if expected_prec == "approximate":
                    j_lat, j_lon = deterministic_jitter(f"{agency}:{message}", max_offset=0.0003)
                    lat = round(lat + j_lat, 6)
                    lon = round(lon + j_lon, 6)
                return lat, lon, expected_prec

    # Fallback to Fire Station coordinates
    base_lat = station.get("lat", 41.3885)
    base_lon = station.get("lon", -74.3515)
    j_lat, j_lon = deterministic_jitter(f"{agency}:{message}", max_offset=0.0022)
    return round(base_lat + j_lat, 6), round(base_lon + j_lon, 6), "station_fallback"


def process_dispatch(
    message: str,
    agency: str,
    mode: str = "auto",
    conn: sqlite3.Connection = None,
    rate_limit_sec: float = 1.05,
    skip_nominatim: bool = False,
) -> dict:
    """
    Full hybrid extraction + geocoding pipeline for a single dispatch.
    mode:
      - 'auto': Try LLM first (if enabled in .env), automatically fall back to Regex
      - 'llm': Try LLM first, fall back to Regex
      - 'regex': Use Regex only (fast bulk backfill without LLM calls)
    """
    close_after = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_after = True

    ensure_schema(conn)

    extracted = None
    if mode in ("auto", "llm"):
        extracted = extract_with_llm(message, agency)

    if extracted is None:
        extracted = extract_with_regex(message, agency)

    lat, lon, precision = geocode_extracted(
        extracted_address=extracted.get("extracted_address"),
        municipality=extracted.get("municipality"),
        cross_streets=extracted.get("cross_streets"),
        agency=agency,
        message=message,
        conn=conn,
        rate_limit_sec=rate_limit_sec,
        skip_nominatim=skip_nominatim,
    )

    if close_after:
        conn.close()

    return {
        "incident_type": extracted["incident_type"],
        "extracted_address": extracted["extracted_address"],
        "municipality": extracted["municipality"],
        "cross_streets": extracted["cross_streets"],
        "latitude": lat,
        "longitude": lon,
        "location_precision": precision,
        "parse_method": extracted["parse_method"],
    }


if __name__ == "__main__":
    # Quick self-test on the 5 most recent dispatches
    with sqlite3.connect(DB_PATH) as conn:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT id, agency, message FROM dispatches ORDER BY id DESC LIMIT 5"
        ).fetchall()
        for r_id, ag, msg in rows:
            print(f"\n--- Testing Dispatch #{r_id} [{ag}] ---")
            print(f"Transcript: {msg}")
            res = process_dispatch(msg, ag, mode="auto", conn=conn, rate_limit_sec=1.0)
            print("Result:", json.dumps(res, indent=2))
