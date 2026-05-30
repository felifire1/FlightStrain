"""FastAPI server: serves the Cesium frontend and exposes /api/chat.

Single origin replaces the bare `python -m http.server` so the browser can POST
to the agent without CORS heroics.

As of T1, /api/chat is backed by the **multi-agent specialist constellation**
(agent/specialists/) rather than the single-agent loop in agent/loop.py. The
Coordinator pulls findings from the WeatherAgent / TrafficAgent / SafetyAgent /
FleetAgent / NarratorAgent, synthesizes one reply, and composes map_actions
across them. All specialists run in LLM mode — real Claude calls via the
ANTHROPIC_API_KEY in .env. See agent/specialists/README.md for the contract.

Run:
    .venv/bin/uvicorn agent.server:app --host 127.0.0.1 --port 8000 --reload
"""
from __future__ import annotations
import glob
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from agent.api_key import get_anthropic_api_key  # noqa: E402
from agent.opensky_auth import authed_get as opensky_get  # noqa: E402
from agent.specialists.bus import bus  # noqa: E402
from agent.specialists.coordinator import Coordinator  # noqa: E402
from agent.specialists.weather import WeatherAgent  # noqa: E402
from agent.specialists.traffic import TrafficAgent  # noqa: E402
from agent.specialists.safety import SafetyAgent  # noqa: E402
from agent.specialists.fleet import FleetAgent  # noqa: E402
from agent.specialists.narrator import NarratorAgent  # noqa: E402

app = FastAPI(title="ASI Hack — 4D Airspace + Multi-Agent Ops Room")

# Static mounts. /frontend serves the Cesium UI; /data serves CZML and JSON
# samples; existing relative paths like "../data/samples/traffic.czml" keep working.
app.mount("/frontend", StaticFiles(directory=ROOT / "frontend"), name="frontend")
app.mount("/data", StaticFiles(directory=ROOT / "data"), name="data")


# --- the constellation ------------------------------------------------------

SPECIALISTS = [WeatherAgent(), TrafficAgent(), SafetyAgent(), FleetAgent(), NarratorAgent()]
COORDINATOR = Coordinator(specialists=SPECIALISTS)

# Specialists use Sonnet; the Coordinator integrates everything, so it can run a
# stronger model. Both default to Sonnet to stay cost-safe; override via env.
SPECIALIST_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
COORDINATOR_MODEL = os.environ.get("COORDINATOR_MODEL", SPECIALIST_MODEL)

# Mode is "stub" until inject_llm succeeds; reported on /api/health.
LLM_MODE = "stub"


def _wire_llm() -> None:
    """Flip every specialist + the Coordinator into LLM mode by injecting an
    Anthropic caller. No-op (stay in stub mode) if no API key is configured, so
    the server still boots and answers with deterministic templates."""
    global LLM_MODE
    try:
        api_key = get_anthropic_api_key()
    except FileNotFoundError as e:
        print(f"[server] no Anthropic key found, specialists stay in stub mode: {e}", flush=True)
        return

    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)

    def make_caller(model: str):
        def llm_call(system_prompt: str, user_msg: str, tools: list) -> str:
            resp = client.messages.create(
                model=model,
                max_tokens=512,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            return "".join(b.text for b in resp.content if b.type == "text")
        return llm_call

    specialist_caller = make_caller(SPECIALIST_MODEL)
    for s in SPECIALISTS:
        s.inject_llm(specialist_caller)
    COORDINATOR.inject_llm(make_caller(COORDINATOR_MODEL))
    LLM_MODE = "llm"
    print(f"[server] specialists in LLM mode (specialists={SPECIALIST_MODEL}, "
          f"coordinator={COORDINATOR_MODEL})", flush=True)


_wire_llm()


# --- traffic resolution -----------------------------------------------------

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


# --- routes -----------------------------------------------------------------

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
        states = _resolve_traffic(msg)
        result = COORDINATOR.handle_user(msg, history=history, traffic_states=states)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return result


@app.post("/api/chat/stream")
async def api_chat_stream(req: Request):
    """NDJSON stream of {type, ...} events. Frontend reads line-by-line.

    The Coordinator's handle_user() is synchronous and returns a complete
    result, so we adapt it into the streaming event shape the frontend already
    understands: tool_use chips, map_action commands, then the synthesized text
    as a single text_delta, then done (carrying history + the multi-agent
    `voices` list)."""
    body = await req.json()
    msg = (body.get("message") or "").strip()
    history = body.get("history") or []
    if not msg:
        return JSONResponse({"error": "empty message"}, status_code=400)

    def gen():
        try:
            states = _resolve_traffic(msg)
            result = COORDINATOR.handle_user(msg, history=history, traffic_states=states)
        except Exception as e:
            yield json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}) + "\n"
            return

        for tool in result.get("tool_trace") or []:
            yield json.dumps({"type": "tool_use", "name": tool.get("name"),
                              "args": tool.get("args") or {}}) + "\n"
        for cmd in result.get("map_actions") or []:
            yield json.dumps({"type": "map_action", "cmd": cmd}) + "\n"
        yield json.dumps({"type": "text_delta", "delta": result.get("text") or ""}) + "\n"
        yield json.dumps({
            "type": "done",
            "history": result.get("history") or history,
            "tool_trace": result.get("tool_trace") or [],
            "voices": result.get("voices") or [],
        }) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "opensky_authed": bool(os.environ.get("OPENSKY_CLIENT_ID")),
        "mode": LLM_MODE,
        "specialists": [s.name for s in SPECIALISTS],
        "specialist_model": SPECIALIST_MODEL,
        "coordinator_model": COORDINATOR_MODEL,
        "findings_in_bus": len(bus.latest(1000)),
    }
