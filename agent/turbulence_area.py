"""PIREP -> turbulence area.

The wedge of the larger turbulence-volume vision (see memory: turbulence-volume-vision).
A PIREP is a single point a pilot actually flew through -- ground truth, but a
*dot*, not an area. This module turns that dot into an *area* two ways:

  1. CORROBORATE (the credible core): test whether the PIREP point falls inside
     an active G-AIRMET / SIGMET turbulence polygon in 3D + time. If it does, the
     "area covered" IS that polygon -- now upgraded from forecast to
     pilot-confirmed. This is `pirep_to_hazard`.

  2. SYNTHESIZE (the inferred fallback): when no official polygon contains the
     point, draw a buffer circle around it (or a hull around a cluster of nearby
     reports). Clearly labelled "inferred". This is `synthesize_area` /
     `cluster_pireps`.

Either way the output is a `Finding` whose `metadata["polygon"]` is a list of
(lat, lon) tuples -- exactly the shape `Coordinator.correlate()` already
consumes -- so the predictive payoff ("N flights will transit this in 30 min")
is free reuse, and the `map_actions` use the existing `draw_polygon` /
`highlight_flight` verbs the frontend already renders.

Run `python -m agent.turbulence_area` to see it light up against data/samples.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable

from agent.specialists.base import Finding

# --- turbulence intensity -> severity ---------------------------------------
# PIREP/G-AIRMET intensity codes, worst-wins. Severity mirrors base.py's 0..5.
_INTENSITY_RANK = {"": 0, "NEG": 0, "LGT": 1, "LGT-MOD": 2, "MOD": 3,
                   "MOD-SEV": 4, "SEV": 4, "SEV-EXTM": 5, "EXTM": 5}
# Confirmed-by-pilot is worth more than a forecast: a MOD PIREP inside a MOD
# polygon is a "significant" (3) finding; SEV+ is "urgent" (4).
_INTENSITY_SEVERITY = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5}

# How far above/below a polygon's altitude band still counts as a vertical match
# (turbulence layers are fuzzy and pilots round to the nearest 1000 ft).
DEFAULT_FL_BUFFER = 20  # flight levels == 2000 ft
# Cached sample advisories predate cached PIREPs; live data aligns. Generous
# slack keeps the static-data demo working. Tighten for production.
DEFAULT_TIME_SLACK_H = 12
NM_PER_DEG = 60.0  # ~1 nm per arc-minute of latitude


# --- intensity helpers ------------------------------------------------------

def _worst_turb_intensity(pirep: dict) -> str:
    """Worst turbulence intensity across the (up to two) reported bands.

    Accepts both the raw aviationweather PIREP (tbInt1/tbInt2) and the digested
    shape from tools.get_pireps (turbulence: [{intensity, ...}]).
    """
    cands: list[str] = []
    for n in (1, 2):
        v = pirep.get(f"tbInt{n}")
        if v:
            cands.append(v.strip().upper())
    for band in pirep.get("turbulence") or []:
        v = (band or {}).get("intensity")
        if v:
            cands.append(v.strip().upper())
    if not cands:
        return ""
    return max(cands, key=lambda c: _INTENSITY_RANK.get(c, 0))


def intensity_rank(code: str) -> int:
    return _INTENSITY_RANK.get((code or "").strip().upper(), 0)


def _pirep_fl(pirep: dict) -> int | None:
    fl = pirep.get("flight_level", pirep.get("fltLvl"))
    try:
        return int(fl)
    except (TypeError, ValueError):
        return None


def _pirep_pos(pirep: dict) -> tuple[float, float] | None:
    lat, lon = pirep.get("lat"), pirep.get("lon")
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


# --- geometry ---------------------------------------------------------------

def _point_in_polygon(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting test. polygon is list of (lat, lon). Mirrors coordinator._point_in_polygon
    so this module stays importable without pulling the whole specialists stack."""
    if len(polygon) < 3:
        return False
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        lat_i, lon_i = polygon[i]
        lat_j, lon_j = polygon[j]
        if ((lon_i > lon) != (lon_j > lon)) and \
           (lat < (lat_j - lat_i) * (lon - lon_i) / ((lon_j - lon_i) or 1e-12) + lat_i):
            inside = not inside
        j = i
    return inside


def advisory_polygon(advisory: dict) -> list[tuple[float, float]]:
    """Extract a (lat, lon) polygon from a raw G-AIRMET / SIGMET advisory.

    Handles G-AIRMET `coords` ([{lat, lon}] as strings) and SIGMET `coords`
    (same shape). Returns [] if no usable geometry.
    """
    pts: list[tuple[float, float]] = []
    for c in advisory.get("coords") or []:
        try:
            pts.append((float(c["lat"]), float(c["lon"])))
        except (KeyError, TypeError, ValueError):
            continue
    return pts


