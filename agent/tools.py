"""Claude tool definitions and executors.

Each tool is two things:
    1. A JSON schema (TOOLS list) — passed to client.messages.create
    2. A Python function (EXECUTORS dict) — called when Claude returns tool_use

Keep them in lockstep. Add new tools by appending to both.
"""
from __future__ import annotations
import json
from typing import Any

import httpx

from agent.opensky_auth import authed_get as opensky_get
from agent.auditor import find_decision_moments as _auditor_run
from agent.aircraft_db import lookup as _ac_lookup, describe as _ac_describe

# Per-request queue of UI commands. agent.loop.chat() clears it before each
# turn and reads it after — populated only by show_on_map().
MAP_ACTIONS: list[dict[str, Any]] = []


def show_on_map(action: str, lat: float | None = None, lon: float | None = None,
                alt_m: float | None = None, pitch_deg: float | None = None,
                icao24: str | None = None, layer: str | None = None,
                iso_time: str | None = None, multiplier: float | None = None) -> dict[str, Any]:
    """Queue a UI command for the Cesium frontend. Doesn't return data — it
    just tells the map to do something visual. Use whenever the answer would
    benefit from a visual: 'show me X' → fly the camera; 'highlight flight Y'
    → track that aircraft; 'show the turbulence overlay' → load that layer.

    Valid `action` values:
      - "fly_to":        requires lat, lon; optional alt_m (default 180000), pitch_deg (-50)
      - "highlight_flight": requires icao24; the camera will track that aircraft
      - "load_layer":    requires layer in {
            "traffic","turb","decisions","airports","radar","buildings",
            "sigmet" (convective storm SIGMETs as red 3D polygons),
            "pirep"  (pilot reports as color-coded 3D points by turb intensity),
            "winds"  (FL450 jet-stream wind arrows, magenta = >70 kt jet)
        }
      - "set_time":      requires iso_time (e.g. "2026-05-30T03:30:00Z")
      - "set_speed":     requires multiplier (1, 10, 30, 100, 600)
    """
    cmd = {"action": action}
    for k, v in (("lat", lat), ("lon", lon), ("alt_m", alt_m), ("pitch_deg", pitch_deg),
                 ("icao24", icao24), ("layer", layer), ("iso_time", iso_time),
                 ("multiplier", multiplier)):
        if v is not None:
            cmd[k] = v
    MAP_ACTIONS.append(cmd)
    return {"queued": True, "command": cmd}

AW = "https://aviationweather.gov/api/data"


