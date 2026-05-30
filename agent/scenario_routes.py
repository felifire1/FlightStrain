"""Flight-side ingest for the ASI hackathon scenario data.

The hackathon bundle is a set of point-in-time *snapshots* of US flights and
their planned routes (origin -> waypoints -> destination), one directory per
"asked_at" moment. This module turns one snapshot into something the rest of
the system already understands: a list of state vectors, shaped exactly like
the input `Coordinator.correlate()` consumes, sampled at any instant `t`.

Modelling follows the bundle's documentation
(`documentation/routes/FILE_FORMAT.md`): each aircraft flies its filed
great-circle route at a constant `cruise_altitude_ft` / `cruise_speed_kt` with
no climb, descent, or speed change. `take_off_time` places it at the origin
waypoint; `scheduled_landing_time` at the destination.

Local data path
---------------
The bundle is ~226 MB and is **gitignored** (see .gitignore) — it is never
committed. It is expected to live at:

    /Users/felipequiroz/Downloads/hackathon_data_bundle

Override with the `HACKATHON_DATA_BUNDLE` environment variable. Layout:

    <bundle>/asked_at_<YYYY-MM-DD>T<HH:MM:SS>Z/routes.json[.gz]

Public API
----------
    load_scenario(asked_at=None)      -> Scenario
    position_at(flight, t)            -> (lat, lon, alt_ft) | None
    snapshot(scenario, t)             -> list[state-dict]   # correlate() input

`snapshot()` is the integration contract — its dicts carry exactly
{icao24, id, callsign, lat, lon, baro_alt (m), velocity (m/s), heading,
on_ground=False}, the same shape `Coordinator.correlate()` reads.

Acceptance:
    python -m agent.scenario_routes
"""
from __future__ import annotations

import gzip
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- constants --------------------------------------------------------------

DEFAULT_BUNDLE = "/Users/felipequiroz/Downloads/hackathon_data_bundle"
EARTH_R_M = 6_371_000.0
KT_TO_MS = 0.514444          # knots -> meters/second
FT_TO_M = 0.3048             # feet -> meters


def bundle_dir() -> Path:
    return Path(os.environ.get("HACKATHON_DATA_BUNDLE", DEFAULT_BUNDLE)).expanduser()


# --- time helpers -----------------------------------------------------------

def _parse_iso(s: str) -> float:
    """ISO 8601 (with Z or +00:00) -> epoch seconds (UTC)."""
    s = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _to_epoch(t: Any) -> float:
    """Accept epoch seconds, a datetime, or an ISO string -> epoch seconds."""
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, datetime):
        dt = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(t, str):
        return _parse_iso(t)
    raise TypeError(f"unsupported time type: {type(t)!r}")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


