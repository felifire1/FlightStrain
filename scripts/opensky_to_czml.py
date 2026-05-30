"""Convert recorded OpenSky JSONL traffic into a CZML document for Cesium.

Usage:
    .venv/bin/python scripts/opensky_to_czml.py <input.jsonl> <output.czml>

Input: one JSON object per line, each with fields:
    {fetched_at, api_time, bbox, states: [[icao24, callsign, country, time_pos, last_contact, lon, lat, baro_alt, on_ground, velocity, heading, vert_rate, ...], ...]}

Output: CZML array — document packet first, then one packet per aircraft with
a time-sampled `position` track. Cesium will interpolate and animate.

Gap filling: between observed samples we use dead reckoning (great-circle
projection along the reported heading at the reported velocity) to synthesize
intermediate points every DR_STEP_SEC seconds. Smooths out the visible
teleport-then-pause that low-frequency polling otherwise produces.
"""
from __future__ import annotations
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# OpenSky state vector field offsets
F_ICAO, F_CALL, F_COUNTRY, F_TPOS, F_TCON = 0, 1, 2, 3, 4
F_LON, F_LAT, F_BARO, F_GROUND = 5, 6, 7, 8
F_VEL, F_HDG, F_VRATE = 9, 10, 11

# Dead-reckoning parameters
DR_STEP_SEC = 5.0       # synthetic sample cadence
DR_MAX_GAP_SEC = 120.0  # don't dead-reckon across gaps > 2 min (was 180; long
                        # gaps produce ghost positions when next obs lands far away)
EARTH_R_M = 6_371_000.0

# Forward-projection beyond last observation. Keep short — extrapolating past a
# real sample is the #1 source of "plane flies off and snaps back" visual glitches.
EXTRAPOLATE_FORWARD_SEC = 20.0  # was 60

# Skip observations whose reported position is older than this vs the api fetch time.
# OpenSky returns the last time a plane self-reported, which can be hours stale.
STALE_OBS_SEC = 60.0    # was 90 — tighten to catch more cached-position artifacts

# Ground-aircraft handling: don't dead-reckon between samples where any sample
# was on_ground (taxiing / pushback). And skip aircraft whose entire track is
# on the ground from path rendering — they'd just be stationary dots with no path.
MIN_AIRBORNE_FRACTION = 0.2   # need at least 20% airborne samples to draw a path

# Outlier rejection: reject consecutive samples that imply impossible ground speed.
# 400 m/s = 778 knots, faster than any civilian aircraft including Concorde.
MAX_PLAUSIBLE_SPEED_M_S = 400.0

# Altitude band colors (meters). Color a whole aircraft by its median altitude.
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


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in meters."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


