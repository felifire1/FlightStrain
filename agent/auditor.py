"""Decision-moment auditor.

Given recorded flights + active turbulence advisories, find flights that flew
through advisory polygons (in 3D + time) and rank them by exposure. This is
the "X minutes of preventable chop" headline number for the demo.

Inputs come from disk so the demo runs offline:
- Flights: data/overnight/traffic/*.jsonl  (recorder output)
- Advisories: data/overnight/weather/gairmet_*.json  (any snapshot — turb is slow-moving)

Each "decision moment" is (flight, advisory, entry_t, exit_t, max_severity).
"""
from __future__ import annotations
import glob
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TRAFFIC_DIR = ROOT / "data" / "overnight" / "traffic"
WEATHER_DIR = ROOT / "data" / "overnight" / "weather"

# OpenSky state vector indices
F_ICAO, F_CALL, F_TPOS, F_LON, F_LAT, F_BARO = 0, 1, 3, 5, 6, 7

FT_TO_M = 0.3048
SEV_WEIGHT = {"LGT": 1, "LGT-MOD": 2, "MOD": 3, "MOD-SEV": 4, "SEV": 5}


def _alt_to_m(s) -> float:
    """G-AIRMET base/top: 'SFC', '090' (=9000ft), '180' (=FL180)."""
    if s is None: return 0.0
    s = str(s).strip().upper()
    if s in ("SFC", "GND", ""): return 0.0
    try: return int(s) * 100 * FT_TO_M
    except ValueError: return 0.0


def _parse_advisories(snapshot_path: Path) -> list[dict]:
    """Return list of {hazard, severity, valid_from, valid_to, base_m, top_m, polygon (shapely)}.
    Skips non-TURB hazards and malformed polygons."""
    from shapely.geometry import Polygon
    import datetime as dt

    raw = json.loads(snapshot_path.read_text())
    out = []
    for a in raw:
        if not (a.get("hazard") or "").startswith("TURB"):
            continue
        coords = a.get("coords") or []
        pts = []
        for c in coords:
            if isinstance(c, dict) and "lat" in c and "lon" in c:
                try: pts.append((float(c["lon"]), float(c["lat"])))
                except (TypeError, ValueError): pass
        if len(pts) < 3:
            continue
        try:
            poly = Polygon(pts)
            if not poly.is_valid:
                poly = poly.buffer(0)
        except Exception:
            continue

        valid_iso = a.get("validTime")
        try:
            valid_from = dt.datetime.fromisoformat(valid_iso.replace("Z", "+00:00"))
        except Exception:
            continue
        # G-AIRMET issues every 3hrs; for the audit we widen to ±3hrs so flights
        # captured before the official validity but in the same weather pattern
        # still count. Turbulence fields are slow-moving — a polygon at 06Z was
        # almost certainly there at 03Z.
        valid_from -= dt.timedelta(hours=3)
        valid_to = valid_from + dt.timedelta(hours=6)

        out.append({
            "hazard": a.get("hazard"),
            "severity": (a.get("severity") or "MOD").upper(),
            "valid_from": valid_from.timestamp(),
            "valid_to": valid_to.timestamp(),
            "base_m": _alt_to_m(a.get("base")),
            "top_m": _alt_to_m(a.get("top")) or 99999.0,
            "polygon": poly,
            "raw": a,
        })
    return out


def _load_flights(t_min: float, t_max: float, bbox: tuple[float, float, float, float] | None):
    """Return {icao24: [(t, lon, lat, alt_m, callsign), ...]} from recorder JSONLs.
    Filters by api_time window and (optionally) bbox."""
    flights: dict[str, list] = defaultdict(list)
    for path in sorted(glob.glob(str(TRAFFIC_DIR / "*.jsonl"))):
        with open(path) as f:
            for line in f:
                if not line.strip(): continue
                rec = json.loads(line)
                t_api = rec.get("api_time")
                if t_api is None or not (t_min <= t_api <= t_max):
                    continue
                for st in rec.get("states", []):
                    lon = st[F_LON]; lat = st[F_LAT]; alt = st[F_BARO]
                    if lon is None or lat is None or alt is None:
                        continue
                    if bbox is not None:
                        lamin, lamax, lomin, lomax = bbox
                        if not (lamin <= lat <= lamax and lomin <= lon <= lomax):
                            continue
                    icao = st[F_ICAO]
                    callsign = (st[F_CALL] or "").strip() or icao
                    flights[icao].append((float(t_api), float(lon), float(lat), float(alt), callsign))
    return flights


