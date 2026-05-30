"""FastAPI server: serves the Cesium frontend and exposes /api/chat.

Single origin replaces the bare `python -m http.server` so the browser can POST
to the agent without CORS heroics.

Run:
    .venv/bin/uvicorn agent.server:app --host 127.0.0.1 --port 8000 --reload
"""
from __future__ import annotations
import asyncio
import glob
import os
import threading
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from anthropic import Anthropic

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from agent.loop import chat, chat_stream  # noqa: E402
from agent.api_key import get_anthropic_api_key  # noqa: E402
from agent.opensky_auth import authed_get as opensky_get  # noqa: E402
from agent.specialists.coordinator import Coordinator  # noqa: E402
from agent.specialists.weather import WeatherAgent  # noqa: E402
from agent.specialists.traffic import TrafficAgent  # noqa: E402
from agent.specialists.safety import SafetyAgent  # noqa: E402
from agent.specialists.fleet import FleetAgent  # noqa: E402
from agent.specialists.narrator import NarratorAgent  # noqa: E402
from agent.specialists.pattern_analyst import PatternAnalystAgent  # noqa: E402
from agent.specialists.conflict_predictor import ConflictPredictorAgent  # noqa: E402
from agent.specialists.wind_router import WindRouterAgent  # noqa: E402
from agent.specialists.base import Event, Finding  # noqa: E402
from agent.specialists.bus import bus  # noqa: E402
# Reuse the data-fetch helpers from the dev console so the watcher doesn't
# duplicate the NOAA / NWS plumbing. (Traffic uses the local _fetch_traffic_bbox
# defined below, not dev_server's, to avoid two copies of the same fetch.)
from agent.specialists.dev_server import (  # noqa: E402
    _weather_events,
    _nws_events,
    _traffic_event,
)
import json  # noqa: E402
import httpx  # noqa: E402
# Track 1 (PIREP -> turbulence area) + Track 2 (HRRR convective) ingest.
# Both emit Findings with metadata["polygon"] (lat,lon) -> feed COORDINATOR.correlate().
from agent.tools import get_pireps  # noqa: E402
from agent.turbulence_area import pireps_to_hazards  # noqa: E402
from agent import scenario_wx  # noqa: E402

AW_API = "https://aviationweather.gov/api/data"  # NOAA aviation weather, keyless

app = FastAPI(title="ASI Hack — 4D Airspace + Agent")

# Initialize multi-agent specialists with LLM mode enabled
SPECIALISTS = [
    WeatherAgent(),
    TrafficAgent(),
    SafetyAgent(),
    FleetAgent(),
    NarratorAgent(),
    PatternAnalystAgent(),
    ConflictPredictorAgent(),
    WindRouterAgent(),
]
COORDINATOR = Coordinator(specialists=SPECIALISTS)

# Inject LLM function into all specialists and coordinator
def _llm_call(system: str, user: str, tools: list | None = None) -> str:
    """LLM reasoning for specialists. Uses Claude Sonnet for speed."""
    client = Anthropic(api_key=get_anthropic_api_key())
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")

for specialist in SPECIALISTS + [COORDINATOR]:
    specialist.inject_llm(_llm_call)

# NE-corridor watch box for the proactive-alert watcher — matches the overnight
# recorder's coverage so the live OpenSky pull lines up with cached traffic if
# we have to fall back.
WATCH_CENTER = (42.0, -71.5)
WATCH_RADIUS_DEG = 2.5
WATCH_INTERVAL_SEC = 30

# Convective track (Track 2). OFF by default — the live demo is unchanged unless
# you opt in via WX_ASKED_AT=<scenario>, e.g. `WX_ASKED_AT=2025-08-22T18:00:00Z`.
WX_ASKED_AT = os.environ.get("WX_ASKED_AT") or None
WX_DBZ = float(os.environ.get("WX_DBZ", "40"))
WX_MAX_CELLS = int(os.environ.get("WX_MAX_CELLS", "5"))  # cap cards/tick (avoid chat flood)

_watcher: dict[str, Any] = {
    "task": None, "ticks": 0, "last_tick": 0.0, "last_error": None,
    "wx_strip_idx": 0, "wx_disabled": False, "wx_last_error": None, "wx_cells": 0,
}

