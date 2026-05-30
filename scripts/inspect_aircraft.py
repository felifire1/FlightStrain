"""Inspect the status of aircraft in a bbox using the latest recorder snapshot.

Usage:
    .venv/bin/python scripts/inspect_aircraft.py             # BOS metro default
    .venv/bin/python scripts/inspect_aircraft.py --bbox 42.0 42.7 -71.5 -70.5
    .venv/bin/python scripts/inspect_aircraft.py --live      # live OpenSky fetch instead of cache

Classifies each aircraft as: ON GROUND / TAXIING / CLIMB-DESCENT / CRUISE / HOLDING / SLOW / UNKNOWN.
Shows callsign, altitude, speed, heading, vert_rate, on_ground flag, and age of last position fix.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Field offsets for OpenSky state vectors
F_ICAO, F_CALL, F_TPOS, F_LON, F_LAT, F_BARO, F_GROUND, F_VEL, F_HDG, F_VRATE, F_GEO = \
    0, 1, 3, 5, 6, 7, 8, 9, 10, 11, 13


def classify(s: dict) -> str:
    if s.get("on_ground"):
        vel = s.get("velocity") or 0
        return "TAXIING" if vel >= 3 else "ON GROUND"
    alt_ft = (s.get("baro_alt") or 0) * 3.28084
    vel_kt = (s.get("velocity") or 0) * 1.94384
    vr = s.get("vert_rate") or 0
    if alt_ft < 100:
        return "ON GROUND?"  # contradicts on_ground=False but happens
    if abs(vr) > 5:
        return "CLIMB" if vr > 0 else "DESCENT"
    if vel_kt < 100:
        return "SLOW/HOLD"
    if alt_ft > 25000:
        return "CRUISE"
    return "ENROUTE"


def get_states_cached(bbox):
    """Read latest recorder snapshot, filter to bbox."""
    files = sorted((ROOT / "data/overnight/traffic").glob("*.jsonl"))
    if not files:
        return [], 0
    last = None
    with files[-1].open() as f:
        for line in f:
            if line.strip():
                last = json.loads(line)
    if not last:
        return [], 0
    fields = ["icao24","callsign","origin_country","time_position","last_contact",
              "lon","lat","baro_alt","on_ground","velocity","heading","vert_rate",
              "sensors","geo_alt","squawk","spi","position_source"]
    api_t = last.get("api_time")
    states = []
    for st in last.get("states", []):
        d = dict(zip(fields, st + [None]*(len(fields)-len(st))))
        if d.get("lat") is None or d.get("lon") is None:
            continue
        lamin, lamax, lomin, lomax = bbox
        if not (lamin <= d["lat"] <= lamax and lomin <= d["lon"] <= lomax):
            continue
        states.append(d)
    return states, api_t


def get_states_live(bbox):
    from agent.opensky_auth import authed_get
    lamin, lamax, lomin, lomax = bbox
    r = authed_get("https://opensky-network.org/api/states/all",
                   params={"lamin":lamin,"lamax":lamax,"lomin":lomin,"lomax":lomax}, timeout=10)
    if r.status_code != 200:
        print(f"OpenSky returned {r.status_code}", file=sys.stderr)
        return [], 0
    p = r.json() or {}
    fields = ["icao24","callsign","origin_country","time_position","last_contact",
              "lon","lat","baro_alt","on_ground","velocity","heading","vert_rate",
              "sensors","geo_alt","squawk","spi","position_source"]
    return [dict(zip(fields, st + [None]*(len(fields)-len(st)))) for st in (p.get("states") or [])], p.get("time")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", nargs=4, type=float, default=[42.0, 42.7, -71.5, -70.5],
                    metavar=("LAMIN","LAMAX","LOMIN","LOMAX"))
    ap.add_argument("--live", action="store_true", help="live OpenSky fetch instead of cached")
    args = ap.parse_args()

    if args.live:
        from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
        states, api_t = get_states_live(tuple(args.bbox))
    else:
        states, api_t = get_states_cached(tuple(args.bbox))

    now = time.time()
    age = (now - api_t) if api_t else None
    print(f"\nbbox: {args.bbox}")
    print(f"source: {'LIVE' if args.live else 'cached recorder snapshot'}")
    print(f"snapshot time: {time.strftime('%H:%M:%SZ', time.gmtime(api_t)) if api_t else '?'}  (age {age:.0f}s)" if age else "")
    print(f"aircraft in bbox: {len(states)}\n")

    rows = []
    for s in states:
        cls = classify(s)
        alt_ft = int((s.get("baro_alt") or 0) * 3.28084)
        vel_kt = int((s.get("velocity") or 0) * 1.94384)
        vr_fpm = int((s.get("vert_rate") or 0) * 196.85)
        t_pos = s.get("time_position")
        pos_age = int(api_t - t_pos) if t_pos and api_t else None
        cs = (s.get("callsign") or "").strip() or s.get("icao24")
        rows.append((cls, cs, alt_ft, vel_kt, vr_fpm, s.get("heading") or 0,
                     "G" if s.get("on_ground") else "A", pos_age, s.get("icao24")))

    # Sort: ground first, then by altitude desc
    rows.sort(key=lambda r: (r[6] != "G", -r[2]))

    print(f"{'STATUS':<12} {'CALLSIGN':<10} {'ALT_FT':>7} {'KT':>4} {'FPM':>6} {'HDG':>4} {'GND':>3} {'AGE':>4}  ICAO24")
    print("-" * 70)
    for cls, cs, alt, vel, vr, hdg, g, age, icao in rows:
        age_s = f"{age}s" if age is not None else "?"
        print(f"{cls:<12} {cs:<10} {alt:>7} {vel:>4} {vr:>6} {int(hdg):>4} {g:>3} {age_s:>4}  {icao}")

    # Summary
    from collections import Counter
    counts = Counter(r[0] for r in rows)
    print()
    print("by status:", dict(counts))


if __name__ == "__main__":
    main()