_AUDIT_CACHE: dict[tuple, tuple[float, dict]] = {}
_AUDIT_TTL_S = 30.0


def find_decision_moments(
    t_min: float,
    t_max: float,
    bbox: tuple[float, float, float, float] | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    """Audit recorded flights for turbulence exposure. Returns ranked decision moments.
    Result cached for 30s on (window, bbox) — re-asking the same window is instant."""
    import time as _t
    # Round window to nearest minute so close-but-not-equal requests share cache
    key = (int(t_min // 60), int(t_max // 60), bbox, top_n)
    now = _t.time()
    hit = _AUDIT_CACHE.get(key)
    if hit and now - hit[0] < _AUDIT_TTL_S:
        return hit[1]
    result = _compute_decision_moments(t_min, t_max, bbox, top_n)
    _AUDIT_CACHE[key] = (now, result)
    return result


def _compute_decision_moments(
    t_min: float,
    t_max: float,
    bbox: tuple[float, float, float, float] | None,
    top_n: int,
) -> dict[str, Any]:
    """Uncached audit body."""
    from shapely.geometry import Point

    # Pick the most recent gairmet snapshot in window (turb fields are slow-moving)
    snapshots = sorted(glob.glob(str(WEATHER_DIR / "gairmet_*.json")))
    if not snapshots:
        return {"error": "no gairmet snapshots on disk"}
    advisories = _parse_advisories(Path(snapshots[-1]))
    if not advisories:
        return {"error": "no turbulence advisories in latest snapshot"}

    flights = _load_flights(t_min, t_max, bbox)
    if not flights:
        return {"error": f"no flights captured in window {t_min}–{t_max}"}

    moments = []
    for icao, samples in flights.items():
        samples.sort()
        callsign = samples[0][4]
        # Pairwise: for each sample, check membership in each advisory and accrue dwell time
        prev_t = None
        in_advisory: dict[int, float] = {}  # adv_idx -> entry_t
        adv_dwell: dict[int, float] = defaultdict(float)

        for t, lon, lat, alt_m, _ in samples:
            pt = Point(lon, lat)
            for i, adv in enumerate(advisories):
                if not (adv["valid_from"] <= t <= adv["valid_to"]):
                    continue
                if not (adv["base_m"] <= alt_m <= adv["top_m"]):
                    continue
                if not adv["polygon"].contains(pt):
                    if i in in_advisory:
                        adv_dwell[i] += t - in_advisory.pop(i)
                    continue
                if i not in in_advisory:
                    in_advisory[i] = t

        # close any open intervals at last sample time
        if samples:
            t_end = samples[-1][0]
            for i, t_in in in_advisory.items():
                adv_dwell[i] += t_end - t_in

        # Offline-enrich icao24 → operator/type so the agent has demo-grade flavor.
        from agent.aircraft_db import lookup as _db_lookup
        meta = _db_lookup(icao)

        for adv_idx, dwell in adv_dwell.items():
            if dwell < 30:  # require at least 30s exposure to count
                continue
            adv = advisories[adv_idx]
            sev_w = SEV_WEIGHT.get(adv["severity"], 3)
            moments.append({
                "callsign": callsign,
                "icao24": icao,
                "registration": meta.get("registration"),
                "operator": meta.get("operator"),
                "model": meta.get("model"),
                "advisory_hazard": adv["hazard"],
                "advisory_severity": adv["severity"],
                "advisory_band_ft": f"{int(adv['base_m']/FT_TO_M)}-{int(adv['top_m']/FT_TO_M) if adv['top_m']<99999 else '∞'}",
                "dwell_seconds": int(dwell),
                "dwell_minutes": round(dwell / 60, 1),
                "exposure_score": int(dwell * sev_w),
            })

    moments.sort(key=lambda m: m["exposure_score"], reverse=True)
    total_minutes = round(sum(m["dwell_seconds"] for m in moments) / 60, 1)
    return {
        "window": {"t_min": t_min, "t_max": t_max},
        "advisories_considered": [
            {"hazard": a["hazard"], "severity": a["severity"]} for a in advisories
        ],
        "n_flights_audited": len(flights),
        "n_decision_moments": len(moments),
        "total_chop_minutes": total_minutes,
        "top": moments[:top_n],
    }


if __name__ == "__main__":
    import sys, time
    # default: last 4 hours, NE corridor
    now = time.time()
    bbox = (40.0, 44.0, -74.0, -69.0)
    out = find_decision_moments(now - 4*3600, now, bbox=bbox, top_n=10)
    print(json.dumps(out, indent=2))
