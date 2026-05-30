"""Standalone dev server for the multi-agent specialists.

Self-contained: does NOT touch agent/server.py. Runs on a separate port so
you can poke at the constellation independently of the main app.

Run:
    .venv/bin/uvicorn agent.specialists.dev_server:app --host 127.0.0.1 --port 8765 --reload

Open:
    http://127.0.0.1:8765/

Endpoints:
    GET  /                       -> the dev UI
    POST /api/chat               -> body {message, history} -> coordinator.handle_user
    GET  /api/findings?since=N   -> long-poll for new findings since timestamp N
    POST /api/scenario           -> body {name: "weather"|"traffic"|"all"|"emergency"}
    GET  /api/specialists        -> list configured specialists w/ manifests
    GET  /api/health             -> liveness + mode info
"""
from __future__ import annotations
import asyncio
import glob
import json
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from agent.specialists.base import Event
from agent.specialists.bus import bus
from agent.specialists.coordinator import Coordinator
from agent.specialists.weather import WeatherAgent
from agent.specialists.traffic import TrafficAgent
from agent.specialists.safety import SafetyAgent
from agent.specialists.fleet import FleetAgent
from agent.specialists.narrator import NarratorAgent
from agent.opensky_auth import authed_get as opensky_get


ROOT = Path(__file__).resolve().parent.parent.parent
SPECIALISTS = [WeatherAgent(), TrafficAgent(), SafetyAgent(), FleetAgent(), NarratorAgent()]
COORDINATOR = Coordinator(specialists=SPECIALISTS)

app = FastAPI(title="ASI Hack — Specialist Dev Console")

# Background watcher state — toggleable from the UI
WATCHER = {
    "running": False,
    "task": None,
    "interval_sec": 30,   # how often to poll
    "tick_count": 0,
    "last_tick": 0.0,
}

# Active focus area. If set, watcher live-fetches OpenSky here every tick and
# location-aware chat queries default to it. If None, watcher uses cached
# recorder data (NE corridor only).
FOCUS = {
    "name": None,   # e.g. "boston" — matches Coordinator.KNOWN_AREAS key
    "lat": None,
    "lon": None,
    "rad_deg": 0.6,
}


# ---- scenarios (LIVE fetch where free + fast; cached where it would cost) ---

AW = "https://aviationweather.gov/api/data"
DATA_FRESHNESS: dict[str, float] = {}  # source -> last fetched ts


def _latest(pattern: str) -> Path | None:
    matches = sorted(glob.glob(str(ROOT / pattern)))
    return Path(matches[-1]) if matches else None


