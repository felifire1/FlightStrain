"""Convert one hackathon scenario snapshot into a time-dynamic CZML for Cesium.

Samples every flight's filed great-circle route at a fixed cadence (using the
same constant-cruise model as agent.scenario_routes) and emits a CZML document
the existing frontend renders exactly like the OpenSky replay: a colored point
far out, a 3D aircraft model close in, a short trail, hover/track labels.

Entity ids are `Flight.uid` — identical to what `snapshot()` puts in each
state-dict's `icao24` — so the agent's `highlight_flight` map-action lands on
the right plane.

Usage:
    .venv/bin/python scripts/routes_to_czml.py OUT.czml
    .venv/bin/python scripts/routes_to_czml.py OUT.czml --asked-at 2025-07-01
    .venv/bin/python scripts/routes_to_czml.py OUT.czml --bbox 40 44 -74 -69 --step-sec 60
    .venv/bin/python scripts/routes_to_czml.py OUT.czml --max-flights 0   # no cap

The bundle is gitignored and expected at $HACKATHON_DATA_BUNDLE (see
agent.scenario_routes). Reuses the CZML packet shape from
scripts/opensky_to_czml.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.scenario_routes import (  # noqa: E402
    Flight, Scenario, FT_TO_M, KT_TO_MS, iso, load_scenario, position_at,
)

# Altitude-band colors (meters) — mirrors scripts/opensky_to_czml.py so the two
# layers read the same on the map.
ALT_BANDS = [
    (0,         1500,    ( 80, 200, 120, 240)),  # ground / low - green
    (1500,      4500,    (250, 200,  60, 240)),  # climb / descent - yellow
    (4500,      9000,    (255, 130,  40, 240)),  # mid - orange
    (9000,     12000,    (235,  60, 140, 240)),  # cruise - magenta
    (12000, 99999999,    ( 80, 200, 230, 240)),  # high - cyan
]


def altitude_color(alt_m: float) -> tuple[int, int, int, int]:
    for lo, hi, rgba in ALT_BANDS:
        if lo <= alt_m < hi:
            return rgba
    return ALT_BANDS[-1][2]


def _in_bbox(flight: Flight, bbox: tuple[float, float, float, float]) -> bool:
    """Keep the flight if any waypoint falls inside (lamin, lamax, lomin, lomax)."""
    lamin, lamax, lomin, lomax = bbox
    return any(lamin <= la <= lamax and lomin <= lo <= lomax
               for la, lo in zip(flight.lats, flight.lons))


def _flight_samples(flight: Flight, step_sec: float) -> list[tuple[float, float, float, float]]:
    """(t, lon, lat, alt_m) samples from take-off to landing at `step_sec`,
    always including both endpoints."""
    t0 = flight.take_off_time
    t1 = flight.scheduled_landing_time
    if t1 <= t0:
        return []
    times: list[float] = []
    t = t0
    while t < t1:
        times.append(t)
        t += step_sec
    times.append(t1)
    out = []
    for tt in times:
        pos = position_at(flight, tt)
        if pos is None:
            continue
        lat, lon, alt_ft = pos
        out.append((tt, lon, lat, alt_ft * FT_TO_M))
    return out


def build_czml(scn: Scenario, *, step_sec: float, bbox=None, max_flights: int) -> list[dict]:
    flights = scn.flights
    if bbox:
        flights = [f for f in flights if _in_bbox(f, bbox)]
    # Prefer flights that are airborne during the window; longer routes first so
    # a capped demo shows the most interesting traffic.
    flights = sorted(flights, key=lambda f: -f._total_m)
    if max_flights and max_flights > 0:
        flights = flights[:max_flights]

    # Clock spans the union of all rendered flight windows (fall back to the
    # snapshot window if somehow empty).
    if flights:
        t_min = min(f.take_off_time for f in flights)
        t_max = max(f.scheduled_landing_time for f in flights)
    else:
        t_min, t_max = scn.window_start, scn.window_end

    czml: list[dict] = [{
        "id": "document",
        "name": f"scenario-routes-{scn.source.name}",
        "version": "1.0",
        "clock": {
            "interval": f"{iso(t_min)}/{iso(t_max)}",
            "currentTime": iso(scn.asked_at if t_min <= scn.asked_at <= t_max else t_min),
            "multiplier": 60,
            "range": "LOOP_STOP",
            "step": "SYSTEM_CLOCK_MULTIPLIER",
        },
    }]

    rendered = 0
    for f in flights:
        pts = _flight_samples(f, step_sec)
        if len(pts) < 2:
            continue
        epoch = iso(pts[0][0])
        flat: list[float] = []
        for t, lon, lat, alt in pts:
            flat.extend([t - pts[0][0], lon, lat, alt])

        med_alt = sorted(p[3] for p in pts)[len(pts) // 2]
        r, g, b, a = altitude_color(med_alt)
        point_color = [r, g, b, a]
        path_color = [r, g, b, 140]

        czml.append({
            "id": f.uid,
            "name": f.flight_number,
            "availability": f"{iso(pts[0][0])}/{iso(pts[-1][0])}",
            "properties": {
                "callsign": f.flight_number,
                "icao24": f.uid,
                "origin": f.origin_airport_icao,
                "destination": f.destination_airport_icao,
                "cruise_alt_ft": f.cruise_altitude_ft,
                "cruise_speed_kt": f.cruise_speed_kt,
                "median_alt_m": med_alt,
                "alt_band_rgba": list(point_color),
            },
            "position": {
                "epoch": epoch,
                "cartographicDegrees": flat,
                "interpolationAlgorithm": "LAGRANGE",
                "interpolationDegree": 1,
            },
            "orientation": {"velocityReference": "#position"},
            "point": {
                "pixelSize": 5,
                "color": {"rgba": point_color},
                "outlineColor": {"rgba": [10, 13, 16, 200]},
                "outlineWidth": 1,
                "distanceDisplayCondition": {"distanceDisplayCondition": [400_000, 30_000_000]},
            },
            "model": {
                "gltf": "/frontend/assets/aircraft.glb",
                "minimumPixelSize": 28,
                "maximumScale": 12_000,
                "color": {"rgba": point_color},
                "colorBlendMode": "MIX",
                "colorBlendAmount": 0.55,
                "distanceDisplayCondition": {"distanceDisplayCondition": [0, 600_000]},
            },
            "label": {
                "text": f.flight_number,
                "font": "9pt 'JetBrains Mono', ui-monospace, monospace",
                "pixelOffset": {"cartesian2": [12, 0]},
                "showBackground": True,
                "backgroundColor": {"rgba": [10, 13, 16, 200]},
                "fillColor": {"rgba": [230, 235, 239, 240]},
                "scale": 0.9,
                "show": False,
            },
            "path": {
                "leadTime": 0,
                "trailTime": 600,
                "width": 1.2,
                "material": {"solidColor": {"color": {"rgba": path_color}}},
                "resolution": 30,
            },
        })
        rendered += 1

    print(f"rendered {rendered} flights  span {iso(t_min)} -> {iso(t_max)}", file=sys.stderr)
    return czml


def main() -> int:
    ap = argparse.ArgumentParser(description="Scenario routes.json -> time-dynamic CZML")
    ap.add_argument("out", help="output .czml path")
    ap.add_argument("--asked-at", default=None,
                    help="snapshot selector (token like 2025-07-01, or ISO/epoch); default earliest")
    ap.add_argument("--step-sec", type=float, default=60.0, help="sample cadence (default 60s)")
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("LAMIN", "LAMAX", "LOMIN", "LOMAX"),
                    help="keep only flights with a waypoint in this bbox")
    ap.add_argument("--max-flights", type=int, default=1500,
                    help="cap rendered flights (longest routes first); 0 = no cap")
    args = ap.parse_args()

    scn = load_scenario(args.asked_at)
    bbox = tuple(args.bbox) if args.bbox else None
    czml = build_czml(scn, step_sec=args.step_sec, bbox=bbox, max_flights=args.max_flights)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(czml))
    print(f"wrote {out}  packets={len(czml) - 1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
