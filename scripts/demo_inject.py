"""Demo injector — the "button that always works."

Drops a synthetic MODERATE turbulence PIREP at an interior point of a *real*
G-AIRMET turbulence polygon, then runs the live pipeline over it:

    PIREP confirms forecast  ->  project current traffic  ->  "N flights transit"

This guarantees the demo's wow moment fires on command, regardless of live
weather or whether any real pilot happened to report chop. The polygon is real
(pulled from aviationweather.gov, or the cached snapshot if offline); only the
single corroborating pilot report is synthetic.

What it does, end to end:
  1. Load G-AIRMET advisories (live NOAA, else data/samples/gairmet_all.json).
  2. Load current traffic (recorder snapshot, else live OpenSky for the polygon
     bbox, else the cached data/samples/traffic.czml positions).
  3. Pick the real TURB advisory that the most traffic is flying through.
  4. Build a synthetic MOD PIREP at an interior point, in the advisory's band.
  5. turbulence_area.pirep_to_hazard() -> a "PIREP CONFIRMS" Finding.
  6. Coordinator.correlate() -> the "N flights projected to transit" Finding.
  7. Persist the PIREP into data/samples + data/overnight so the chat agent's
     get_pireps fallbacks and the specialist watchers see it too, publish the
     findings to the bus, and print the headline.

Run:
    .venv/bin/python scripts/demo_inject.py
    .venv/bin/python scripts/demo_inject.py --intensity SEV --live
    .venv/bin/python scripts/demo_inject.py --dry-run        # compute, don't write
"""
from __future__ import annotations
import argparse
import glob
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import turbulence_area as ta  # noqa: E402
from agent.specialists.coordinator import Coordinator  # noqa: E402

SAMPLES_DIR = ROOT / "data" / "samples"
OVERNIGHT_TRAFFIC = ROOT / "data" / "overnight" / "traffic"
OVERNIGHT_WEATHER = ROOT / "data" / "overnight" / "weather"

AW = "https://aviationweather.gov/api/data"
# Recorder corridor — where both the live recorder and the cached sample
# traffic have aircraft. We bias polygon selection toward here so transits hit.
TRAFFIC_BBOX = (40.0, 44.0, -74.0, -69.0)  # (lamin, lamax, lomin, lomax)

# OpenSky state-vector field order (states/all).
STATE_FIELDS = [
    "icao24", "callsign", "origin_country", "time_position", "last_contact",
    "lon", "lat", "baro_alt", "on_ground", "velocity", "heading", "vert_rate",
    "sensors", "geo_alt", "squawk", "spi", "position_source",
]

DEMO_ICAO = "DEMO"  # marks our synthetic PIREP so repeated runs stay idempotent


# --- advisories --------------------------------------------------------------

def load_advisories(live: bool) -> tuple[list[dict], str]:
    """Return (advisories, source). Live NOAA G-AIRMET first; cached snapshot
    on any failure so the button still works on dead Fenway wifi."""
    if live:
        try:
            r = httpx.get(f"{AW}/gairmet", params={"format": "json"}, timeout=10)
            if r.status_code == 200 and isinstance(r.json(), list) and r.json():
                return r.json(), "live:aviationweather.gov"
        except Exception:
            pass
    # cached fallbacks
    for name in ("gairmet_all.json",):
        p = SAMPLES_DIR / name
        if p.exists():
            return json.loads(p.read_text()), f"cached:{p.name}"
    snaps = sorted(glob.glob(str(OVERNIGHT_WEATHER / "gairmet_*.json")))
    if snaps:
        return json.loads(Path(snaps[-1]).read_text()), f"cached:{Path(snaps[-1]).name}"
    return [], "none"


def turb_advisories(advisories: list[dict]) -> list[dict]:
    out = []
    for a in advisories:
        if "TURB" not in (a.get("hazard") or "").upper():
            continue
        if len(ta.advisory_polygon(a)) >= 3:
            out.append(a)
    return out


# --- traffic -----------------------------------------------------------------

def _states_from_rows(rows: list) -> list[dict]:
    out = []
    for s in rows:
        padded = list(s) + [None] * (len(STATE_FIELDS) - len(s))
        out.append(dict(zip(STATE_FIELDS, padded)))
    return out