# Static mounts. /frontend serves the Cesium UI; /data serves CZML and JSON
# samples; existing relative paths like "../data/samples/traffic.czml" keep working.
app.mount("/frontend", StaticFiles(directory=ROOT / "frontend"), name="frontend")
app.mount("/data", StaticFiles(directory=ROOT / "data"), name="data")


# --- traffic resolution (net-new; does not alter the LLM wiring above) ------

_STATE_FIELDS = [
    "icao24", "callsign", "origin_country", "time_position", "last_contact",
    "lon", "lat", "baro_alt", "on_ground", "velocity", "heading", "vert_rate",
    "sensors", "geo_alt", "squawk", "spi", "position_source",
]


def _recorder_states() -> list[dict]:
    """Latest cached traffic snapshot written by scripts/record_overnight.py.
    NE-corridor only, usually <60s old. The default when no city is named."""
    matches = sorted(glob.glob(str(ROOT / "data/overnight/traffic/traffic_*.jsonl")))
    if not matches:
        return []
    last = None
    with open(matches[-1]) as fh:
        for line in fh:
            if line.strip():
                last = json.loads(line)
    if not last:
        return []
    return [dict(zip(_STATE_FIELDS, s)) for s in (last.get("states") or [])]


def _fetch_traffic_bbox(lat: float, lon: float, rad_deg: float = 0.6) -> list[dict]:
    """Live-fetch OpenSky state vectors for a bbox centered on (lat, lon).
    ~1 OpenSky credit per call; used when the user names a known city/airport."""
    params = {
        "lamin": lat - rad_deg, "lamax": lat + rad_deg,
        "lomin": lon - rad_deg, "lomax": lon + rad_deg,
    }
    try:
        r = opensky_get("https://opensky-network.org/api/states/all", params=params, timeout=10)
        if r.status_code != 200:
            return []
        states = (r.json() or {}).get("states") or []
    except Exception:
        return []
    out = []
    for s in states:
        padded = list(s) + [None] * (len(_STATE_FIELDS) - len(s))
        out.append(dict(zip(_STATE_FIELDS, padded)))
    return out


def _resolve_traffic(message: str) -> list[dict]:
    """Traffic source priority: explicit known city/airport in the message →
    live OpenSky for that bbox; otherwise the recorder's cached NE snapshot."""
    msg_low = message.lower()
    for name, (lat, lon, rad_deg) in COORDINATOR.KNOWN_AREAS.items():
        if name in msg_low:
            states = _fetch_traffic_bbox(lat, lon, rad_deg)
            if states:
                return states
            break
    return _recorder_states()


@app.get("/")
def root():
    return FileResponse(ROOT / "frontend" / "index.html")


# Pinned conversation history per browser session would normally key by cookie
# or token. For a hackathon demo we keep one global history and let the client
# pass it back on each request — stateless server, simpler.

@app.post("/api/chat")
async def api_chat(req: Request):
    body = await req.json()
    msg = (body.get("message") or "").strip()
    history = body.get("history") or []
    if not msg:
        return JSONResponse({"error": "empty message"}, status_code=400)
    try:
        # Use multi-agent coordinator (LLM mode enabled).
        # Resolve traffic so location-aware questions can filter aircraft.
        states = _resolve_traffic(msg)
        result = COORDINATOR.handle_user(msg, history=history, traffic_states=states)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return result


@app.post("/api/chat/stream")
async def api_chat_stream(req: Request):
    """NDJSON stream of {type, ...} events. Frontend reads line-by-line."""
    body = await req.json()
    msg = (body.get("message") or "").strip()
    history = body.get("history") or []
    if not msg:
        return JSONResponse({"error": "empty message"}, status_code=400)

    def gen():
        try:
            for ev in chat_stream(msg, history=history):
                yield json.dumps(ev) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ============================================================
# PROACTIVE ALERTS — background watcher + SSE push
# ============================================================
# A background task polls NOAA (G-AIRMET/SIGMET/PIREP), NWS active alerts, and
# OpenSky every WATCH_INTERVAL_SEC, routes the raw data through every
# interested specialist, and runs the Coordinator's cross-correlation pass.
# Specialists publish findings onto the bus; the SSE endpoint tails the bus and
# forwards anything at/above the push threshold to subscribed browsers, which
# render them in the chat panel unprompted.


