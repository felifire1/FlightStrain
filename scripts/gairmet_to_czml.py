"""Convert a G-AIRMET JSON snapshot into a CZML document with extruded 3D polygons.

Each turbulence advisory becomes a translucent volume between its base altitude
and top altitude, visible during its valid time window.

Usage:
    .venv/bin/python scripts/gairmet_to_czml.py <gairmet.json> <output.czml>
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

FL_TO_M = 30.48  # feet per FL hundred -> meters: FL250 = 25000 ft = 7620 m
FT_TO_M = 0.3048


def alt_to_m(val: str | int | None) -> float:
    """G-AIRMET base/top: 'SFC', '090' (= 9000 ft), '180', '300' (= FL300)."""
    if val is None:
        return 0.0
    s = str(val).strip().upper()
    if s in ("SFC", "GND", ""):
        return 0.0
    try:
        n = int(s)
    except ValueError:
        return 0.0
    return n * 100 * FT_TO_M  # 090 -> 9000 ft


def parse_coords(coords) -> list[tuple[float, float]] | None:
    """G-AIRMET coords is a list of {lat, lon} dicts with string values.
    Return list of (lon, lat) float tuples or None if unusable."""
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


def iso(t) -> str:
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(t, str):
        # already iso-ish
        return t.replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


SEVERITY_COLOR = {
    "LGT":     [110, 231, 160,  25],   # soft green
    "LGT-MOD": [220, 200, 100,  30],
    "MOD":     [255, 165,  80,  35],   # muted orange
    "MOD-SEV": [255, 110,  60,  50],
    "SEV":     [255,  70,  70,  70],
}


def main(in_path: Path, out_path: Path) -> None:
    raw = json.loads(in_path.read_text())
    advisories = [g for g in raw if (g.get("hazard") or "").startswith("TURB")]

    if not advisories:
        print("no turbulence advisories in input", file=sys.stderr)
        sys.exit(1)

    # doc clock spans from earliest validTime to (latest validTime + 3hr advisory window)
    valid_dts = []
    for a in advisories:
        vt = a.get("validTime")
        if not vt:
            continue
        try:
            valid_dts.append(datetime.fromisoformat(vt.replace("Z", "+00:00")))
        except ValueError:
            pass
    if valid_dts:
        t_start = iso(min(valid_dts).timestamp())
        t_end = iso((max(valid_dts) + timedelta(hours=3)).timestamp())
    else:
        now = datetime.now(timezone.utc)
        t_start = iso(now.timestamp())
        t_end = iso((now + timedelta(hours=6)).timestamp())

    # No clock packet: turbulence is an overlay, not a timeline driver. Its
    # entity-level `availability` windows handle when polygons are visible.
    # (A clock here would override the viewer's clock with multiplier=600.)
    czml = [{
        "id": "document",
        "name": "turbulence-volumes",
        "version": "1.0",
    }]

    used = 0
    for i, a in enumerate(advisories):
        pts = parse_coords(a.get("coords"))
        if not pts:
            continue
        base_m = alt_to_m(a.get("base"))
        top_m = alt_to_m(a.get("top"))
        if top_m <= base_m:
            top_m = base_m + 1000.0  # avoid zero-height
        sev = (a.get("severity") or "MOD").upper()
        color = SEVERITY_COLOR.get(sev, [255, 140, 0, 130])

        valid = a.get("validTime") or t_start
        # show a polygon for 3 hours starting at validTime (typical G-AIRMET cadence)
        try:
            t0 = datetime.fromisoformat(valid.replace("Z", "+00:00"))
            availability = f"{iso(t0.timestamp())}/{iso((t0 + timedelta(hours=3)).timestamp())}"
        except Exception:
            availability = f"{t_start}/{t_end}"

        positions = []
        for lon, lat in pts:
            positions.extend([lon, lat, base_m])

        czml.append({
            "id": f"turb-{i}",
            "name": f"{a.get('hazard')} {sev} {a.get('base')}-{a.get('top')}",
            "availability": availability,
            "polygon": {
                "positions": {"cartographicDegrees": positions},
                "height": base_m,
                "extrudedHeight": top_m,
                "material": {"solidColor": {"color": {"rgba": color}}},
                "outline": True,
                "outlineColor": {"rgba": [color[0], color[1], color[2], 255]},
            },
        })
        used += 1

    out_path.write_text(json.dumps(czml))
    print(f"wrote {out_path}  volumes={used}/{len(advisories)}  span={t_start} -> {t_end}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: gairmet_to_czml.py <gairmet.json> <output.czml>", file=sys.stderr)
        sys.exit(2)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