def _traffic_from_recorder() -> list[dict]:
    files = sorted(glob.glob(str(OVERNIGHT_TRAFFIC / "*.jsonl")))
    if not files:
        return []
    last = None
    with open(files[-1]) as fh:
        for line in fh:
            if line.strip():
                last = json.loads(line)
    if not last:
        return []
    return _states_from_rows(last.get("states") or [])


def _traffic_from_opensky(bbox: tuple[float, float, float, float]) -> list[dict]:
    lamin, lamax, lomin, lomax = bbox
    try:
        from agent.opensky_auth import authed_get
        r = authed_get(
            "https://opensky-network.org/api/states/all",
            params={"lamin": lamin, "lamax": lamax, "lomin": lomin, "lomax": lomax},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        return _states_from_rows((r.json() or {}).get("states") or [])
    except Exception:
        return []


def _traffic_from_sample_czml() -> list[dict]:
    """Recover state vectors from the cached traffic.czml. Each entity's
    `position.cartographicDegrees` is [t, lon, lat, alt, t, lon, lat, alt, ...];
    we take the first sample as the position and derive heading/velocity from
    the first two so the forward projection in correlate() has something to chew
    on. Real recorded positions — not fabricated."""
    import math
    p = SAMPLES_DIR / "traffic.czml"
    if not p.exists():
        return []
    try:
        doc = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for ent in doc:
        pos = ent.get("position") or {}
        cd = pos.get("cartographicDegrees")
        if not cd or len(cd) < 4:
            continue
        _, lon0, lat0, alt0 = cd[0], cd[1], cd[2], cd[3]
        velocity = None
        heading = None
        if len(cd) >= 8:
            dt = (cd[4] - cd[0]) or 1.0
            lon1, lat1 = cd[5], cd[6]
            dlat = lat1 - lat0
            dlon = lon1 - lon0
            mlat = dlat * 111_320.0
            mlon = dlon * 111_320.0 * math.cos(math.radians(lat0))
            dist = math.hypot(mlat, mlon)
            velocity = dist / dt
            heading = (math.degrees(math.atan2(mlon, mlat)) + 360.0) % 360.0
        out.append({
            "icao24": ent.get("id"),
            "callsign": (ent.get("name") or "").split()[0] if ent.get("name") else None,
            "lat": lat0, "lon": lon0, "baro_alt": alt0,
            "velocity": velocity, "heading": heading, "on_ground": False,
        })
    return out


def load_traffic(bbox: tuple[float, float, float, float], live: bool) -> tuple[list[dict], str]:
    states = _traffic_from_recorder()
    if states:
        return states, "recorder"
    if live:
        states = _traffic_from_opensky(bbox)
        if states:
            return states, "opensky:live"
    states = _traffic_from_sample_czml()
    if states:
        return states, "cached:traffic.czml"
    return [], "none"


# --- geometry ----------------------------------------------------------------

M_TO_FT = 3.28084


def _state_fl(s: dict) -> int | None:
    """Flight level (hundreds of feet) from a state vector's baro_alt (meters)."""
    alt_m = s.get("baro_alt")
    if alt_m is None:
        return None
    try:
        return int(round(float(alt_m) * M_TO_FT / 100.0))
    except (TypeError, ValueError):
        return None


def filter_by_band(states: list[dict], base_fl: int, top_fl: int, buffer_fl: int = 20) -> list[dict]:
    """Keep only aircraft within the advisory's altitude band (+/- buffer). The
    Coordinator's correlate() tests geography only; doing the vertical filter
    here keeps the 'who transits' set physically honest — a jet at FL350 is not
    transiting a surface-to-FL120 turbulence layer."""
    out = []
    for s in states:
        fl = _state_fl(s)
        if fl is None or (base_fl - buffer_fl) <= fl <= (top_fl + buffer_fl):
            out.append(s)
    return out


def _count_in_poly(poly_latlon: list[tuple[float, float]], states: list[dict]) -> int:
    n = 0
    for s in states:
        lat, lon = s.get("lat"), s.get("lon")
        if lat is None or lon is None:
            continue
        if ta._point_in_polygon(lat, lon, poly_latlon):
            n += 1
    return n


def pick_advisory(turbs: list[dict], states: list[dict]) -> dict | None:
    """Choose the TURB advisory the most band-appropriate traffic is inside;
    tie-break on overlap with the recorder corridor, then on polygon size."""
    if not turbs:
        return None
    lamin, lamax, lomin, lomax = TRAFFIC_BBOX

    def score(a: dict) -> tuple:
        poly = ta.advisory_polygon(a)
        base_fl, top_fl = ta._advisory_band(a)
        inside = _count_in_poly(poly, filter_by_band(states, base_fl, top_fl))
        lats = [p[0] for p in poly]; lons = [p[1] for p in poly]
        overlaps_corridor = not (max(lats) < lamin or min(lats) > lamax or
                                 max(lons) < lomin or min(lons) > lomax)
        area = (max(lats) - min(lats)) * (max(lons) - min(lons))
        return (inside, overlaps_corridor, area)

    return max(turbs, key=score)


def interior_point(poly_latlon: list[tuple[float, float]]) -> tuple[float, float]:
    """A point guaranteed inside the polygon. Centroid first; if the polygon is
    concave and the centroid lands outside, scan a grid for the first hit."""
    lats = [p[0] for p in poly_latlon]; lons = [p[1] for p in poly_latlon]
    clat = sum(lats) / len(lats); clon = sum(lons) / len(lons)
    if ta._point_in_polygon(clat, clon, poly_latlon):
        return clat, clon
    lo_la, hi_la, lo_lo, hi_lo = min(lats), max(lats), min(lons), max(lons)
    for i in range(1, 20):
        for j in range(1, 20):
            la = lo_la + (hi_la - lo_la) * i / 20.0
            lo = lo_lo + (hi_lo - lo_lo) * j / 20.0
            if ta._point_in_polygon(la, lo, poly_latlon):
                return la, lo
    return clat, clon  # degenerate; centroid is the best we have


# --- synthetic PIREP ---------------------------------------------------------

def build_pirep(lat: float, lon: float, fl: int, intensity: str, actype: str = "B738") -> dict:
    """A raw aviationweather-shaped PIREP (the shape turbulence_area + tools
    already parse). fltLvl is in hundreds of feet."""
    now = int(time.time())
    raw = f"DEMO UA /OV {lat:.2f}/{lon:.2f} /TM SYNTH /FL{fl:03d} /TP {actype} /TB {intensity}"
    return {
        "receiptTime": now,
        "obsTime": now,
        "icaoId": DEMO_ICAO,
        "acType": actype,
        "lat": round(lat, 4),
        "lon": round(lon, 4),
        "fltLvl": fl,
        "fltLvlType": "FLT",
        "tbInt1": intensity,
        "tbType1": "CAT",
        "tbBas1": max(0, fl - 20) * 100,
        "tbTop1": (fl + 20) * 100,
        "tbFreq1": "OCNL",
        "rawOb": raw,
        "_synthetic": True,
    }


def persist_pirep(pirep: dict) -> list[str]:
    """Make the synthetic PIREP visible to the other readers: prepend it to the
    cached PIREP sample (the get_pireps / specialist offline fallback) and drop a
    dedicated overnight snapshot. Idempotent — strips any prior DEMO entries."""
    written = []

    def _strip_demo(items: list) -> list:
        return [p for p in items if (p.get("icaoId") != DEMO_ICAO and not p.get("_synthetic"))]

    big = SAMPLES_DIR / "pireps_us.json"
    if big.exists():
        try:
            items = json.loads(big.read_text())
            if isinstance(items, list):
                big.write_text(json.dumps([pirep] + _strip_demo(items)))
                written.append(str(big.relative_to(ROOT)))
        except (json.JSONDecodeError, OSError):
            pass

    recent = SAMPLES_DIR / "pirep_recent.json"
    try:
        recent.write_text(json.dumps([pirep]))
        written.append(str(recent.relative_to(ROOT)))
    except OSError:
        pass

    OVERNIGHT_WEATHER.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M", time.gmtime())
    snap = OVERNIGHT_WEATHER / f"pirep_demo_{stamp}.json"
    try:
        snap.write_text(json.dumps([pirep]))
        written.append(str(snap.relative_to(ROOT)))
    except OSError:
        pass

    return written


# --- main --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Inject a synthetic MOD PIREP inside a real G-AIRMET turbulence polygon.")
    ap.add_argument("--intensity", default="MOD", choices=["MOD", "MOD-SEV", "SEV"],
                    help="reported turbulence intensity (default MOD)")
    ap.add_argument("--live", action="store_true",
                    help="prefer live NOAA + OpenSky fetches (default: cached-first for demo determinism)")
    ap.add_argument("--dry-run", action="store_true", help="compute and print, but do not write files or the bus")
    ap.add_argument("--json", action="store_true", help="emit the result as JSON only")
    args = ap.parse_args()

    advisories, adv_src = load_advisories(live=args.live)
    turbs = turb_advisories(advisories)
    if not turbs:
        print(f"[demo_inject] no TURB advisory available (source={adv_src}). Cannot inject.", file=sys.stderr)
        return 2

    states, traffic_src = load_traffic(TRAFFIC_BBOX, live=args.live)
    adv = pick_advisory(turbs, states)
    poly = ta.advisory_polygon(adv)
    base_fl, top_fl = ta._advisory_band(adv)
    fl = max(10, base_fl + (top_fl - base_fl) // 2)
    lat, lon = interior_point(poly)

    pirep = build_pirep(lat, lon, fl, args.intensity)

    # Disable the time gate: the cached advisories predate "now", and the whole
    # point of the button is that it fires regardless of clock alignment.
    hazard = ta.pirep_to_hazard(pirep, [adv], min_intensity="MOD", time_slack_h=10_000)
    if hazard is None or hazard.metadata.get("kind") != "confirmed":
        print(f"[demo_inject] PIREP did not confirm against the polygon (kind="
              f"{hazard.metadata.get('kind') if hazard else None}). Aborting.", file=sys.stderr)
        return 3

    # Only aircraft in the advisory's altitude band can actually transit it.
    band_states = filter_by_band(states, base_fl, top_fl)
    coord = Coordinator()
    correlations = coord.correlate(band_states, [hazard]) if band_states else []
    transit = correlations[0] if correlations else None
    n_transit = transit.metadata.get("affected_count", 0) if transit else 0

    written = [] if args.dry_run else persist_pirep(pirep)
    if not args.dry_run:
        try:
            from agent.specialists.bus import bus
            bus.publish(hazard)
            if transit:
                bus.publish(transit)
        except Exception as e:
            print(f"[demo_inject] bus publish skipped: {type(e).__name__}: {e}", file=sys.stderr)

    map_actions = list(hazard.map_actions) + (list(transit.map_actions) if transit else [])
    result = {
        "advisory_source": adv_src,
        "traffic_source": traffic_src,
        "advisory": {"hazard": adv.get("hazard"), "severity": adv.get("severity"),
                     "band_fl": [base_fl, top_fl]},
        "pirep": {"lat": lat, "lon": lon, "fl": fl, "intensity": args.intensity},
        "confirmed_summary": hazard.summary,
        "transit_summary": transit.summary if transit else None,
        "n_flights_transit": n_transit,
        "traffic_states_total": len(states),
        "traffic_states_in_band": len(band_states),
        "map_actions": map_actions,
        "files_written": written,
    }

    if not args.dry_run:
        try:
            artifact = SAMPLES_DIR / "demo_inject_result.json"
            artifact.write_text(json.dumps(result, indent=2))
            result["files_written"].append(str(artifact.relative_to(ROOT)))
        except OSError:
            pass

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print("=" * 72)
    print("DEMO INJECT — synthetic PIREP inside a real G-AIRMET turbulence area")
    print("=" * 72)
    print(f"advisory  : {adv.get('hazard')} {adv.get('severity')}  FL{base_fl:03d}-FL{top_fl:03d}  (src {adv_src})")
    print(f"PIREP     : {args.intensity} turb @ FL{fl:03d}  ({lat:.2f}N, {abs(lon):.2f}W)")
    print(f"traffic   : {len(states)} states from {traffic_src} "
          f"({len(band_states)} in band FL{base_fl:03d}-FL{top_fl:03d})")
    print()
    print("CONFIRMS  :", hazard.summary)
    if transit:
        print("TRANSIT   :", transit.summary)
    else:
        print(f"TRANSIT   : 0 flights projected to transit (traffic source: {traffic_src}).")
    print()
    print(f"-> headline: PIREP confirms forecast; {n_transit} flight(s) projected to transit.")
    if written:
        print(f"-> wrote: {', '.join(written)}")
    print("-> map_actions ready for the frontend (draw_polygon + highlight_flight).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