def _collect_events() -> list[Event]:
    """Blocking data pull for one watcher tick. Runs in a thread so the event
    loop is never blocked on httpx/OpenSky I/O.

    Traffic prefers a live OpenSky pull for the watch box; if that comes back
    empty (auth hiccup / rate limit) we fall back to the recorder's cached
    snapshot so the safety + correlation passes still have aircraft to chew on.
    """
    evs: list[Event] = _weather_events(live=True)
    evs.extend(_nws_events())

    lat, lon = WATCH_CENTER
    states = _fetch_traffic_bbox(lat, lon, WATCH_RADIUS_DEG)
    if not states:
        t = _traffic_event()
        states = (t.payload.get("states") or []) if t else []
    if states:
        evs.append(Event(type="traffic.snapshot", payload={"states": states}))
    return evs


def _fetch_advisories_raw() -> list[dict]:
    """RAW G-AIRMET + SIGMET advisory dicts (each carrying `coords`) for the
    turbulence-area corroboration — NOT the digested tool versions, which strip
    the polygon geometry pireps_to_hazards needs."""
    out: list[dict] = []
    for path in ("gairmet", "airsigmet"):
        try:
            r = httpx.get(f"{AW_API}/{path}", params={"format": "json"}, timeout=10)
            data = r.json() if r.status_code == 200 else None
            if isinstance(data, list):
                out.extend(data)
        except Exception:
            continue
    return out


def _fetch_turbulence_inputs() -> tuple[list[dict], list[dict]]:
    """Blocking pull of the two Track-1 inputs for the watch box: digested PIREPs
    (get_pireps' `reports` shape) and RAW G-AIRMET/SIGMET advisories with
    polygons. Runs in a thread."""
    lat, lon = WATCH_CENTER
    rad = WATCH_RADIUS_DEG
    try:
        pr = get_pireps(lat - rad, lat + rad, lon - rad, lon + rad, age_hours=6)
        pireps = pr.get("reports") or []
    except Exception:
        pireps = []
    return pireps, _fetch_advisories_raw()


def _collect_convective(strip_idx: int) -> tuple[list[Finding], tuple[float, float] | None]:
    """Blocking pull for one convective-track tick (runs in a thread — np.load is
    blocking). Picks the strip at `strip_idx` (wrapping over the ~73-strip, 18h
    forecast), contours refc>=WX_DBZ into storm-cell Findings, returns the
    strongest WX_MAX_CELLS plus the centroid of the strongest cell."""
    strips = scenario_wx.list_strips(WX_ASKED_AT, "refc")
    if not strips:
        return [], None
    strip = strips[strip_idx % len(strips)]
    hazards = scenario_wx.hazard_polygons(WX_ASKED_AT, strip.valid_from, dbz=WX_DBZ)
    hazards = hazards[:WX_MAX_CELLS]  # already strongest-first
    centroid = None
    if hazards:
        poly = hazards[0].metadata.get("polygon") or []
        if poly:
            centroid = (sum(p[0] for p in poly) / len(poly),
                        sum(p[1] for p in poly) / len(poly))
    return hazards, centroid


