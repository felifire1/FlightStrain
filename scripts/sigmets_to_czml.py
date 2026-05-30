"""Convert active SIGMETs into a CZML overlay.

Focus: CONVECTIVE SIGMETs (thunderstorms — the single biggest disruption driver
in operational ATC). Each becomes a translucent red extruded polygon between
the surface and the storm's tops. Tops are parsed from the raw SIGMET text
("TOPS TO FL380") because the structured JSON exposes nulls for base/top.

Usage:
    .venv/bin/python scripts/sigmets_to_czml.py <airsigmet.json> <output.czml>
"""
from __future__ import annotations
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

FT_TO_M = 0.3048

# Default top when not parseable. Convective SIGMETs typically cap at FL350-FL450.
DEFAULT_TOP_FT = 40_000

# Tunable color per hazard type. CONVECTIVE is the loud one.
HAZARD_COLOR = {
    "CONVECTIVE": [255, 60, 60, 60],     # bright red, translucent
    "ICE":        [120, 180, 255, 45],   # icy blue
    "TURB":       [255, 165, 80, 40],    # orange
}

TOPS_PATTERN = re.compile(r"TOPS?\s+TO\s+FL(\d{3})", re.IGNORECASE)


def iso(ts) -> str:
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return str(ts)


def parse_coords(coords) -> list[tuple[float, float]] | None:
    """Return list of (lon, lat) tuples. SIGMET coords are dicts with float lat/lon."""
    if not coords or not isinstance(coords, list):
        return None
    pts = []
    for v in coords:
        if isinstance(v, dict) and "lat" in v and "lon" in v:
            try:
                pts.append((float(v["lon"]), float(v["lat"])))
            except (TypeError, ValueError):
                continue
    if len(pts) < 3:
        return None
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts


def parse_tops_ft(raw: str | None) -> int:
    """Pull 'TOPS TO FLxxx' from the raw SIGMET text. FL380 -> 38000 ft."""
    if not raw:
        return DEFAULT_TOP_FT
    m = TOPS_PATTERN.search(raw)
    if not m:
        return DEFAULT_TOP_FT
    return int(m.group(1)) * 100


def main(in_path: Path, out_path: Path) -> None:
    raw_data = json.loads(in_path.read_text())
    sigmets = [s for s in raw_data if s.get("hazard") == "CONVECTIVE"]

    if not sigmets:
        print("no convective SIGMETs in input", file=sys.stderr)
        sys.exit(1)

    czml = [{
        "id": "document",
        "name": "convective-sigmets",
        "version": "1.0",
        # No clock packet — this overlay rides whatever clock the viewer has.
    }]

    used = 0
    for i, s in enumerate(sigmets):
        pts = parse_coords(s.get("coords"))
        if not pts:
            continue
        hazard = s.get("hazard", "CONVECTIVE")
        color = HAZARD_COLOR.get(hazard, [255, 80, 80, 60])
        outline = [color[0], color[1], color[2], 220]

        top_ft = parse_tops_ft(s.get("rawAirSigmet"))
        base_m = 0.0
        top_m = top_ft * FT_TO_M

        # Availability window from epoch fields
        t_from = s.get("validTimeFrom")
        t_to = s.get("validTimeTo")
        if t_from and t_to:
            availability = f"{iso(t_from)}/{iso(t_to)}"
        else:
            availability = None  # always visible

        positions = []
        for lon, lat in pts:
            positions.extend([lon, lat, base_m])

        entity = {
            "id": f"sigmet-{i}",
            "name": f"CONVECTIVE SIGMET · tops FL{top_ft // 100}",
            "description": f"<pre style='color:#e6ebef;font:11px ui-monospace,monospace'>{(s.get('rawAirSigmet') or '').strip()}</pre>",
            "polygon": {
                "positions": {"cartographicDegrees": positions},
                "height": base_m,
                "extrudedHeight": top_m,
                "material": {"solidColor": {"color": {"rgba": color}}},
                "outline": True,
                "outlineColor": {"rgba": outline},
            },
        }
        if availability:
            entity["availability"] = availability
        czml.append(entity)
        used += 1

    out_path.write_text(json.dumps(czml))
    print(f"wrote {out_path}  sigmets={used}/{len(sigmets)}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: sigmets_to_czml.py <airsigmet.json> <output.czml>", file=sys.stderr)
        sys.exit(2)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