def great_circle_project(lat_deg: float, lon_deg: float, bearing_deg: float, dist_m: float) -> tuple[float, float]:
    """Project a point forward by dist_m along bearing_deg using the spherical earth model.
    Returns (lat, lon) in degrees."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    brg = math.radians(bearing_deg)
    d_over_r = dist_m / EARTH_R_M

    new_lat = math.asin(
        math.sin(lat) * math.cos(d_over_r) +
        math.cos(lat) * math.sin(d_over_r) * math.cos(brg)
    )
    new_lon = lon + math.atan2(
        math.sin(brg) * math.sin(d_over_r) * math.cos(lat),
        math.cos(d_over_r) - math.sin(lat) * math.sin(new_lat)
    )
    return math.degrees(new_lat), math.degrees(new_lon)


def main(in_path: Path, out_path: Path, bbox: tuple[float, float, float, float] | None = None) -> None:
    """If bbox=(lamin, lamax, lomin, lomax) is given, drop aircraft observations
    outside it. Reduces CZML size for region-focused demos."""
    # Observed: list of (t, lon, lat, alt_m, velocity_m_s, heading_deg, vert_rate_m_s)
    observed = defaultdict(list)
    meta = {}
    api_t_min, api_t_max = None, None  # bounds the recording window

    with in_path.open() as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            t_api = rec.get("api_time")
            if t_api is None:
                continue
            api_t_min = t_api if api_t_min is None else min(api_t_min, t_api)
            api_t_max = t_api if api_t_max is None else max(api_t_max, t_api)
            for st in rec.get("states", []):
                icao = st[F_ICAO]
                lon, lat, alt = st[F_LON], st[F_LAT], st[F_BARO]
                t_pos = st[F_TPOS]
                if lon is None or lat is None or alt is None:
                    continue
                # Filter: reject observations whose position is stale relative to fetch time
                if t_pos is None or (t_api - t_pos) > STALE_OBS_SEC:
                    continue
                if bbox is not None:
                    lamin, lamax, lomin, lomax = bbox
                    if not (lamin <= lat <= lamax and lomin <= lon <= lomax):
                        continue
                vel = st[F_VEL] if len(st) > F_VEL else None
                hdg = st[F_HDG] if len(st) > F_HDG else None
                vrt = st[F_VRATE] if len(st) > F_VRATE else None
                on_ground = bool(st[F_GROUND]) if len(st) > F_GROUND else False
                # Use api_time as the canonical timestamp — every observation from one
                # snapshot lands at the same tick, so sort order is deterministic and
                # speed math is honest. (t_pos is sometimes a stale cached value.)
                observed[icao].append((
                    float(t_api), float(lon), float(lat), float(alt),
                    float(vel) if vel is not None else None,
                    float(hdg) if hdg is not None else None,
                    float(vrt) if vrt is not None else None,
                    on_ground,
                ))
                meta.setdefault(icao, {
                    "callsign": (st[F_CALL] or "").strip() or icao,
                    "country": st[F_COUNTRY],
                })

    # Clamp doc clock span to the actual recording window
    t_min = api_t_min
    t_max = api_t_max

    # Densify each aircraft's track via dead reckoning between observations.
    # Outlier filter: drop any consecutive pair implying impossible ground speed
    # (e.g. cached OpenSky positions stitched to fresh ones).
    samples = {}
    rejected_jumps = 0
    for icao, obs in observed.items():
        # de-dup by timestamp (same snapshot can't have two entries for one plane)
        obs = sorted({o[0]: o for o in obs}.values(), key=lambda o: o[0])
        if len(obs) < 1:
            continue

        # Pass 1: walk in time order, drop any observation that implies a jump
        # faster than physically plausible from the previously-kept observation.
        cleaned = [obs[0]]
        for o in obs[1:]:
            prev = cleaned[-1]
            dt = o[0] - prev[0]
            if dt <= 0:
                continue
            dist = haversine_m(prev[2], prev[1], o[2], o[1])
            if dist / dt > MAX_PLAUSIBLE_SPEED_M_S:
                rejected_jumps += 1
                continue
            cleaned.append(o)
        obs = cleaned
        if len(obs) < 1:
            continue

        dense = [(obs[0][0], obs[0][1], obs[0][2], obs[0][3])]
        for i in range(len(obs) - 1):
            t0, lon0, lat0, alt0, vel0, hdg0, vrt0, ground0 = obs[i]
            t1, lon1, lat1, alt1, *_, ground1 = obs[i + 1]
            gap = t1 - t0
            # Skip dead-reckoning if either endpoint is on the ground — they
            # don't move smoothly along their heading vector, they taxi.
            either_ground = ground0 or ground1
            # Only dead-reckon if we have velocity+heading, gap is reasonable,
            # both endpoints airborne, and speed > 30 m/s (~60kt — below this
            # we're looking at a hold/taxi, not cruise flight).
            if (vel0 is not None and hdg0 is not None
                    and 0 < gap <= DR_MAX_GAP_SEC
                    and not either_ground
                    and vel0 >= 30.0):
                steps = max(1, int(gap // DR_STEP_SEC))
                for k in range(1, steps):
                    dt = (gap * k) / steps
                    dist = vel0 * dt
                    new_lat, new_lon = great_circle_project(lat0, lon0, hdg0, dist)
                    new_alt = alt0 + (vrt0 or 0.0) * dt
                    dense.append((t0 + dt, new_lon, new_lat, new_alt))
            dense.append((t1, lon1, lat1, alt1))

        # Extrapolate past the last observation — short window, airborne only.
        t_last, lon_last, lat_last, alt_last, vel_last, hdg_last, vrt_last, ground_last = obs[-1]
        if (vel_last is not None and hdg_last is not None
                and EXTRAPOLATE_FORWARD_SEC > 0
                and not ground_last
                and vel_last >= 30.0):
            steps = int(EXTRAPOLATE_FORWARD_SEC // DR_STEP_SEC)
            for k in range(1, steps + 1):
                dt = k * DR_STEP_SEC
                dist = vel_last * dt
                new_lat, new_lon = great_circle_project(lat_last, lon_last, hdg_last, dist)
                new_alt = alt_last + (vrt_last or 0.0) * dt
                dense.append((t_last + dt, new_lon, new_lat, new_alt))
            t_max = max(t_max or 0, t_last + EXTRAPOLATE_FORWARD_SEC)

        # Track airborne ratio so we can decide path-vs-no-path later
        airborne = sum(1 for o in obs if not o[7])
        samples[icao] = dense
        meta[icao]["airborne_fraction"] = airborne / len(obs)
        # CURRENT status: is the most recent observation on the ground?
        meta[icao]["last_ground"] = obs[-1][7]

    if t_min is None:
        print("no samples found", file=sys.stderr)
        sys.exit(1)

    # de-dup + sort per aircraft
    czml = [{
        "id": "document",
        "name": "opensky-replay",
        "version": "1.0",
        "clock": {
            "interval": f"{iso(t_min)}/{iso(t_max)}",
            "currentTime": iso(t_min),
            "multiplier": 10,
            "range": "LOOP_STOP",
            "step": "SYSTEM_CLOCK_MULTIPLIER",
        },
    }]

    for icao, pts in samples.items():
        pts = sorted(pts)
        if len(pts) < 2:
            continue
        # CZML cartographicDegrees: [t_iso_or_seconds, lon, lat, alt_m, ...]
        epoch = iso(pts[0][0])
        flat = []
        for t, lon, lat, alt in pts:
            flat.extend([t - pts[0][0], lon, lat, alt])
        info = meta[icao]

        # Color by median altitude across this aircraft's track
        alts = sorted(p[3] for p in pts)
        med_alt = alts[len(alts) // 2]
        r, g, b, a = altitude_color(med_alt)
        path_color = [r, g, b, 140]
        point_color = [r, g, b, a]

        # Dim aircraft whose CURRENT (latest) observation is on the ground.
        # An aircraft that flew in and parked is "ground" right now — dim it —
        # regardless of how much of its track was airborne.
        is_ground_now = info.get("last_ground", False)
        if is_ground_now:
            point_color = [r, g, b, 90]       # quieter dot
            path_color  = [r, g, b, 30]        # near-invisible trail

        czml.append({
            "id": icao,
            "name": info["callsign"],
            "availability": f"{iso(pts[0][0])}/{iso(pts[-1][0])}",
            "properties": {
                "callsign": info["callsign"],
                "icao24": icao,
                "origin_country": info.get("country"),
                "median_alt_m": med_alt,
                "alt_band_rgba": list(point_color),
            },
            "position": {
                "epoch": epoch,
                "cartographicDegrees": flat,
                "interpolationAlgorithm": "LAGRANGE",
                "interpolationDegree": 1,
            },
            # Auto-orient the model along its direction of travel.
            "orientation": {"velocityReference": "#position"},
            # Far away: a colored point. Close in: a 3D airplane model.
            # CZML distanceDisplayCondition value is an [near, far] array, not an object.
            "point": {
                "pixelSize": 3 if is_ground_now else 5,
                "color": {"rgba": point_color},
                "outlineColor": {"rgba": [10, 13, 16, 200]},
                "outlineWidth": 1,
                "distanceDisplayCondition": {"distanceDisplayCondition": [400_000, 30_000_000]},
            },
            "model": {
                "gltf": "/frontend/assets/aircraft.glb",
                "minimumPixelSize": 14 if is_ground_now else 28,
                "maximumScale": 6_000 if is_ground_now else 12_000,
                "color": {"rgba": point_color},
                "colorBlendMode": "MIX",
                "colorBlendAmount": 0.55,
                "distanceDisplayCondition": {"distanceDisplayCondition": [0, 600_000]},
            },
            # Labels are off by default — JS turns them on for hovered / tracked entity.
            "label": {
                "text": info["callsign"],
                "font": "9pt 'JetBrains Mono', ui-monospace, monospace",
                "pixelOffset": {"cartesian2": [12, 0]},
                "showBackground": True,
                "backgroundColor": {"rgba": [10, 13, 16, 200]},
                "fillColor": {"rgba": [230, 235, 239, 240]},
                "scale": 0.9,
                "show": False,
            },
            # Trail time governs how much path is rendered behind each plane.
            # Mostly-ground aircraft (taxiing, parked) shouldn't have a trail —
            # just a stationary dot. Airborne aircraft get a short trail.
            "path": {
                "leadTime": 0,
                # No trail if it landed (ground now) OR never airborne. Otherwise short trail.
                "trailTime": 0 if (is_ground_now or info.get("airborne_fraction", 0) < MIN_AIRBORNE_FRACTION) else 300,
                "width": 1.2,
                "material": {"solidColor": {"color": {"rgba": path_color}}},
                "resolution": 15,
            },
        })

    out_path.write_text(json.dumps(czml))
    print(f"wrote {out_path}  aircraft={len(czml)-1}  span={iso(t_min)} -> {iso(t_max)}  rejected_jumps={rejected_jumps}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", help="one or more JSONL files; last positional is the output .czml")
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("LAMIN","LAMAX","LOMIN","LOMAX"),
                    help="filter aircraft to bbox before writing CZML")
    args = ap.parse_args()
    if len(args.inputs) < 2:
        print("need at least one input and one output", file=sys.stderr); sys.exit(2)
    out = Path(args.inputs[-1])
    ins = [Path(p) for p in args.inputs[:-1]]
    bbox = tuple(args.bbox) if args.bbox else None
    if len(ins) == 1:
        main(ins[0], out, bbox=bbox)
    else:
        import tempfile, os as _os
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as tf:
            for p in ins:
                with p.open() as f:
                    for line in f:
                        if line.strip():
                            tf.write(line)
            tmp = Path(tf.name)
        try:
            main(tmp, out, bbox=bbox)
        finally:
            _os.unlink(tmp)