def _advisory_band(advisory: dict) -> tuple[int, int]:
    """Altitude band as (base_fl, top_fl). Null -> unbounded.

    G-AIRMET uses base/top (flight levels as strings). SIGMET uses
    altitudeLow1/altitudeHi1 in *feet*.
    """
    base, top = advisory.get("base"), advisory.get("top")
    if base is None and top is None and (
        advisory.get("altitudeLow1") is not None or advisory.get("altitudeHi1") is not None
    ):
        lo = advisory.get("altitudeLow1")
        hi = advisory.get("altitudeHi1") or advisory.get("altitudeHi2")
        base = (lo / 100.0) if lo is not None else None
        top = (hi / 100.0) if hi is not None else None
    try:
        base_fl = int(float(base)) if base not in (None, "") else 0
    except (TypeError, ValueError):
        base_fl = 0
    try:
        top_fl = int(float(top)) if top not in (None, "") else 600
    except (TypeError, ValueError):
        top_fl = 600
    return base_fl, top_fl


def _fl_in_band(fl: int | None, base_fl: int, top_fl: int, buffer_fl: int) -> bool:
    if fl is None:
        return True  # no altitude reported -> don't exclude on vertical grounds
    return (base_fl - buffer_fl) <= fl <= (top_fl + buffer_fl)