async def _alert_watcher() -> None:
    while True:
        try:
            events = await asyncio.to_thread(_collect_events)

            # Route each event to every specialist that cares; publish findings.
            for ev in events:
                for s in SPECIALISTS:
                    if ev.type in s.interests():
                        for f in s.formulate(ev):
                            bus.publish(f)

            # Cross-correlate the latest traffic against active hazard polygons.
            traffic_states = next(
                (e.payload.get("states") or [] for e in events if e.type == "traffic.snapshot"),
                [],
            )
            if traffic_states:
                hazards = [
                    f for f in bus.latest(50)
                    if f.specialist == "weather"
                    and f.severity >= 3
                    and (f.metadata or {}).get("polygon")
                ]
                if hazards:
                    COORDINATOR.correlate(traffic_states, hazards)

            # --- Track 1: PIREP -> turbulence-area hazards -> transit prediction ---
            # Pilot ground-truth turned into confirmed/inferred area Findings, then
            # projected against the same traffic snapshot.
            pireps, advisories = await asyncio.to_thread(_fetch_turbulence_inputs)
            turb_hazards = pireps_to_hazards(pireps, advisories, min_intensity="MOD")
            for hz in turb_hazards:
                bus.publish(hz)  # "PIREP CONFIRMS…" / "INFERRED…" (sev>=3 -> SSE push)
            if turb_hazards and traffic_states:
                COORDINATOR.correlate(traffic_states, turb_hazards)

            # --- Track 2: HRRR convective grids -> storm-cell hazards -> transit ---
            # Opt-in (WX_ASKED_AT). Pairs *live* aircraft with a *forecast* storm
            # field — demonstrates the prediction mechanism on real planes. The
            # fully coherent scenario (reconstructed routes.json traffic) is the
            # integration script scripts/coherent_demo.py.
            if WX_ASKED_AT and not _watcher["wx_disabled"]:
                try:
                    wx_hazards, centroid = await asyncio.to_thread(
                        _collect_convective, _watcher["wx_strip_idx"])
                    _watcher["wx_strip_idx"] += 1
                    _watcher["wx_cells"] = len(wx_hazards)
                    for hz in wx_hazards:
                        bus.publish(hz)
                    if wx_hazards and centroid:
                        wx_traffic = await asyncio.to_thread(
                            _fetch_traffic_bbox, centroid[0], centroid[1], WATCH_RADIUS_DEG)
                        if wx_traffic:
                            COORDINATOR.correlate(wx_traffic, wx_hazards)
                    _watcher["wx_last_error"] = None
                except Exception as e:
                    _watcher["wx_disabled"] = True
                    _watcher["wx_last_error"] = f"{type(e).__name__}: {e}"
                    print(f"convective track disabled: {_watcher['wx_last_error']}", flush=True)

            _watcher["ticks"] += 1
            _watcher["last_tick"] = time.time()
            _watcher["last_error"] = None
        except Exception as e:  # never let one bad tick kill the loop
            _watcher["last_error"] = f"{type(e).__name__}: {e}"
            print(f"alert watcher error: {_watcher['last_error']}", flush=True)
        await asyncio.sleep(WATCH_INTERVAL_SEC)


@app.on_event("startup")
async def _start_watcher() -> None:
    if _watcher["task"] is None or _watcher["task"].done():
        _watcher["task"] = asyncio.create_task(_alert_watcher())


@app.get("/api/alerts/stream")
async def alerts_stream(request: Request):
    """Server-Sent Events stream of high-severity (>=PUSH_THRESHOLD) findings.

    Subscribes to the in-process bus and forwards each qualifying finding as an
    SSE `data:` frame carrying the finding's `chat_render()` payload. The bus's
    blocking subscribe() iterator runs in a daemon thread that bridges into an
    asyncio.Queue; the async generator drains the queue, emits keepalives, and
    tears the bridge down when the client disconnects.
    """
    loop = asyncio.get_running_loop()
    aq: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()
    # Skip the ring-replay backlog the bus hands a new subscriber — we only want
    # to push findings that land *after* this client connected.
    start_ts = time.time()

    def pump() -> None:
        gen = bus.subscribe()
        try:
            for item in gen:
                if stop.is_set():
                    break
                loop.call_soon_threadsafe(aq.put_nowait, item)
        finally:
            gen.close()

    threading.Thread(target=pump, daemon=True).start()

    async def event_stream():
        yield ": connected\n\n"
        try:
            while True:
                try:
                    item = await asyncio.wait_for(aq.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # comment frame — keeps proxies warm
                    continue
                ts = getattr(item, "timestamp", 0) or 0
                if ts < start_ts:
                    continue  # stale ring-replay item
                if isinstance(item, Finding) and COORDINATOR.should_push(item):
                    yield f"data: {json.dumps(item.chat_render())}\n\n"
        finally:
            stop.set()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
        },
    )


@app.get("/api/alerts/status")
def alerts_status():
    return {
        "watcher_running": bool(_watcher["task"] and not _watcher["task"].done()),
        "ticks": _watcher["ticks"],
        "seconds_since_last_tick": (time.time() - _watcher["last_tick"]) if _watcher["last_tick"] else None,
        "last_error": _watcher["last_error"],
        "push_threshold": COORDINATOR.PUSH_THRESHOLD,
        "findings_in_bus": len(bus.latest(1000)),
        "convective": {
            "enabled": bool(WX_ASKED_AT),
            "scenario": WX_ASKED_AT,
            "disabled": _watcher["wx_disabled"],
            "strip_idx": _watcher["wx_strip_idx"],
            "cells_last_tick": _watcher["wx_cells"],
            "last_error": _watcher["wx_last_error"],
        },
    }


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "opensky_authed": bool(os.environ.get("OPENSKY_CLIENT_ID")),
        "model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
    }