def get_metar(icao: str) -> dict[str, Any]:
    """Current weather observation for one airport."""
    icao = icao.upper().strip()
    r = httpx.get(f"{AW}/metar", params={"ids": icao, "format": "json"}, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not data:
        return {"error": f"no METAR for {icao}"}
    m = data[0]
    return {
        "icao": m.get("icaoId"),
        "report_time": m.get("reportTime"),
        "flight_category": m.get("fltCat"),
        "wind_dir_deg": m.get("wdir"),
        "wind_speed_kt": m.get("wspd"),
        "wind_gust_kt": m.get("wgst"),
        "visibility_sm": m.get("visib"),
        "temp_c": m.get("temp"),
        "dewpoint_c": m.get("dewp"),
        "altimeter_hpa": m.get("altim"),
        "clouds": m.get("clouds"),
        "raw": m.get("rawOb"),
    }


def get_taf(icao: str) -> dict[str, Any]:
    """Terminal Aerodrome Forecast for one airport."""
    icao = icao.upper().strip()
    r = httpx.get(f"{AW}/taf", params={"ids": icao, "format": "json"}, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not data:
        return {"error": f"no TAF for {icao}"}
    t = data[0]
    return {
        "icao": t.get("icaoId"),
        "issue_time": t.get("issueTime"),
        "valid_from": t.get("validTimeFrom"),
        "valid_to": t.get("validTimeTo"),
        "forecast_periods": len(t.get("fcsts") or []),
        "raw": t.get("rawTAF"),
    }


def get_sigmets() -> list[dict[str, Any]]:
    """All active SIGMETs (significant meteorological hazards)."""
    r = httpx.get(f"{AW}/airsigmet", params={"format": "json"}, timeout=10)
    r.raise_for_status()
    return [
        {
            "hazard": s.get("hazard"),
            "severity": s.get("severity"),
            "valid_from": s.get("validTimeFrom"),
            "valid_to": s.get("validTimeTo"),
            "raw_preview": (s.get("rawAirSigmet") or "")[:200],
        }
        for s in (r.json() or [])
    ]


def get_turbulence_advisories() -> list[dict[str, Any]]:
    """Active G-AIRMET turbulence forecasts (TURB-HI, TURB-LO) with altitude bands."""
    r = httpx.get(f"{AW}/gairmet", params={"format": "json"}, timeout=10)
    r.raise_for_status()
    out = []
    for g in (r.json() or []):
        if not (g.get("hazard") or "").startswith("TURB"):
            continue
        out.append({
            "hazard": g.get("hazard"),
            "severity": g.get("severity"),
            "valid_time": g.get("validTime"),
            "altitude_base": g.get("base"),
            "altitude_top": g.get("top"),
            "polygon_vertices": len(g.get("geom") or []),
        })
    return out


def get_pireps(lamin: float, lamax: float, lomin: float, lomax: float, age_hours: int = 6) -> dict[str, Any]:
    """Pilot reports (PIREPs) within a bounding box in the last N hours.
    PIREPs are free-text observations from pilots in the air — the strongest
    ground-truth signal for actual turbulence, icing, and ride quality.
    Forecasts (G-AIRMET) say what *might* happen; PIREPs say what *did*."""
    r = httpx.get(
        f"{AW}/pirep",
        params={"bbox": f"{lamin},{lomin},{lamax},{lomax}", "format": "json", "age": age_hours},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json() or []
    if not isinstance(data, list):
        return {"error": "pirep api returned non-list", "raw": data}
    digested = []
    for p in data:
        # collapse the two-band turbulence/icing fields into one summary each
        turb = []
        for n in (1, 2):
            intensity = (p.get(f"tbInt{n}") or "").strip()
            if intensity:
                turb.append({
                    "intensity": intensity,
                    "type": (p.get(f"tbType{n}") or "").strip() or None,
                    "base_ft": p.get(f"tbBas{n}"),
                    "top_ft": p.get(f"tbTop{n}"),
                    "frequency": (p.get(f"tbFreq{n}") or "").strip() or None,
                })
        icing = []
        for n in (1, 2):
            intensity = (p.get(f"icgInt{n}") or "").strip()
            if intensity:
                icing.append({
                    "intensity": intensity,
                    "type": (p.get(f"icgType{n}") or "").strip() or None,
                    "base_ft": p.get(f"icgBas{n}"),
                    "top_ft": p.get(f"icgTop{n}"),
                })
        digested.append({
            "obs_time": p.get("obsTime"),
            "lat": p.get("lat"),
            "lon": p.get("lon"),
            "aircraft_type": p.get("acType"),
            "flight_level": p.get("fltLvl"),  # in 100s of feet
            "turbulence": turb,
            "icing": icing,
            "raw": p.get("rawOb"),
        })
    return {"count": len(digested), "age_hours": age_hours, "reports": digested[:30]}


def audit_recorded_flights(hours_back: int = 4, top_n: int = 10) -> dict[str, Any]:
    """Scan recorded flights for the last N hours and find decision moments —
    flights that flew through active turbulence advisory polygons. Returns
    ranked list with dwell time and severity. This is the auditor."""
    import time
    now = time.time()
    bbox = (40.0, 44.0, -74.0, -69.0)  # NE corridor — matches recorder
    return _auditor_run(t_min=now - hours_back * 3600, t_max=now, bbox=bbox, top_n=top_n)


def lookup_aircraft(icao24: str) -> dict[str, Any]:
    """Resolve an ICAO24 hex to registration, operator, aircraft model.
    Offline lookup against OpenSky's 537k-row aircraft DB."""
    info = _ac_lookup(icao24)
    if not info:
        return {"icao24": icao24, "found": False}
    return {"icao24": icao24, "found": True, **info, "description": _ac_describe(icao24)}


def get_flight_track(icao24: str, time: int) -> dict[str, Any]:
    """Pull the full waypoint track for a specific flight from OpenSky.
    `time` is any unix epoch second during the flight. Expensive (~400 credits/call)
    — use for one-off Q&A about a specific historical flight, not in a loop.
    Returns waypoints as [time, lat, lon, alt_m, heading_deg, on_ground]."""
    r = opensky_get(
        "https://opensky-network.org/api/tracks/all",
        params={"icao24": icao24, "time": time},
        timeout=20,
    )
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}", "body": r.text[:200]}
    j = r.json() or {}
    path = j.get("path") or []
    return {
        "icao24": j.get("icao24"),
        "callsign": (j.get("callsign") or "").strip(),
        "start_time": j.get("startTime"),
        "end_time": j.get("endTime"),
        "waypoint_count": len(path),
        "first_waypoint": path[0] if path else None,
        "last_waypoint": path[-1] if path else None,
        "aircraft": _ac_lookup(j.get("icao24") or icao24) or None,
    }


def find_recent_arrivals(airport_icao: str, begin: int, end: int) -> dict[str, Any]:
    """List flights that landed at an airport between two unix epochs. NOTE:
    OpenSky's /flights/* data is ~7+ days lagged — query begin/end ≥7 days ago,
    otherwise empty results. Useful to enumerate candidates for a /tracks lookup."""
    r = opensky_get(
        "https://opensky-network.org/api/flights/arrival",
        params={"airport": airport_icao, "begin": begin, "end": end},
        timeout=20,
    )
    if r.status_code == 404:
        return {"count": 0, "note": "no flights matched (or window too recent — /flights/* has ~7d lag)"}
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}", "body": r.text[:200]}
    flights = r.json() or []
    return {
        "count": len(flights),
        "flights": [
            {
                "icao24": f.get("icao24"),
                "callsign": (f.get("callsign") or "").strip(),
                "departure_airport": f.get("estDepartureAirport"),
                "first_seen": f.get("firstSeen"),
                "last_seen": f.get("lastSeen"),
            } for f in flights[:30]
        ],
    }


def get_traffic(lamin: float, lamax: float, lomin: float, lomax: float) -> dict[str, Any]:
    """Live aircraft state vectors in a bounding box (OpenSky ADS-B)."""
    r = opensky_get(
        "https://opensky-network.org/api/states/all",
        params={"lamin": lamin, "lamax": lamax, "lomin": lomin, "lomax": lomax},
        timeout=15,
    )
    r.raise_for_status()
    payload = r.json() or {}
    states = payload.get("states") or []
    # OpenSky state vector positional schema; keep names that an LLM can reason about
    fields = ["icao24","callsign","origin_country","time_position","last_contact",
              "lon","lat","baro_alt","on_ground","velocity","heading","vert_rate",
              "sensors","geo_alt","squawk","spi","position_source"]
    return {
        "fetched_at": payload.get("time"),
        "count": len(states),
        "aircraft": [dict(zip(fields, s)) for s in states[:50]],  # cap for token sanity
    }


TOOLS = [
    {
        "name": "get_metar",
        "description": "Get the current METAR (weather observation) for one airport by ICAO code (e.g. KBOS, KJFK). Returns wind, visibility, ceiling, flight category.",
        "input_schema": {
            "type": "object",
            "properties": {"icao": {"type": "string", "description": "ICAO airport code, e.g. KBOS"}},
            "required": ["icao"],
        },
    },
    {
        "name": "get_taf",
        "description": "Get the TAF (terminal forecast) for one airport. Use to anticipate conditions in the next 24-30 hours.",
        "input_schema": {
            "type": "object",
            "properties": {"icao": {"type": "string"}},
            "required": ["icao"],
        },
    },
    {
        "name": "get_sigmets",
        "description": "List active SIGMETs across US airspace. Use to check for severe hazards: convective storms, icing, severe turbulence.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_turbulence_advisories",
        "description": "List active turbulence forecasts (G-AIRMET TURB-HI for high altitude, TURB-LO for low) with altitude bands and severity.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_pireps",
        "description": "Pilot reports (PIREPs) in a bbox over the last N hours. These are the ground truth for turbulence/icing — pilots actually flew through it and reported what they experienced. Use for 'are pilots reporting chop near BOS', 'any icing reports in the climb-out', etc. Free-text reports come back in the `raw` field.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lamin": {"type": "number"},
                "lamax": {"type": "number"},
                "lomin": {"type": "number"},
                "lomax": {"type": "number"},
                "age_hours": {"type": "integer", "description": "How far back to search (default 6, max 24)"},
            },
            "required": ["lamin", "lamax", "lomin", "lomax"],
        },
    },
    {
        "name": "show_on_map",
        "description": "Drive the Cesium frontend. Call alongside your text reply, not instead of it. Actions: fly_to (camera to lat/lon), highlight_flight (track an icao24), load_layer (turn on a data overlay), set_time (jump timeline to ISO), set_speed (1|10|30|100|600× playback). Available layers: traffic (live aircraft), turb (G-AIRMET turbulence advisory polygons), decisions (red trails of flights flagged by the auditor), airports (3D airport markers), radar (NEXRAD ground reflectivity), buildings (3D OSM buildings, BOS only when zoomed), sigmet (red convective storm polygons), pirep (color-coded 3D pilot reports), winds (FL450 jet-stream arrows).",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["fly_to", "highlight_flight", "load_layer", "set_time", "set_speed"],
                },
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "alt_m": {"type": "number", "description": "camera altitude in meters; default 180000"},
                "pitch_deg": {"type": "number", "description": "camera pitch; default -50"},
                "icao24": {"type": "string", "description": "6-char hex; required for highlight_flight"},
                "layer": {"type": "string", "enum": ["traffic","turb","decisions","airports","radar","buildings","sigmet","pirep","winds"]},
                "iso_time": {"type": "string", "description": "ISO-8601, e.g. 2026-05-30T03:30:00Z"},
                "multiplier": {"type": "number"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "lookup_aircraft",
        "description": "Resolve an ICAO24 hex code (e.g. 'a73dfe') to its tail number, operator, and model from the offline OpenSky aircraft database. Use any time you have an icao24 and want to render it as human flavor — e.g. when reporting a decision moment, you almost always want to say 'N566JB JetBlue A320' instead of 'a73dfe'.",
        "input_schema": {
            "type": "object",
            "properties": {"icao24": {"type": "string", "description": "6-char hex"}},
            "required": ["icao24"],
        },
    },
    {
        "name": "get_flight_track",
        "description": "Pull the actual waypoint track for one historical flight from OpenSky's REST API. EXPENSIVE (~400 credits per call out of our 4000/day budget) — only call for a single specific flight you want to inspect, not in a loop. `time` is a unix epoch second during the flight; use any value between first_seen and last_seen. Returns waypoint list with lat/lon/altitude/heading.",
        "input_schema": {
            "type": "object",
            "properties": {
                "icao24": {"type": "string"},
                "time": {"type": "integer", "description": "unix epoch seconds during the flight"},
            },
            "required": ["icao24", "time"],
        },
    },
    {
        "name": "find_recent_arrivals",
        "description": "Enumerate flights that arrived at an airport during a window. CAVEAT: OpenSky's flight database has ~7+ days lag — query 'last week' not 'yesterday' or it returns empty. Use to find candidate flights to inspect with get_flight_track. ICAO airport code (KBOS, KJFK, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "airport_icao": {"type": "string", "description": "e.g. KBOS"},
                "begin": {"type": "integer", "description": "unix epoch start"},
                "end": {"type": "integer", "description": "unix epoch end"},
            },
            "required": ["airport_icao", "begin", "end"],
        },
    },
    {
        "name": "audit_recorded_flights",
        "description": "Audit recorded flights from the recorder for turbulence exposure. Returns flights ranked by how long they spent inside active turbulence advisory polygons (3D point-in-polygon + time + altitude band). Use this to answer questions like 'find the worst decision moments' or 'how much preventable chop did flights endure tonight'. Returns a `total_chop_minutes` headline number and a top-N list with `callsign`, `dwell_minutes`, `advisory_severity`, `advisory_band_ft`.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours_back": {"type": "integer", "description": "Window size in hours (default 4)"},
                "top_n": {"type": "integer", "description": "How many decision moments to return (default 10)"},
            },
        },
    },
    {
        "name": "get_traffic",
        "description": "Get live aircraft (ADS-B state vectors) in a lat/lon bounding box. Returns up to 50 aircraft with position, altitude, velocity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lamin": {"type": "number"},
                "lamax": {"type": "number"},
                "lomin": {"type": "number"},
                "lomax": {"type": "number"},
            },
            "required": ["lamin", "lamax", "lomin", "lomax"],
        },
    },
]

EXECUTORS = {
    "get_metar": get_metar,
    "get_taf": get_taf,
    "get_sigmets": lambda: get_sigmets(),
    "get_turbulence_advisories": lambda: get_turbulence_advisories(),
    "get_pireps": get_pireps,
    "show_on_map": show_on_map,
    "lookup_aircraft": lookup_aircraft,
    "get_flight_track": get_flight_track,
    "find_recent_arrivals": find_recent_arrivals,
    "audit_recorded_flights": audit_recorded_flights,
    "get_traffic": get_traffic,
}


def run_tool(name: str, args: dict) -> str:
    fn = EXECUTORS.get(name)
    if fn is None:
        return json.dumps({"error": f"unknown tool {name}"})
    try:
        result = fn(**args) if args else fn()
        return json.dumps(result, default=str)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
