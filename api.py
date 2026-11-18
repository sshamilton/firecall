#!/usr/bin/env python3
"""
FastAPI Backend for Firecall Interactive Map Dashboard.
Serves:
  - GET /api/dispatches        (filterable by hours, agency, incident_type, precision, search, limit)
  - GET /api/stats             (summary statistics, category breakdown, precision counts, LLM status)
  - GET /api/stations          (Orange County Fire Department station locations & metadata)
  - POST /api/dispatches/{id}/reparse   (re-run LLM/Nominatim geocoding on a specific call)
  - PATCH /api/dispatches/{id}/location (manual address/coordinate override from map UI)
  - POST /api/backfill/batch   (trigger background batch LLM upgrade on recent calls)
  - GET /audio/{filename}      (stream WAV files from recordings/)
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from geocoder import (
    BASE_DIR,
    DB_PATH,
    INCIDENT_CATEGORIES,
    STATIONS,
    ensure_schema,
    geocode_extracted,
    get_llm_config,
    process_dispatch,
)
from backfill_locations import fast_backfill_all_stations, geocode_rows

RECORDINGS_DIR = BASE_DIR / "recordings"
WEB_DIST_DIR = BASE_DIR / "web" / "dist"

app = FastAPI(
    title="Firecall Orange County NY Dispatch Map API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.on_event("startup")
def on_startup():
    if DB_PATH.exists():
        with sqlite3.connect(DB_PATH) as conn:
            ensure_schema(conn)
            fast_backfill_all_stations(conn)


class LocationUpdate(BaseModel):
    extracted_address: Optional[str] = None
    municipality: Optional[str] = None
    cross_streets: Optional[str] = None
    incident_type: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    regeocode: bool = False


def format_dispatch_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    # Parse JSON fields safely
    try:
        d["tones"] = json.loads(d["tones"]) if d.get("tones") else []
    except Exception:
        d["tones"] = []

    try:
        d["agencies"] = json.loads(d["agencies"]) if d.get("agencies") else []
    except Exception:
        d["agencies"] = []

    if not d["agencies"] and d.get("agency"):
        ag = d["agency"]
        if ag != "Standard Voice / Patch" and not ag.startswith("Single Alert") and not ag.startswith("Unknown"):
            d["agencies"] = [p.strip() for p in re.split(r"\s+/\s+(?![^()]*\))", ag)]

    # Build local audio path relative to API server so any browser can stream it directly
    audio_filename = None
    if d.get("audio_file"):
        audio_filename = Path(d["audio_file"]).name
    elif d.get("audio_url"):
        audio_filename = d["audio_url"].rsplit("/", 1)[-1]

    if audio_filename and (RECORDINGS_DIR / audio_filename).exists():
        d["audio_stream_url"] = f"/audio/{audio_filename}"
        d["has_local_audio"] = True
    else:
        d["audio_stream_url"] = d.get("audio_url")
        d["has_local_audio"] = False

    return d


@app.get("/api/dispatches")
def list_dispatches(
    hours: Optional[int] = Query(None, description="Filter to last N hours (e.g. 24, 72, 168, 720)"),
    agency: Optional[str] = Query(None, description="Filter by agency name substring"),
    incident_type: Optional[str] = Query(None, description="Filter by incident category"),
    precision: Optional[str] = Query(None, description="Filter by location_precision"),
    search: Optional[str] = Query(None, description="Full-text search in transcript, address, or municipality"),
    limit: int = Query(300, ge=1, le=2500),
):
    with get_db() as conn:
        clauses = ["latitude IS NOT NULL", "longitude IS NOT NULL"]
        params = []

        if hours is not None and hours > 0:
            clauses.append("timestamp >= datetime('now', ?)")
            params.append(f"-{int(hours)} hours")

        if agency and agency.lower() != "all":
            clauses.append("agency LIKE ?")
            params.append(f"%{agency}%")

        if incident_type and incident_type.lower() != "all":
            clauses.append("incident_type = ?")
            params.append(incident_type)

        if precision and precision.lower() != "all":
            clauses.append("location_precision = ?")
            params.append(precision)

        if search and search.strip():
            q = f"%{search.strip()}%"
            clauses.append(
                "(message LIKE ? OR extracted_address LIKE ? OR municipality LIKE ? OR cross_streets LIKE ? OR agency LIKE ?)"
            )
            params.extend([q, q, q, q, q])

        where_sql = " AND ".join(clauses)
        sql = f"""
            SELECT id, timestamp, agency, agencies, tones, message, raw_message,
                   channel, frequency, audio_file, audio_url,
                   total_duration_sec, active_transmission_sec, speech_sec,
                   incident_type, extracted_address, municipality, cross_streets,
                   latitude, longitude, location_precision, parse_method
            FROM dispatches
            WHERE {where_sql}
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
        """
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        items = [format_dispatch_row(r) for r in rows]
        return {
            "count": len(items),
            "dispatches": items,
        }


@app.get("/api/stats")
def get_stats(hours: Optional[int] = Query(None)):
    with get_db() as conn:
        time_clause = "1=1"
        params = ()
        if hours is not None and hours > 0:
            time_clause = "timestamp >= datetime('now', ?)"
            params = (f"-{int(hours)} hours",)

        total_all = conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
        last_24h = conn.execute(
            "SELECT COUNT(*) FROM dispatches WHERE timestamp >= datetime('now', '-24 hours')"
        ).fetchone()[0]
        last_7d = conn.execute(
            "SELECT COUNT(*) FROM dispatches WHERE timestamp >= datetime('now', '-168 hours')"
        ).fetchone()[0]

        filtered_total = conn.execute(
            f"SELECT COUNT(*) FROM dispatches WHERE {time_clause}", params
        ).fetchone()[0]

        # Breakdown by incident_type
        cat_rows = conn.execute(
            f"""
            SELECT COALESCE(incident_type, 'Other') as cat, COUNT(*) as cnt
            FROM dispatches
            WHERE {time_clause}
            GROUP BY cat
            ORDER BY cnt DESC
            """,
            params,
        ).fetchall()
        by_category = {r["cat"]: r["cnt"] for r in cat_rows}

        # Breakdown by location_precision
        prec_rows = conn.execute(
            f"""
            SELECT COALESCE(location_precision, 'station_fallback') as prec, COUNT(*) as cnt
            FROM dispatches
            WHERE {time_clause}
            GROUP BY prec
            """,
            params,
        ).fetchall()
        by_precision = {r["prec"]: r["cnt"] for r in prec_rows}

        # Breakdown by parse_method
        method_rows = conn.execute(
            f"""
            SELECT COALESCE(parse_method, 'regex_fast') as m, COUNT(*) as cnt
            FROM dispatches
            WHERE {time_clause}
            GROUP BY m
            """,
            params,
        ).fetchall()
        by_method = {r["m"]: r["cnt"] for r in method_rows}

        # Top agencies
        ag_rows = conn.execute(
            f"SELECT agency FROM dispatches WHERE agency IS NOT NULL AND ({time_clause})",
            params,
        ).fetchall()
        from collections import Counter
        ag_counter = Counter()
        for r in ag_rows:
            ag = r["agency"]
            if ag:
                if ag == "Standard Voice / Patch" or ag.startswith("Single Alert") or ag.startswith("Unknown"):
                    parts = [ag]
                else:
                    parts = [p.strip() for p in re.split(r"\s+/\s+(?![^()]*\))", ag)]
                for p in parts:
                    ag_counter[p] += 1

        top_agencies = [{"agency": k, "count": v} for k, v in ag_counter.most_common(60)]
        llm_cfg = get_llm_config()
        import os
        carto_key = os.getenv("FIRECALL_CARTO_API_KEY", "").strip()

        return {
            "total_all_time": total_all,
            "last_24h": last_24h,
            "last_7d": last_7d,
            "filtered_total": filtered_total,
            "by_category": by_category,
            "by_precision": by_precision,
            "by_parse_method": by_method,
            "top_agencies": top_agencies,
            "categories": INCIDENT_CATEGORIES,
            "llm": {
                "enabled": llm_cfg["enabled"],
                "model": llm_cfg["model"],
            },
            "map_config": {
                "carto_api_key": carto_key,
            },
        }


@app.get("/api/stations")
def list_stations():
    stations_list = []
    for key, info in STATIONS.items():
        stations_list.append({
            "id": key,
            **info,
        })
    return {"stations": stations_list}


@app.post("/api/dispatches/{dispatch_id}/reparse")
def reparse_dispatch(dispatch_id: int, mode: str = Query("llm", pattern="^(auto|llm|regex)$")):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM dispatches WHERE id = ?", (dispatch_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Dispatch not found")

        res = process_dispatch(
            message=row["message"] or "",
            agency=row["agency"] or "",
            mode=mode,
            conn=conn,
            rate_limit_sec=0.3,
            skip_nominatim=False,
        )
        conn.execute(
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
                dispatch_id,
            ),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM dispatches WHERE id = ?", (dispatch_id,)
        ).fetchone()
        return {"dispatch": format_dispatch_row(updated)}


@app.patch("/api/dispatches/{dispatch_id}/location")
def update_dispatch_location(dispatch_id: int, body: LocationUpdate):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM dispatches WHERE id = ?", (dispatch_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Dispatch not found")

        addr = body.extracted_address if body.extracted_address is not None else row["extracted_address"]
        muni = body.municipality if body.municipality is not None else row["municipality"]
        cross = body.cross_streets if body.cross_streets is not None else row["cross_streets"]
        inc = body.incident_type if body.incident_type is not None else row["incident_type"]
        lat = body.latitude if body.latitude is not None else row["latitude"]
        lon = body.longitude if body.longitude is not None else row["longitude"]
        prec = "exact"

        if body.regeocode and addr:
            lat, lon, prec = geocode_extracted(
                extracted_address=addr,
                municipality=muni,
                cross_streets=cross,
                agency=row["agency"] or "",
                message=row["message"] or "",
                conn=conn,
                rate_limit_sec=0.2,
                skip_nominatim=False,
            )

        conn.execute(
            """
            UPDATE dispatches
            SET extracted_address = ?,
                municipality = ?,
                cross_streets = ?,
                incident_type = ?,
                latitude = ?,
                longitude = ?,
                location_precision = ?,
                parse_method = 'manual'
            WHERE id = ?
            """,
            (addr, muni, cross, inc, lat, lon, prec, dispatch_id),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM dispatches WHERE id = ?", (dispatch_id,)
        ).fetchone()
        return {"dispatch": format_dispatch_row(updated)}


@app.post("/api/backfill/batch")
def trigger_batch_upgrade(background_tasks: BackgroundTasks, limit: int = Query(15, ge=1, le=200)):
    def _run_upgrade(n: int):
        with sqlite3.connect(DB_PATH) as conn:
            geocode_rows(conn, mode="llm", limit=n)

    background_tasks.add_task(_run_upgrade, limit)
    return {"status": "started", "limit": limit}


@app.get("/audio/{filename}")
def serve_audio(filename: str):
    safe_name = Path(filename).name
    file_path = RECORDINGS_DIR / safe_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio recording not found on disk")
    return FileResponse(file_path, media_type="audio/wav")


# If web/dist exists (production build), serve it at root
if WEB_DIST_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIST_DIR), html=True), name="web")
