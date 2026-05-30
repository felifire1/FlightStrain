"""FastAPI server: serves the Cesium frontend and exposes /api/chat.

Single origin replaces the bare `python -m http.server` so the browser can POST
to the agent without CORS heroics.

Run:
    .venv/bin/uvicorn agent.server:app --host 127.0.0.1 --port 8000 --reload
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from agent.loop import chat, chat_stream  # noqa: E402
import json  # noqa: E402

app = FastAPI(title="ASI Hack — 4D Airspace + Agent")

# Static mounts. /frontend serves the Cesium UI; /data serves CZML and JSON
# samples; existing relative paths like "../data/samples/traffic.czml" keep working.
app.mount("/frontend", StaticFiles(directory=ROOT / "frontend"), name="frontend")
app.mount("/data", StaticFiles(directory=ROOT / "data"), name="data")


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
        result = chat(msg, history=history)
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


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "opensky_authed": bool(os.environ.get("OPENSKY_CLIENT_ID")),
        "model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
    }
