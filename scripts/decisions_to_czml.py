"""Render the top N decision moments as a Cesium overlay.

For each flight flagged by `agent.auditor.find_decision_moments`, re-emit its
recorded track from the JSONL files but stylized red and with a label that
shows the dwell time. Loads alongside the main traffic.czml so judges can
literally see "this is the flight that spent 40 minutes in chop."

Usage:
    .venv/bin/python scripts/decisions_to_czml.py \
        --hours-back 4 --top-n 5 \
        --out data/samples/decisions.czml
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.auditor import find_decision_moments  # noqa: E402
from agent.aircraft_db import lookup as ac_lookup  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TRAFFIC_DIR = ROOT / "data" / "overnight" / "traffic"

# OpenSky positional fields
F_ICAO, F_CALL, F_TPOS, F_LON, F_LAT, F_BARO = 0, 1, 3, 5, 6, 7


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def load_tracks(icaos: set[str], t_min: float, t_max: float, bbox):
    """Return {icao: [(t, lon, lat, alt_m), ...]} for selected aircraft only."""
    out: dict[str, list] = defaultdict(list)
    lamin, lamax, lomin, lomax = bbox
    for path in sorted(TRAFFIC_DIR.glob("*.jsonl")):
        for line in path.open():
            if not line.strip(): continue
            rec = json.loads(line)
            t_api = rec.get("api_time")
            if t_api is None or not (t_min <= t_api <= t_max): continue
            for st in rec.get("states", []):
                if st[F_ICAO] not in icaos: continue
                lon, lat, alt = st[F_LON], st[F_LAT], st[F_BARO]
                if lon is None or lat is None or alt is None: continue
                if not (lamin <= lat <= lamax and lomin <= lon <= lomax): continue
                out[st[F_ICAO]].append((float(t_api), float(lon), float(lat), float(alt)))
    for k in out:
        out[k].sort()
    return out


def main(args):
    audit = find_decision_moments(
        t_min=time.time() - args.hours_back * 3600,
        t_max=time.time(),
        bbox=(40.0, 44.0, -74.0, -69.0),
        top_n=args.top_n,
    )
    if audit.get("error"):
        print(f"audit error: {audit['error']}", file=sys.stderr); sys.exit(1)

    moments = audit["top"]
    print(f"flagged {len(moments)} flights  total_chop={audit['total_chop_minutes']}min", file=sys.stderr)

    icaos = {m["icao24"] for m in moments}
    by_icao = {m["icao24"]: m for m in moments}

    tracks = load_tracks(
        icaos,
        time.time() - args.hours_back * 3600,
        time.time(),
        (40.0, 44.0, -74.0, -69.0),
    )

    # doc clock spans all tracks combined
    all_t = [t for pts in tracks.values() for (t, *_rest) in pts]
    if not all_t:
        print("no track points found for flagged icaos", file=sys.stderr); sys.exit(1)
    t_min, t_max = min(all_t), max(all_t)

    czml = [{
        "id": "document",
        "name": "decision-moments",
        "version": "1.0",
        "clock": {
            "interval": f"{iso(t_min)}/{iso(t_max)}",
            "currentTime": iso(t_min),
            "multiplier": 60,
            "range": "LOOP_STOP",
            "step": "SYSTEM_CLOCK_MULTIPLIER",
        },
    }]

    for icao, pts in tracks.items():
        if len(pts) < 2: continue
        m = by_icao[icao]
        epoch = iso(pts[0][0])
        flat = []
        for t, lon, lat, alt in pts:
            flat.extend([t - pts[0][0], lon, lat, alt])

        ac = ac_lookup(icao)
        tail = ac.get("registration") or m["callsign"]
        model = ac.get("model") or ""
        suffix = f" ({model})" if model else ""
        label = f"{tail}{suffix} · {m['dwell_minutes']}min {m['advisory_severity']}"

        czml.append({
            "id": f"decision-{icao}",
            "name": label,
            "availability": f"{iso(pts[0][0])}/{iso(pts[-1][0])}",
            "position": {
                "epoch": epoch,
                "cartographicDegrees": flat,
                "interpolationAlgorithm": "LAGRANGE",
                "interpolationDegree": 1,
            },
            "point": {
                "pixelSize": 11,
                "color": {"rgba": [255, 60, 60, 255]},
                "outlineColor": {"rgba": [255, 220, 220, 255]},
                "outlineWidth": 2,
            },
            "label": {
                "text": label,
                "font": "10pt 'JetBrains Mono', ui-monospace, monospace",
                "pixelOffset": {"cartesian2": [12, 0]},
                "showBackground": True,
                "backgroundColor": {"rgba": [70, 0, 0, 220]},
                "fillColor": {"rgba": [255, 230, 230, 255]},
                "scale": 0.95,
                "outlineColor": {"rgba": [255, 100, 100, 255]},
                "outlineWidth": 2,
            },
            "path": {
                "leadTime": 0,
                "trailTime": 7200,
                "width": 3.0,
                "material": {
                    "polylineGlow": {
                        "color": {"rgba": [255, 70, 70, 230]},
                        "glowPower": 0.25,
                    }
                },
                "resolution": 5,
            },
        })

    args.out.write_text(json.dumps(czml))
    print(f"wrote {args.out}  flights={len(tracks)}  span={iso(t_min)} -> {iso(t_max)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours-back", type=int, default=4)
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument("--out", type=Path, default=Path("data/samples/decisions.czml"))
    main(ap.parse_args())
