"""Overnight ADS-B + weather recorder. Run under caffeinate -is.

Usage:
    caffeinate -is .venv/bin/python scripts/record_overnight.py

Behavior:
    - Polls OpenSky every 60s for Northeast bbox (BOS-DCA corridor)
    - Polls aviationweather.gov every 5min for METAR/TAF/SIGMET/G-AIRMET
    - Writes hourly JSONL files for traffic, snapshot files for weather
    - Exponential backoff on 429s, logs everything
    - Ctrl-C exits cleanly

Output:
    data/overnight/traffic/traffic_YYYYMMDD_HH.jsonl
    data/overnight/weather/{metar,taf,airsigmet,gairmet}_YYYYMMDDTHHMM.json
    data/overnight/recorder.log
"""
from __future__ import annotations
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# late import: needs env loaded
sys.path.insert(0, str(ROOT))
from agent.opensky_auth import auth_headers, get_token  # noqa: E402
OUT_TRAFFIC = ROOT / "data" / "overnight" / "traffic"
OUT_WEATHER = ROOT / "data" / "overnight" / "weather"
LOG_PATH = ROOT / "data" / "overnight" / "recorder.log"

# NYC + Boston corridor — captures BOS, JFK, EWR, LGA, BDL, PVD, HPN, MHT.
# ~20 sq deg → 1 credit/call authenticated (vs 4 for CONUS). At 60s cadence
# that's 60 cred/hr against a 4000/day budget → effectively unlimited.
BBOX = {"lamin": 40.0, "lamax": 44.0, "lomin": -74.0, "lomax": -69.0}

TRAFFIC_INTERVAL = 60         # seconds between OpenSky polls
WEATHER_INTERVAL = 300        # seconds between weather polls

WEATHER_ENDPOINTS = {
    "metar": "https://aviationweather.gov/api/data/metar?ids=KBOS,KJFK,KLGA,KEWR,KPHL,KBWI,KDCA,KORD,KATL,KDFW&format=json",
    "taf":   "https://aviationweather.gov/api/data/taf?ids=KBOS,KJFK,KLGA,KEWR&format=json",
    "airsigmet": "https://aviationweather.gov/api/data/airsigmet?format=json",
    "gairmet": "https://aviationweather.gov/api/data/gairmet?format=json",
}

OPENSKY_URL = (
    "https://opensky-network.org/api/states/all"
    f"?lamin={BBOX['lamin']}&lamax={BBOX['lamax']}"
    f"&lomin={BBOX['lomin']}&lomax={BBOX['lomax']}"
)

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def fetch(url: str, timeout: float = 20.0, headers: dict | None = None) -> dict | list | None:
    """GET JSON with bounded retries + exp backoff."""
    delay = 2.0
    for attempt in range(4):
        try:
            r = httpx.get(url, timeout=timeout, headers=headers or {})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 401 and headers:
                # token may have rotated mid-flight; refresh and retry once
                from agent.opensky_auth import get_token as _gt
                _gt(force=True)
                headers = auth_headers()
                continue
            if r.status_code in (429, 503):
                log(f"  backoff {r.status_code} attempt={attempt} sleep={delay}s url={url[:80]}")
                time.sleep(delay)
                delay *= 2
                continue
            log(f"  http {r.status_code} url={url[:80]}")
            return None
        except (httpx.TimeoutException, httpx.HTTPError) as e:
            log(f"  exc {type(e).__name__}: {e} attempt={attempt}")
            time.sleep(delay)
            delay *= 2
    return None


def hour_bucket() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H")


def minute_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")


def record_traffic() -> int:
    payload = fetch(OPENSKY_URL, headers=auth_headers())
    if not payload or not isinstance(payload, dict):
        return 0
    states = payload.get("states") or []
    if not states:
        log(f"  traffic empty t={payload.get('time')}")
        return 0
    OUT_TRAFFIC.mkdir(parents=True, exist_ok=True)
    out = OUT_TRAFFIC / f"traffic_{hour_bucket()}.jsonl"
    record = {
        "fetched_at": int(time.time()),
        "api_time": payload.get("time"),
        "bbox": BBOX,
        "states": states,
    }
    with out.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return len(states)


def record_weather() -> dict[str, int]:
    OUT_WEATHER.mkdir(parents=True, exist_ok=True)
    stamp = minute_stamp()
    results = {}
    for name, url in WEATHER_ENDPOINTS.items():
        data = fetch(url)
        if data is None:
            results[name] = -1
            continue
        path = OUT_WEATHER / f"{name}_{stamp}.json"
        path.write_text(json.dumps(data))
        results[name] = len(data) if isinstance(data, list) else 1
    return results


def main() -> None:
    tok = get_token()
    log(f"start auth={'oauth2' if tok else 'anonymous'} bbox={BBOX}")
    last_weather = 0.0
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True
        log("signal received, exiting")

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    iteration = 0
    while not stop["flag"]:
        iteration += 1
        t0 = time.time()
        n_aircraft = record_traffic()
        log(f"iter={iteration} traffic={n_aircraft}")

        if t0 - last_weather >= WEATHER_INTERVAL:
            w = record_weather()
            log(f"iter={iteration} weather={w}")
            last_weather = t0

        # sleep to next traffic tick, but wake periodically to check stop flag
        elapsed = time.time() - t0
        remaining = max(0.0, TRAFFIC_INTERVAL - elapsed)
        slept = 0.0
        while slept < remaining and not stop["flag"]:
            chunk = min(2.0, remaining - slept)
            time.sleep(chunk)
            slept += chunk

    log("stopped cleanly")


if __name__ == "__main__":
    sys.exit(main())
