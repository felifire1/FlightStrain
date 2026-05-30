"""Visualize avoidance plans as CZML: the original route (red, straight through
the storm) vs the avoided route (green) the impact engine recommends, with a
per-flight metrics label. Time-dynamic — each flight rides the Cesium clock over
its airborne window, with a moving marker along the original track.

For each shown flight we draw:
  - the hit storm cell  (translucent red extruded volume — what it's avoiding)
  - the ORIGINAL route   (red polyline, through the cell) + a moving red marker
  - the AVOIDED route    (green polyline): a lateral detour for "fly around",
                          or the same track lifted to the climb altitude for
                          "climb over" / held for "let it pass"
  - a metrics LABEL      (maneuver · fuel gal · delay min · CO2 · pax)

Geometry + economics are reused from agent.impact / agent.scenario_routes — no
reimplementation.

Usage:
    .venv/bin/python scripts/avoidance_to_czml.py \
        --asked-at 2025-08-22T18:00:00Z --out data/samples/avoidance.czml \
        [--top 12] [--multiplier 120]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import impact  # noqa: E402
from agent import scenario_routes as sr  # noqa: E402

FT_TO_M = 0.3048
NM_PER_DEG = 60.0

RED = [235, 64, 52, 255]
RED_FILL = [235, 64, 52, 70]
GREEN = [64, 220, 120, 255]
LABEL_BG = [10, 14, 18, 200]


def iso(epoch: float) -> str:
    return sr.iso(epoch)


# --- avoided-route geometry --------------------------------------------------

def _cell_centroid_radius(polygon: list[tuple[float, float]]) -> tuple[float, float, float]:
    clat = sum(p[0] for p in polygon) / len(polygon)
    clon = sum(p[1] for p in polygon) / len(polygon)
    # radius in nm = max centroid->vertex distance
    coslat = max(math.cos(math.radians(clat)), 1e-6)
    rad_nm = max(
        math.hypot((la - clat) * NM_PER_DEG, (lo - clon) * NM_PER_DEG * coslat)
        for la, lo in polygon
    )
    return clat, clon, rad_nm


def _detour(lats: list[float], lons: list[float], polygon: list[tuple[float, float]],
            standoff_nm: float) -> tuple[list[float], list[float]]:
    """Bend the route around the cell: any waypoint within (cell radius +
    standoff) of the centroid is pushed radially out to that distance."""
    clat, clon, rad_nm = _cell_centroid_radius(polygon)
    keep_nm = rad_nm + standoff_nm
    coslat = max(math.cos(math.radians(clat)), 1e-6)
    out_lat, out_lon = [], []
    for la, lo in zip(lats, lons):
        dlat_nm = (la - clat) * NM_PER_DEG
        dlon_nm = (lo - clon) * NM_PER_DEG * coslat
        d = math.hypot(dlat_nm, dlon_nm)
        if 0 < d < keep_nm:
            scale = keep_nm / d
            la = clat + (dlat_nm * scale) / NM_PER_DEG
            lo = clon + (dlon_nm * scale) / (NM_PER_DEG * coslat)
        out_lat.append(la)
        out_lon.append(lo)
    return out_lat, out_lon


def _positions(lats: list[float], lons: list[float], alt_m: float) -> list[float]:
    out: list[float] = []
    for la, lo in zip(lats, lons):
        out.extend([lo, la, alt_m])
    return out


def _sampled_track(flight, t0: float, t1: float, alt_m: float, step_s: float = 300.0) -> list[float]:
    """Time-tagged [t, lon, lat, alt, ...] samples for a moving marker."""
    out: list[float] = []
    t = t0
    while t <= t1:
        pos = sr.position_at(flight, t)
        if pos:
            lat, lon, _ = pos
            out.extend([iso(t), lon, lat, alt_m])
        t += step_s
    return out


# --- CZML build --------------------------------------------------------------

def build_czml(asked_at: str, top: int = 12, multiplier: int = 120) -> list[dict]:
    res = impact.fleet_impact(asked_at)
    plans = res["plans"]
    scn = sr.load_scenario(asked_at)
    by_uid = {f.uid: f for f in scn.flights}

    # rank by 2D (around-only) cost — the most expensive-to-avoid flights are the
    # most interesting to show.
    def around_cost(p):
        opt = impact._around_only(p) or impact.cheapest(p, p.actype)
        return impact.weighted_cost(impact.score(opt, p.actype)) if opt else 0.0

    plans = sorted(plans, key=around_cost, reverse=True)[:top]

    spans = [(by_uid[p.flight_id].take_off_time, by_uid[p.flight_id].scheduled_landing_time)
             for p in plans if p.flight_id in by_uid]
    clk_from = min(s[0] for s in spans) if spans else scn.window_start
    clk_to = max(s[1] for s in spans) if spans else scn.window_end

    czml: list[dict] = [{
        "id": "document",
        "name": f"avoidance-{asked_at}",
        "version": "1.0",
        "clock": {
            "interval": f"{iso(clk_from)}/{iso(clk_to)}",
            "currentTime": iso(clk_from),
            "multiplier": multiplier,
            "range": "LOOP_STOP",
            "step": "SYSTEM_CLOCK_MULTIPLIER",
        },
    }]

    for p in plans:
        fl = by_uid.get(p.flight_id)
        if fl is None:
            continue
        avail = f"{iso(fl.take_off_time)}/{iso(fl.scheduled_landing_time)}"
        cruise_m = p.cruise_alt_ft * FT_TO_M

        chosen = impact.cheapest(p, p.actype)
        sc = impact.score(chosen, p.actype)
        kind = chosen.kind

        # avoided geometry by maneuver
        if kind == "lateral":
            a_lats, a_lons = _detour(p.lats, p.lons, p.cell_polygon, impact.CONVECTIVE_STANDOFF_NM)
            a_alt_m = cruise_m
        elif kind == "climb":
            a_lats, a_lons = p.lats, p.lons
            a_alt_m = (chosen.target_alt_ft or p.cruise_alt_ft) * FT_TO_M
        else:  # wait — same track, same altitude; the cost is time (see label)
            a_lats, a_lons = p.lats, p.lons
            a_alt_m = cruise_m

        # 1) the storm cell being avoided (translucent red volume)
        if p.cell_polygon:
            ring = []
            for la, lo in p.cell_polygon:
                ring.extend([lo, la, 0.0])
            czml.append({
                "id": f"cell-{p.flight_id}",
                "name": "convective cell",
                "availability": avail,
                "polygon": {
                    "positions": {"cartographicDegrees": ring},
                    "height": 0.0,
                    "extrudedHeight": cruise_m + 2000.0,
                    "material": {"solidColor": {"color": {"rgba": RED_FILL}}},
                    "outline": True,
                    "outlineColor": {"rgba": [235, 64, 52, 160]},
                },
            })

        # 2) original route (red, through the storm)
        czml.append({
            "id": f"orig-{p.flight_id}",
            "name": f"{p.callsign} original",
            "availability": avail,
            "polyline": {
                "positions": {"cartographicDegrees": _positions(p.lats, p.lons, cruise_m)},
                "width": 2,
                "material": {"solidColor": {"color": {"rgba": RED}}},
                "clampToGround": False,
            },
        })

        # 3) avoided route (green)
        czml.append({
            "id": f"avoid-{p.flight_id}",
            "name": f"{p.callsign} avoided ({kind})",
            "availability": avail,
            "polyline": {
                "positions": {"cartographicDegrees": _positions(a_lats, a_lons, a_alt_m)},
                "width": 3,
                "material": {"polylineGlow": {"glowPower": 0.2, "color": {"rgba": GREEN}}},
                "clampToGround": False,
            },
        })

        # 4) moving marker + metrics label on the original track
        track = _sampled_track(fl, fl.take_off_time, fl.scheduled_landing_time, cruise_m)
        metrics = (f"{p.callsign}  [{kind.upper()}]\n"
                   f"{sc['fuel_gal']:.0f} gal · {sc['delay_min']:.0f} min · "
                   f"{sc['co2_kg']:.0f} kg CO2\n"
                   f"{sc['turb_min_avoided']:.0f} turb-min avoided · {sc['pax']:.0f} pax")
        marker: dict = {
            "id": f"mark-{p.flight_id}",
            "name": p.callsign,
            "availability": avail,
            "point": {"pixelSize": 8, "color": {"rgba": RED},
                      "outlineColor": {"rgba": [255, 255, 255, 200]}, "outlineWidth": 1},
            "label": {
                "text": metrics,
                "font": "11px ui-monospace, monospace",
                "style": "FILL",
                "fillColor": {"rgba": [230, 235, 239, 255]},
                "showBackground": True,
                "backgroundColor": {"rgba": LABEL_BG},
                "pixelOffset": {"cartesian2": [12, 0]},
                "horizontalOrigin": "LEFT",
                "scale": 1.0,
            },
        }
        if track:
            marker["position"] = {"epoch": iso(fl.take_off_time), "cartographicDegrees": track,
                                  "interpolationAlgorithm": "LINEAR", "interpolationDegree": 1}
        else:
            marker["position"] = {"cartographicDegrees": [p.lons[0], p.lats[0], cruise_m]}
        czml.append(marker)

    build_czml.last_stats = {  # type: ignore[attr-defined]
        "shown": len(plans), "from": iso(clk_from), "to": iso(clk_to),
    }
    return czml


def main() -> None:
    ap = argparse.ArgumentParser(description="Avoidance plans -> red/green route CZML")
    ap.add_argument("--asked-at", default="2025-08-22T18:00:00Z", help="scenario timestamp")
    ap.add_argument("--out", default="data/samples/avoidance.czml", help="output CZML path")
    ap.add_argument("--top", type=int, default=12, help="how many flights to draw (by avoidance cost)")
    ap.add_argument("--multiplier", type=int, default=120, help="CZML clock playback multiplier")
    args = ap.parse_args()

    czml = build_czml(args.asked_at, top=args.top, multiplier=args.multiplier)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(czml))

    st = build_czml.last_stats  # type: ignore[attr-defined]
    print(f"wrote {out_path}")
    print(f"  asked_at={args.asked_at}  flights drawn={st['shown']}  packets={len(czml)}")
    print(f"  clock {st['from']} -> {st['to']}  (multiplier {args.multiplier}x)")


if __name__ == "__main__":
    main()