def _http_json(url: str, params: dict) -> list | dict | None:
    try:
        r = httpx.get(url, params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def _weather_events(live: bool = True) -> list[Event]:
    """Fetch the four weather products. NOAA's aviation-weather.gov is free,
    keyless, and current — no reason to cache during the demo."""
    out = []
    now = time.time()

    # G-AIRMETs (turbulence/icing forecasts)
    gairmet = _http_json(f"{AW}/gairmet", {"format": "json"}) if live else None
    if gairmet is None:
        f = _latest("data/overnight/weather/gairmet_*.json")
        if f:
            gairmet = json.loads(f.read_text())
            DATA_FRESHNESS["gairmet"] = f.stat().st_mtime
    else:
        DATA_FRESHNESS["gairmet"] = now
    if gairmet:
        out.append(Event(type="gairmet.snapshot", payload={"advisories": gairmet}))

    # SIGMETs (active hazards)
    sigmets = _http_json(f"{AW}/airsigmet", {"format": "json"}) if live else None
    if sigmets is None:
        f = _latest("data/overnight/weather/airsigmet_*.json")
        if f:
            sigmets = json.loads(f.read_text())
            DATA_FRESHNESS["airsigmet"] = f.stat().st_mtime
    else:
        DATA_FRESHNESS["airsigmet"] = now
    if sigmets:
        for s in sigmets:
            if s.get("hazard") != "CONVECTIVE":
                continue
            raw_lines = (s.get("rawAirSigmet") or "").split("\n")
            states_line = raw_lines[3] if len(raw_lines) > 3 else "n/a"
            # Pass the polygon through so the Coordinator can cross-correlate.
            coords = s.get("coords") or []
            pts = []
            for c in coords:
                try:
                    pts.append((float(c["lat"]), float(c["lon"])))
                except (KeyError, TypeError, ValueError):
                    continue
            out.append(Event(type="sigmet.issued", payload={
                "hazard": "CONVECTIVE",
                "id": s.get("airSigmetId") or "?",
                "states": states_line.strip(),
                "tops_ft": 38000, "duration_min": 120,
                "polygon": pts,                                 # list of (lat, lon)
                "valid_from": s.get("validTimeFrom"),
                "valid_to": s.get("validTimeTo"),
            }))

    # PIREPs (live pilot reports — most operationally relevant signal)
    pireps = _http_json(f"{AW}/pirep", {"bbox": "24,-125,50,-66", "format": "json", "age": 6}) if live else None
    if pireps is None:
        f = ROOT / "data/samples/pireps_us.json"
        if f.exists():
            pireps = json.loads(f.read_text())
            DATA_FRESHNESS["pirep"] = f.stat().st_mtime
    else:
        DATA_FRESHNESS["pirep"] = now
    if pireps:
        scale = ("NEG","SMTH","LGT","MOD","SEV","EXTM")
        reports = []
        for p in pireps[:200]:
            ints = [(p.get(f"tbInt{n}") or "").strip().upper() for n in (1, 2)]
            ints = [i for i in ints if i]
            worst = max(ints, default="",
                        key=lambda x: scale.index(x.split("-")[0]) if x and x.split("-")[0] in scale else 0)
            reports.append({"lat": p.get("lat"), "lon": p.get("lon"), "worst_intensity": worst})
        out.append(Event(type="pirep.snapshot", payload={"reports": reports}))

    return out


NWS_AREAS = "MA,NY,CT,NJ,PA,VT,NH,ME,RI,MD,DE,DC,VA"  # NE corridor + mid-Atlantic
NWS_SEEN_IDS: set[str] = set()  # de-dup alerts across ticks (NWS sends Updates)


def _nws_events() -> list[Event]:
    """Pull active NWS alerts in the NE corridor; emit per-alert events.
    Only first appearance of each id fires; subsequent ticks are silent
    unless the alert is re-issued with a new id."""
    try:
        r = httpx.get(
            "https://api.weather.gov/alerts/active",
            params={"area": NWS_AREAS},
            headers={"Accept": "application/geo+json"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        features = r.json().get("features", [])
    except Exception:
        return []
    DATA_FRESHNESS["nws"] = time.time()
    out = []
    for f in features:
        p = f.get("properties") or {}
        aid = p.get("id")
        if not aid or aid in NWS_SEEN_IDS:
            continue
        NWS_SEEN_IDS.add(aid)
        out.append(Event(type="nws.alert", payload={
            "id": aid,
            "event": p.get("event"),
            "severity": p.get("severity"),   # Minor / Moderate / Severe / Extreme
            "certainty": p.get("certainty"),
            "urgency": p.get("urgency"),
            "headline": p.get("headline"),
            "description": (p.get("description") or "")[:400],
            "area": p.get("areaDesc"),
            "effective": p.get("effective"),
            "expires": p.get("expires"),
            "sender": p.get("senderName"),
        }))
    return out


def _traffic_event() -> Event | None:
    """Traffic comes from the recorder. We could hit OpenSky live but it costs
    credits and adds latency. Latest cached snapshot is usually <60s old."""
    f = _latest("data/overnight/traffic/traffic_*.jsonl")
    if not f:
        return None
    last = None
    with f.open() as fh:
        for line in fh:
            if line.strip():
                last = json.loads(line)
    if not last:
        return None
    fields = ["icao24","callsign","origin_country","time_position","last_contact",
              "lon","lat","baro_alt","on_ground","velocity","heading","vert_rate",
              "sensors","geo_alt","squawk","spi","position_source"]
    states = [dict(zip(fields, s)) for s in (last.get("states") or [])]
    DATA_FRESHNESS["traffic"] = float(last.get("api_time") or f.stat().st_mtime)
    return Event(type="traffic.snapshot", payload={"states": states})


def _fetch_traffic_bbox(lat: float, lon: float, rad_deg: float = 0.6) -> list[dict]:
    """Live-fetch OpenSky state vectors for a bbox centered on (lat, lon)."""
    params = {
        "lamin": lat - rad_deg, "lamax": lat + rad_deg,
        "lomin": lon - rad_deg, "lomax": lon + rad_deg,
    }
    try:
        r = opensky_get("https://opensky-network.org/api/states/all", params=params, timeout=10)
        if r.status_code != 200:
            return []
        payload = r.json() or {}
        states = payload.get("states") or []
    except Exception:
        return []
    fields = ["icao24","callsign","origin_country","time_position","last_contact",
              "lon","lat","baro_alt","on_ground","velocity","heading","vert_rate",
              "sensors","geo_alt","squawk","spi","position_source"]
    out = []
    for s in states:
        if len(s) >= len(fields):
            out.append(dict(zip(fields, s)))
        else:
            d = dict(zip(fields, s + [None] * (len(fields) - len(s))))
            out.append(d)
    DATA_FRESHNESS[f"traffic_{lat:.0f}_{lon:.0f}"] = time.time()
    return out


def _fake_emergency() -> Event:
    return Event(type="traffic.snapshot", payload={"states": [
        {"icao24": "a0e250", "callsign": "AAL1767", "baro_alt": 4500,
         "velocity": 130, "on_ground": False, "squawk": "7700",
         "vert_rate": -18.0},
    ]})


def _fake_sigmet_over_traffic() -> list[Event]:
    """Drop a synthetic CONVECTIVE SIGMET right over the NE corridor where the
    recorder has aircraft — guarantees the cross-correlation pass finds hits.
    Useful for demoing the Coordinator's correlation step."""
    polygon = [
        (41.0, -75.0), (44.0, -75.0), (44.0, -70.0), (41.0, -70.0), (41.0, -75.0),
    ]
    return [Event(type="sigmet.issued", payload={
        "hazard": "CONVECTIVE",
        "id": "DEMO-X",
        "states": "NY NJ CT MA RI (synthetic demo)",
        "tops_ft": 38000, "duration_min": 90,
        "polygon": polygon,
        "valid_from": int(time.time()),
        "valid_to": int(time.time() + 90 * 60),
    })]


def _fire(events: list[Event]) -> int:
    n = 0
    for ev in events:
        for s in SPECIALISTS:
            if ev.type in s.interests():
                for finding in s.formulate(ev):
                    bus.publish(finding)
                    n += 1
    return n


# ---- routes -----------------------------------------------------------------

@app.get("/")
def root():
    return FileResponse(ROOT / "frontend" / "specialists.html")


@app.get("/specialists.html")
def static_html():
    return FileResponse(ROOT / "frontend" / "specialists.html")


@app.get("/api/specialists")
def list_specialists():
    return {
        "specialists": [s.manifest.to_dict() for s in SPECIALISTS],
        "coordinator": COORDINATOR.manifest.to_dict(),
        "mode": "stub",  # flip when inject_llm is called
        "push_threshold": COORDINATOR.PUSH_THRESHOLD,
    }


@app.post("/api/scenario")
async def scenario(req: Request):
    body = await req.json()
    name = (body.get("name") or "all").lower()
    if name == "weather":
        n = _fire(_weather_events())
    elif name == "traffic":
        e = _traffic_event()
        n = _fire([e]) if e else 0
    elif name == "all":
        events = _weather_events()
        events.extend(_nws_events())
        e = _traffic_event()
        if e: events.append(e)
        n = _fire(events)
    elif name == "emergency":
        n = _fire([_fake_emergency()])
    elif name == "correlate":
        # Drop a synthetic SIGMET over the NE traffic AND fire current traffic
        # so the Coordinator can run the correlation pass.
        events = _fake_sigmet_over_traffic()
        t = _traffic_event()
        if t: events.append(t)
        n = _fire(events)
        # Now run the cross-correlation
        states = (t.payload.get("states") or []) if t else []
        hazards = [f for f in bus.latest(50)
                   if f.specialist == "weather" and f.severity >= 3
                   and (f.metadata or {}).get("polygon")]
        corr = COORDINATOR.correlate(states, hazards)
        n += len(corr)
    else:
        return JSONResponse({"error": f"unknown scenario {name}"}, status_code=400)
    return {"fired": n, "scenario": name}


@app.post("/api/chat")
async def chat(req: Request):
    body = await req.json()
    msg = (body.get("message") or "").strip()
    history = body.get("history") or []
    if not msg:
        return JSONResponse({"error": "empty message"}, status_code=400)

    # Traffic source priority: explicit city in message > active focus > recorder
    msg_low = msg.lower()
    states: list[dict] = []
    for name, (lat, lon, rad_deg) in COORDINATOR.KNOWN_AREAS.items():
        if name in msg_low:
            states = _fetch_traffic_bbox(lat, lon, rad_deg)
            break
    if not states and FOCUS["lat"] is not None:
        states = _fetch_traffic_bbox(FOCUS["lat"], FOCUS["lon"], FOCUS["rad_deg"])
        # Inject focus name into message so the location-query path matches.
        # We do this by augmenting message; coordinator looks for area keywords.
        if FOCUS["name"] and FOCUS["name"] not in msg_low:
            msg = f"{msg} (over {FOCUS['name']})"
    if not states:
        t = _traffic_event()
        states = (t.payload.get("states") or []) if t else []

    result = COORDINATOR.handle_user(msg, history=history, traffic_states=states)
    return result


@app.get("/api/findings")
async def findings(since: float = 0.0, wait: float = 0.0):
    """Return findings whose timestamp > `since`. If `wait` > 0, hold the
    response open up to that many seconds waiting for new ones (poor-man's
    SSE substitute). Returns `{findings, server_time}`."""
    deadline = time.time() + wait if wait > 0 else None
    while True:
        recent = [f for f in bus.latest(200) if f.timestamp > since]
        if recent or not deadline or time.time() >= deadline:
            return {
                "findings": [f.to_dict() for f in recent],
                "server_time": time.time(),
            }
        await asyncio.sleep(0.25)


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "specialists": [s.name for s in SPECIALISTS],
        "findings_in_bus": len(bus.latest(1000)),
        "log_path": str(ROOT / "data" / "events" / "findings.jsonl"),
        "data_freshness": {k: time.time() - v for k, v in DATA_FRESHNESS.items()},
    }


@app.post("/api/clear")
def clear():
    """Wipe the bus and dedup state."""
    bus.clear()
    return {"ok": True}


# ---- background watcher -----------------------------------------------------

async def _watcher_loop():
    """Every WATCHER.interval_sec, fetch fresh weather + traffic and fire
    events through every specialist. Traffic source depends on FOCUS:
        - If a focus is set, live-fetch OpenSky for that bbox
        - Otherwise use the recorder's cached snapshot (NE-only)
    After each tick, run the Coordinator's cross-correlation pass."""
    while WATCHER["running"]:
        try:
            evs = _weather_events(live=True)
            evs.extend(_nws_events())

            # Decide traffic source for this tick
            if FOCUS["lat"] is not None:
                states = _fetch_traffic_bbox(FOCUS["lat"], FOCUS["lon"], FOCUS["rad_deg"])
                t_event = Event(type="traffic.snapshot", payload={"states": states}) if states else None
            else:
                t_event = _traffic_event()

            if t_event:
                evs.append(t_event)
            for ev in evs:
                for s in SPECIALISTS:
                    if ev.type in s.interests():
                        for f in s.formulate(ev):
                            bus.publish(f)

            # Cross-correlation pass: find affected flights
            if t_event:
                states = t_event.payload.get("states") or []
                hazards = [
                    f for f in bus.latest(50)
                    if f.specialist == "weather"
                    and f.severity >= 3
                    and (f.metadata or {}).get("polygon")
                ]
                if hazards:
                    COORDINATOR.correlate(states, hazards)

            WATCHER["tick_count"] += 1
            WATCHER["last_tick"] = time.time()
        except Exception as e:
            print(f"watcher error: {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(WATCHER["interval_sec"])


@app.post("/api/watcher/start")
async def watcher_start(req: Request):
    body = {}
    try: body = await req.json()
    except Exception: pass
    interval = int(body.get("interval_sec") or WATCHER["interval_sec"])
    WATCHER["interval_sec"] = max(5, min(600, interval))
    if WATCHER["running"]:
        return {"ok": True, "already_running": True, **{k: WATCHER[k] for k in ("interval_sec", "tick_count", "last_tick")}}
    WATCHER["running"] = True
    WATCHER["task"] = asyncio.create_task(_watcher_loop())
    return {"ok": True, "started": True, "interval_sec": WATCHER["interval_sec"]}


@app.post("/api/watcher/stop")
def watcher_stop():
    WATCHER["running"] = False
    if WATCHER["task"]:
        WATCHER["task"].cancel()
        WATCHER["task"] = None
    return {"ok": True, "stopped": True}


@app.get("/api/watcher/status")
def watcher_status():
    return {
        "running": WATCHER["running"],
        "interval_sec": WATCHER["interval_sec"],
        "tick_count": WATCHER["tick_count"],
        "seconds_since_last_tick": (time.time() - WATCHER["last_tick"]) if WATCHER["last_tick"] else None,
        "focus": FOCUS["name"],
    }


@app.get("/api/focus")
def focus_get():
    return {
        "current": FOCUS["name"],
        "lat": FOCUS["lat"],
        "lon": FOCUS["lon"],
        "rad_deg": FOCUS["rad_deg"],
        "available": [
            {"key": k, "lat": v[0], "lon": v[1], "rad_deg": v[2]}
            for k, v in COORDINATOR.KNOWN_AREAS.items()
        ],
    }


@app.post("/api/focus")
async def focus_set(req: Request):
    body = await req.json()
    name = (body.get("name") or "").strip().lower()
    if not name or name in ("none", "clear", "off"):
        FOCUS["name"] = FOCUS["lat"] = FOCUS["lon"] = None
        return {"ok": True, "focus": None}
    area = COORDINATOR.KNOWN_AREAS.get(name)
    if not area:
        return JSONResponse({"error": f"unknown area '{name}'"}, status_code=400)
    FOCUS["name"] = name
    FOCUS["lat"], FOCUS["lon"], FOCUS["rad_deg"] = area
    return {"ok": True, "focus": name, "lat": area[0], "lon": area[1], "rad_deg": area[2]}


@app.get("/api/freshness")
def freshness():
    """How old (in seconds) is the data each source last reported."""
    now = time.time()
    return {k: round(now - v, 1) for k, v in DATA_FRESHNESS.items()}