# --- geometry ---------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in meters."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees [0, 360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def slerp(lat1: float, lon1: float, lat2: float, lon2: float, f: float) -> tuple[float, float]:
    """Spherical interpolation between two lat/lon points; f in [0, 1].
    f=0 -> point 1, f=1 -> point 2. Stays on the great circle between them."""
    if f <= 0:
        return lat1, lon1
    if f >= 1:
        return lat2, lon2
    p1, l1 = math.radians(lat1), math.radians(lon1)
    p2, l2 = math.radians(lat2), math.radians(lon2)
    # angular distance
    d = 2 * math.asin(math.sqrt(
        math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2
    ))
    if d == 0:
        return lat1, lon1
    a = math.sin((1 - f) * d) / math.sin(d)
    b = math.sin(f * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    lat = math.atan2(z, math.sqrt(x * x + y * y))
    lon = math.atan2(y, x)
    return math.degrees(lat), math.degrees(lon)


# --- data model -------------------------------------------------------------

@dataclass
class Flight:
    flight_number: str
    take_off_time: float            # epoch seconds
    scheduled_landing_time: float   # epoch seconds
    origin_airport_icao: str
    destination_airport_icao: str
    cruise_altitude_ft: float
    cruise_speed_kt: float
    lats: list[float]
    lons: list[float]
    is_airborne: bool
    uid: str                        # stable lowercase id; == CZML entity id
    # cached cumulative great-circle distances (meters) along the waypoints
    _seglens: list[float] = field(default_factory=list, repr=False)
    _total_m: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        self._seglens = [
            haversine_m(self.lats[i], self.lons[i], self.lats[i + 1], self.lons[i + 1])
            for i in range(len(self.lats) - 1)
        ]
        self._total_m = sum(self._seglens)


@dataclass
class Scenario:
    asked_at: float                 # epoch seconds
    window_start: float
    window_end: float
    flights: list[Flight]
    asked_at_iso: str
    source: Path

    def airborne_at(self, t: Any) -> list[Flight]:
        te = _to_epoch(t)
        return [f for f in self.flights if f.take_off_time <= te <= f.scheduled_landing_time]


def flight_uid(flight_number: str, take_off_time: float, origin: str) -> str:
    """Stable, lowercase, unique-per-flight id. Lowercase because the frontend's
    highlight_flight lowercases icao24 before matching CZML entity ids — the CZML
    emitter must use this exact same id so highlights land."""
    return f"{flight_number}-{origin}-{int(take_off_time)}".lower()


# --- loading ----------------------------------------------------------------

def _find_snapshot_dir(bundle: Path, asked_at: Any | None) -> Path:
    dirs = sorted(p for p in bundle.glob("asked_at_*") if p.is_dir())
    if not dirs:
        raise FileNotFoundError(f"no asked_at_* snapshots under {bundle}")
    if asked_at is None:
        return dirs[0]
    # match by raw token substring first (e.g. "2025-07-01" or full token)
    token = asked_at if isinstance(asked_at, str) else iso(_to_epoch(asked_at))
    for d in dirs:
        stamp = d.name[len("asked_at_"):]
        if token in d.name or token in stamp:
            return d
    # fall back to nearest by epoch
    target = _to_epoch(asked_at)
    return min(dirs, key=lambda d: abs(_parse_iso(d.name[len("asked_at_"):]) - target))


def _read_routes(snapshot_dir: Path) -> dict:
    gz = snapshot_dir / "routes.json.gz"
    plain = snapshot_dir / "routes.json"
    if gz.exists():
        with gzip.open(gz, "rt") as fh:
            return json.load(fh)
    if plain.exists():
        return json.loads(plain.read_text())
    raise FileNotFoundError(f"no routes.json[.gz] in {snapshot_dir}")


def load_scenario(asked_at: Any | None = None, bundle: Path | None = None) -> Scenario:
    """Load one snapshot. `asked_at` selects the directory:
        - None              -> earliest snapshot in the bundle
        - "2025-07-01"      -> first directory whose name contains the token
        - epoch / datetime  -> nearest snapshot by time
    """
    bundle = bundle or bundle_dir()
    snapshot_dir = _find_snapshot_dir(bundle, asked_at)
    raw = _read_routes(snapshot_dir)

    flights: list[Flight] = []
    for r in raw.get("flights", []):
        lats = r.get("lats") or []
        lons = r.get("lons") or []
        if len(lats) < 1 or len(lats) != len(lons):
            continue  # unusable geometry
        to = _parse_iso(r["take_off_time"])
        flights.append(Flight(
            flight_number=r["flight_number"],
            take_off_time=to,
            scheduled_landing_time=_parse_iso(r["scheduled_landing_time"]),
            origin_airport_icao=r["origin_airport_icao"],
            destination_airport_icao=r["destination_airport_icao"],
            cruise_altitude_ft=float(r.get("cruise_altitude_ft") or 0.0),
            cruise_speed_kt=float(r.get("cruise_speed_kt") or 0.0),
            lats=[float(x) for x in lats],
            lons=[float(x) for x in lons],
            is_airborne=bool(r.get("is_airborne")),
            uid=flight_uid(r["flight_number"], to, r["origin_airport_icao"]),
        ))

    return Scenario(
        asked_at=_parse_iso(raw["asked_at"]),
        window_start=_parse_iso(raw["window_start"]),
        window_end=_parse_iso(raw["window_end"]),
        flights=flights,
        asked_at_iso=raw["asked_at"],
        source=snapshot_dir,
    )


# --- position / heading -----------------------------------------------------

def _distance_along(flight: Flight, te: float) -> float | None:
    """Meters traveled along the route at time `te`, or None if not airborne.
    Clamped to the total route length (aircraft holds at the destination
    waypoint between early arrival and scheduled landing)."""
    if te < flight.take_off_time or te > flight.scheduled_landing_time:
        return None
    speed_ms = flight.cruise_speed_kt * KT_TO_MS
    d = speed_ms * (te - flight.take_off_time)
    return min(d, flight._total_m)


def _segment_at(flight: Flight, d: float) -> tuple[int, float]:
    """Return (segment_index, fraction_within_segment) for distance `d` along
    the route. Index is into the waypoint pairs (i, i+1)."""
    acc = 0.0
    for i, seg in enumerate(flight._seglens):
        if seg <= 0:
            continue
        if acc + seg >= d:
            return i, (d - acc) / seg
        acc += seg
    # past the end -> sit on the last segment's end waypoint
    last = max(0, len(flight._seglens) - 1)
    return last, 1.0


def position_at(flight: Flight, t: Any) -> tuple[float, float, float] | None:
    """(lat, lon, alt_ft) of `flight` at time `t`, or None if not airborne.

    Great-circle interpolation along the filed waypoints at constant
    `cruise_speed_kt`, bounded by take_off_time -> scheduled_landing_time, at a
    constant `cruise_altitude_ft`.
    """
    te = _to_epoch(t)
    d = _distance_along(flight, te)
    if d is None:
        return None
    if len(flight.lats) == 1 or flight._total_m == 0.0:
        return flight.lats[0], flight.lons[0], flight.cruise_altitude_ft
    i, f = _segment_at(flight, d)
    lat, lon = slerp(flight.lats[i], flight.lons[i],
                     flight.lats[i + 1], flight.lons[i + 1], f)
    return lat, lon, flight.cruise_altitude_ft


def heading_at(flight: Flight, t: Any) -> float:
    """Heading (degrees) at time `t`: bearing from the current position toward
    the next route waypoint, so forward projection in correlate() points the
    right way. Falls back to the final segment's bearing once at the end."""
    te = _to_epoch(t)
    d = _distance_along(flight, te)
    if d is None or len(flight.lats) < 2 or flight._total_m == 0.0:
        return 0.0
    i, f = _segment_at(flight, d)
    cur = slerp(flight.lats[i], flight.lons[i],
                flight.lats[i + 1], flight.lons[i + 1], f)
    # bearing toward the end of the current segment (the next waypoint)
    return bearing_deg(cur[0], cur[1], flight.lats[i + 1], flight.lons[i + 1])


# --- the integration contract -----------------------------------------------

def state_dict(flight: Flight, t: Any) -> dict[str, Any] | None:
    """One state vector for `flight` at `t`, or None if not airborne. Shape
    matches Coordinator.correlate()'s input exactly."""
    pos = position_at(flight, t)
    if pos is None:
        return None
    lat, lon, alt_ft = pos
    return {
        "icao24": flight.uid,
        "id": flight.uid,
        "callsign": flight.flight_number,
        "lat": lat,
        "lon": lon,
        "baro_alt": alt_ft * FT_TO_M,            # meters, like OpenSky baro_alt
        "velocity": flight.cruise_speed_kt * KT_TO_MS,  # m/s
        "heading": heading_at(flight, t),
        "on_ground": False,
    }


def snapshot(scenario: Scenario, t: Any) -> list[dict[str, Any]]:
    """All airborne flights at time `t` as correlate()-shaped state dicts."""
    out: list[dict[str, Any]] = []
    for fl in scenario.flights:
        sd = state_dict(fl, t)
        if sd is not None:
            out.append(sd)
    return out


# --- acceptance / smoke test ------------------------------------------------

def _demo() -> None:
    scn = load_scenario()
    print(f"loaded snapshot: {scn.source.name}")
    print(f"asked_at  : {scn.asked_at_iso}")
    print(f"window    : {iso(scn.window_start)} -> {iso(scn.window_end)}")
    print(f"flights   : {len(scn.flights)}  (filed-airborne at snapshot: "
          f"{sum(1 for f in scn.flights if f.is_airborne)})")

    # Pick a flight with a real multi-waypoint route for a legible sample.
    sample = max(scn.flights, key=lambda f: (len(f.lats), f._total_m))
    dur = sample.scheduled_landing_time - sample.take_off_time
    print(f"\nsample flight: {sample.flight_number}  "
          f"{sample.origin_airport_icao}->{sample.destination_airport_icao}  "
          f"{len(sample.lats)} wp  FL{int(sample.cruise_altitude_ft/100):03d}  "
          f"{int(sample.cruise_speed_kt)}kt  uid={sample.uid}")
    print("position_at at 3 timestamps (10% / 50% / 90% of flight):")
    for frac in (0.10, 0.50, 0.90):
        te = sample.take_off_time + dur * frac
        pos = position_at(sample, te)
        hdg = heading_at(sample, te)
        if pos:
            print(f"  t={iso(te)}  lat={pos[0]:8.4f}  lon={pos[1]:9.4f}  "
                  f"alt={int(pos[2])}ft  hdg={hdg:5.1f}")

    # snapshot() at the asked_at moment — the correlate() contract.
    states = snapshot(scn, scn.asked_at)
    print(f"\nsnapshot(scenario, asked_at): {len(states)} airborne states")
    if states:
        s = states[0]
        print("first state-dict (correlate() input shape):")
        print("  " + json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                                  for k, v in s.items()}))


if __name__ == "__main__":
    _demo()