def _to_epoch(v: Any) -> float | None:
    """Parse a unix int/float or an ISO8601 string (…Z) to epoch seconds."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).replace("Z", "+00:00")
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _time_match(obs_time: Any, advisory: dict, slack_h: float) -> bool:
    obs = _to_epoch(obs_time)
    if obs is None:
        return True  # unknown obs time -> don't exclude
    start = _to_epoch(advisory.get("validTime") or advisory.get("validTimeFrom"))
    end = _to_epoch(advisory.get("expireTime") or advisory.get("validTimeTo"))
    slack = slack_h * 3600
    if start is not None and obs < start - slack:
        return False
    if end is not None and obs > end + slack:
        return False
    return True


def synthesize_area(lat: float, lon: float, radius_nm: float = 30.0, n: int = 16) -> list[tuple[float, float]]:
    """A buffer circle (as an n-gon of (lat, lon)) around a point -- the
    inferred area when no official polygon contains the PIREP."""
    dlat = radius_nm / NM_PER_DEG
    coslat = max(math.cos(math.radians(lat)), 1e-6)
    dlon = radius_nm / (NM_PER_DEG * coslat)
    return [
        (lat + dlat * math.cos(2 * math.pi * k / n),
         lon + dlon * math.sin(2 * math.pi * k / n))
        for k in range(n)
    ]


# --- map-action helpers (match coordinator.correlate's draw_polygon contract) -

def _draw_polygon_action(polygon: list[tuple[float, float]], poly_id: str, label: str,
                         color: list[int], extruded_m: int) -> dict[str, Any]:
    return {
        "action": "draw_polygon",
        "id": poly_id,
        "points": [[lon, lat] for (lat, lon) in polygon],  # frontend wants [lon, lat]
        "color": color,
        "label": label,
        "height_m": 0,
        "extruded_m": extruded_m,
    }


# --- the core: PIREP -> hazard Finding --------------------------------------

def pirep_to_hazard(pirep: dict, advisories: list[dict], *,
                    min_intensity: str = "MOD",
                    fl_buffer: int = DEFAULT_FL_BUFFER,
                    time_slack_h: float = DEFAULT_TIME_SLACK_H,
                    synthesize_radius_nm: float = 30.0) -> Finding | None:
    """Turn one PIREP into a turbulence-area Finding, or None if it's below
    `min_intensity` or has no position.

    Strategy: corroborate against the advisories (3D + time). If a turbulence
    polygon contains the point -> emit a "pilot-confirmed" Finding carrying that
    polygon. Otherwise synthesize a buffer circle -> emit an "inferred" Finding.

    The returned Finding.metadata["polygon"] is (lat, lon) tuples, ready to feed
    straight into Coordinator.correlate(traffic, [finding]).
    """
    intensity = _worst_turb_intensity(pirep)
    if intensity_rank(intensity) < intensity_rank(min_intensity):
        return None
    pos = _pirep_pos(pirep)
    if pos is None:
        return None
    lat, lon = pos
    fl = _pirep_fl(pirep)
    severity = _INTENSITY_SEVERITY.get(intensity_rank(intensity), 3)
    actype = pirep.get("aircraft_type") or pirep.get("acType") or "?"
    fl_label = f"FL{fl:03d}" if fl is not None else "unknown FL"

    # 1) try to corroborate against a real turbulence advisory
    for adv in advisories:
        hazard = (adv.get("hazard") or "")
        if "TURB" not in hazard.upper() and hazard.upper() != "CONVECTIVE":
            continue
        poly = advisory_polygon(adv)
        if len(poly) < 3 or not _point_in_polygon(lat, lon, poly):
            continue
        base_fl, top_fl = _advisory_band(adv)
        if not _fl_in_band(fl, base_fl, top_fl, fl_buffer):
            continue
        if not _time_match(pirep.get("obs_time") or pirep.get("obsTime"), adv, time_slack_h):
            continue
        adv_sev = (adv.get("severity") or "").upper() or "?"
        return Finding(
            specialist="turbulence",
            severity=max(severity, 3),
            summary=(
                f"PIREP CONFIRMS turbulence area: {actype} reported {intensity} at "
                f"{fl_label} inside an active {hazard} polygon ({adv_sev}, "
                f"FL{base_fl:03d}-FL{top_fl:03d})."
            ),
            detail=(
                "Pilot ground-truth corroborates the forecast hazard. The area "
                "covered is the advisory polygon below; projecting traffic forward "
                "shows who will transit it."
            ),
            recommended_action=(
                f"Advise transit traffic FL{base_fl:03d}-FL{top_fl:03d} of confirmed "
                f"{intensity} turbulence; consider altitude/lateral reroute."
            ),
            map_actions=[
                _draw_polygon_action(poly, f"turb-confirmed-{hazard}",
                                     f"CONFIRMED {intensity} TURB ({hazard})",
                                     [255, 90, 40, 110], 14000),
                {"action": "fly_to", "lat": lat, "lon": lon, "alt_m": 220_000, "pitch_deg": -55},
            ],
            sources=["get_pireps", "get_turbulence_advisories"],
            metadata={
                "polygon": poly,                      # (lat, lon) -> feeds correlate()
                "kind": "confirmed",
                "hazard": hazard,
                "advisory_severity": adv_sev,
                "band_fl": [base_fl, top_fl],
                "pirep": {"lat": lat, "lon": lon, "fl": fl, "intensity": intensity,
                          "actype": actype, "raw": pirep.get("raw") or pirep.get("rawOb")},
            },
        )

    # 2) no official polygon contained it -> synthesize an inferred area
    poly = synthesize_area(lat, lon, radius_nm=synthesize_radius_nm)
    return Finding(
        specialist="turbulence",
        severity=severity,
        summary=(
            f"INFERRED turbulence area: {actype} reported {intensity} at {fl_label} "
            f"near ({lat:.1f}°N, {abs(lon):.1f}°W). No active advisory here -- "
            f"area is a ~{synthesize_radius_nm:.0f} nm buffer around the report."
        ),
        detail="Speculative: built from a single pilot report, not an official product.",
        recommended_action=(
            f"Caution transit traffic near {fl_label} within ~{synthesize_radius_nm:.0f} nm; "
            "no corroborating G-AIRMET/SIGMET on file."
        ),
        map_actions=[
            _draw_polygon_action(poly, "turb-inferred",
                                 f"INFERRED {intensity} TURB (PIREP)",
                                 [255, 170, 60, 80], 8000),
            {"action": "fly_to", "lat": lat, "lon": lon, "alt_m": 220_000, "pitch_deg": -55},
        ],
        sources=["get_pireps"],
        metadata={
            "polygon": poly,
            "kind": "inferred",
            "radius_nm": synthesize_radius_nm,
            "pirep": {"lat": lat, "lon": lon, "fl": fl, "intensity": intensity,
                      "actype": actype, "raw": pirep.get("raw") or pirep.get("rawOb")},
        },
    )


def pireps_to_hazards(pireps: Iterable[dict], advisories: list[dict], *,
                      min_intensity: str = "MOD", **kw) -> list[Finding]:
    """Map a batch of PIREPs to hazard Findings, de-duplicating confirmed areas
    that resolve to the same advisory polygon (many PIREPs, one storm)."""
    out: list[Finding] = []
    seen_confirmed: set[str] = set()
    for p in pireps:
        f = pirep_to_hazard(p, advisories, min_intensity=min_intensity, **kw)
        if f is None:
            continue
        if f.metadata.get("kind") == "confirmed":
            key = f.metadata.get("hazard", "") + str(f.metadata.get("band_fl"))
            if key in seen_confirmed:
                continue
            seen_confirmed.add(key)
        out.append(f)
    # confirmed first (higher confidence), then by severity
    out.sort(key=lambda f: (f.metadata.get("kind") != "confirmed", -f.severity))
    return out


# --- runnable demo against cached samples -----------------------------------

if __name__ == "__main__":
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    pireps = json.loads((root / "data/samples/pireps_us.json").read_text())
    advisories = json.loads((root / "data/samples/gairmet_all.json").read_text())
    sigmets = json.loads((root / "data/samples/airsigmet_all.json").read_text())
    advisories = advisories + (sigmets if isinstance(sigmets, list) else [])

    findings = pireps_to_hazards(pireps, advisories, min_intensity="MOD")
    confirmed = [f for f in findings if f.metadata.get("kind") == "confirmed"]
    inferred = [f for f in findings if f.metadata.get("kind") == "inferred"]
    print(f"PIREPs in: {len(pireps)}  advisories: {len(advisories)}")
    print(f"hazard areas out: {len(findings)}  (confirmed: {len(confirmed)}, inferred: {len(inferred)})")
    for f in findings[:8]:
        print(f"\n[sev {f.severity}] {f.summary}")
        print(f"    polygon pts: {len(f.metadata['polygon'])}  map_actions: {[a['action'] for a in f.map_actions]}")
