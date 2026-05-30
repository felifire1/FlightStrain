"""Impact engine — turn avoidance options into money / time / climate, pick the
cheapest maneuver per flight, and aggregate the fleet.

Where `scenario_wx` says *where* the convective hazards are and `scenario_routes`
says *which* flights fly through them, this module answers *what it costs to get
out of the way* — and proves the 4D thesis: knowing the storm's position **in
time** lets a flight climb over it or wait it out instead of always burning fuel
to fly around (the 2D reflex).

Per flight we score three maneuver archetypes and pick the cheapest:

  - **lateral** (fly around)  — extra distance -> extra fuel + extra time
  - **climb**   (fly over)    — short extra burn during the climb; cruising
                                higher is ~fuel-neutral, so this is cheap when
                                there's altitude headroom under the echo top
  - **wait**    (let it pass)  — ~zero extra fuel, cost is delay minutes only.
                                This is the 4D advantage made visible.

`fleet_impact()` runs the engine over every transiting flight in a scenario and
returns totals + the headline 4D-vs-2D delta (sum-of-cheapest vs sum-of-around-
only) + a per-maneuver breakdown.

CONTRACT (built against Window 1's avoidance.py, stubbed until it lands):
`AvoidanceOption` / `AvoidancePlan` below. `_load_plans()` uses Window 1's
generator if `agent.avoidance` exposes one; otherwise it derives a data-grounded
stub from scenario_routes + scenario_wx so the headline is real today.

Acceptance:
    python -m agent.impact          # prints the 2025-08-22T18:00:00Z fleet headline
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any

from agent import scenario_routes
from agent import scenario_wx
from agent.specialists import aircraft_perf

# ============================================================================
# Economics constants  (documented; tune here)
# ============================================================================
# Jet-A energy/price/emissions — standard industry reference values.
JET_A_LB_PER_GAL = 6.7        # Jet-A density at 15 C: ~6.7 lb per US gallon
CO2_KG_PER_GAL = 9.6          # combustion CO2: ~9.6 kg per gallon of Jet-A
FUEL_USD_PER_GAL = 2.5        # jet fuel price assumption ($/gal)

# The single knob that makes fuel ($) and delay (min) comparable so we can pick
# a "cheapest" maneuver across both axes. ~$30/min is a tactical (crew + lost
# slot) delay cost — lower than the ~$74/min fully-allocated A4A figure, because
# a planned reroute/wait isn't an irregular-ops cancellation. Raise it and 4D
# (which trades fuel for time) looks less attractive; lower it and waits win
# more. The 4D-vs-2D delta is reported against this weighting.
DELAY_USD_PER_MIN = 30.0

# Climb burn model: extra fuel during a climb ~= half a cruise-hour's burn,
# prorated by climb minutes. Cruising higher afterward is ~fuel-neutral.
CLIMB_BURN_FRACTION = 0.5
CLIMB_CLEARANCE_FT = 2000.0   # climb this far above the echo top to clear it

# Convective avoidance standoff. Crews don't skim a heavy cell — standard
# practice (FAA AC 00-24, airline ops) is to give it ~20 nm of berth, upwind
# more. So a lateral reroute adds roughly the cell's width PLUS a standoff on
# each side, not just the cell width. This is what makes going-around genuinely
# costly — and the 4D "wait it out / time it" play worth modeling.
CONVECTIVE_STANDOFF_NM = 20.0

# Fallback aircraft type when one isn't known (see _assign_actype).
DEFAULT_ACTYPE = "B738"

NM_PER_DEG_LAT = 60.0


# ============================================================================
# The AvoidanceOption / AvoidancePlan contract  (Window 1 produces these)
# ============================================================================

@dataclass
class AvoidanceOption:
    """One way out of a hazard. `kind` selects which fields matter:
        lateral -> extra_nm           (and the induced delay it implies)
        climb   -> climb_min, target_alt_ft
        wait    -> delay_min
    `turb_min_avoided` is carried on every option (what we buy by maneuvering).
    """
    kind: str                              # "lateral" | "climb" | "wait"
    extra_nm: float = 0.0
    climb_min: float = 0.0
    target_alt_ft: float | None = None
    delay_min: float = 0.0
    turb_min_avoided: float = 0.0
    label: str = ""


@dataclass
class AvoidancePlan:
    """A flight's menu of avoidance options + the context to cost and draw them.
    Window 1's real plan need only match the duck-typed fields the engine reads:
    `actype`, `options`, and (for the CZML) `lats`/`lons`/`cell_polygon`."""
    flight_id: str
    actype: str
    options: list[AvoidanceOption]
    callsign: str = ""
    origin: str = ""
    dest: str = ""
    cruise_alt_ft: float = 0.0
    cruise_kt: float = 0.0
    lats: list[float] = field(default_factory=list)        # original (through-storm) route
    lons: list[float] = field(default_factory=list)
    cell_polygon: list[tuple[float, float]] = field(default_factory=list)  # (lat, lon)
    hit_time: float = 0.0                                   # epoch s of the transit


# duck-typed access so Window 1's objects OR plain dicts both work
def _g(obj: Any, name: str, default: Any = 0.0) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ============================================================================
# Scoring
# ============================================================================

def _perf(actype: str) -> dict:
    return aircraft_perf.lookup(actype) or aircraft_perf.lookup(DEFAULT_ACTYPE)


def score(option: Any, actype: str) -> dict[str, float]:
    """Cost one avoidance option for a given aircraft type.

    Returns {fuel_gal, co2_kg, usd, delay_min, turb_min_avoided, pax}, where
    `usd` is the FUEL dollar cost only and `delay_min` is the time penalty — the
    two axes `cheapest()` weighs together. (For a wait, fuel ~0 and the whole
    cost shows up as delay — the 4D advantage made visible.)
    """
    perf = _perf(actype)
    cruise_kt = perf["cruise_kt"] or 1.0
    fuel_lb_hr = perf["fuel_lb_hr"]
    seats = perf["seats"]

    kind = _g(option, "kind", "lateral")
    if kind == "lateral":
        extra_nm = _g(option, "extra_nm", 0.0)
        extra_fuel_lb = (extra_nm / cruise_kt) * fuel_lb_hr      # hrs aloft * lb/hr
        delay_min = (extra_nm / cruise_kt) * 60.0               # extra time on the longer path
    elif kind == "climb":
        climb_min = _g(option, "climb_min", 0.0)
        extra_fuel_lb = (climb_min / 60.0) * fuel_lb_hr * CLIMB_BURN_FRACTION
        delay_min = 0.0                                          # higher cruise ~= neutral
    elif kind == "wait":
        extra_fuel_lb = 0.0
        delay_min = _g(option, "delay_min", 0.0)                # cost is time only
    else:
        extra_fuel_lb = 0.0
        delay_min = 0.0

    fuel_gal = extra_fuel_lb / JET_A_LB_PER_GAL
    return {
        "fuel_gal": fuel_gal,
        "co2_kg": fuel_gal * CO2_KG_PER_GAL,
        "usd": fuel_gal * FUEL_USD_PER_GAL,
        "delay_min": delay_min,
        "turb_min_avoided": _g(option, "turb_min_avoided", 0.0),
        "pax": float(seats),
    }


def weighted_cost(scored: dict[str, float]) -> float:
    """Combine the fuel ($) and delay (min) axes into one comparable number."""
    return scored["usd"] + scored["delay_min"] * DELAY_USD_PER_MIN


def cheapest(plan: Any, actype: str | None = None) -> Any | None:
    """The plan's minimum-weighted-cost option (fuel $ + delay-weighted)."""
    actype = actype or _g(plan, "actype", DEFAULT_ACTYPE)
    options = _g(plan, "options", []) or []
    best, best_cost = None, math.inf
    for opt in options:
        c = weighted_cost(score(opt, actype))
        if c < best_cost:
            best, best_cost = opt, c
    return best


def _around_only(plan: Any) -> Any | None:
    """The 2D baseline: the lateral (fly-around) option, if the plan has one."""
    for opt in _g(plan, "options", []) or []:
        if _g(opt, "kind", "") == "lateral":
            return opt
    return None


# ============================================================================
# Fleet aggregation
# ============================================================================

def fleet_impact(asked_at: str, **stub_kw: Any) -> dict[str, Any]:
    """Run the engine over every transiting flight in a scenario.

    Returns totals for the recommended (4D, cheapest-per-flight) plan, the 2D
    (always-around) baseline, the delta between them, a per-maneuver breakdown,
    and a ready-to-print `headline`.
    """
    plans = _load_plans(asked_at, **stub_kw)

    z4 = _zero_totals()
    z2 = _zero_totals()
    breakdown = {k: {"n": 0, "fuel_gal": 0.0, "delay_min": 0.0, "turb_min_avoided": 0.0}
                 for k in ("lateral", "climb", "wait")}

    for p in plans:
        actype = _g(p, "actype", DEFAULT_ACTYPE)

        chosen = cheapest(p, actype)
        if chosen is None:
            continue
        cs = score(chosen, actype)
        _accumulate(z4, cs)
        z4["cost"] += weighted_cost(cs)

        around = _around_only(p) or chosen      # if no lateral, 2D == chosen for this flight
        as_ = score(around, actype)
        _accumulate(z2, as_)
        z2["cost"] += weighted_cost(as_)

        k = _g(chosen, "kind", "lateral")
        if k in breakdown:
            breakdown[k]["n"] += 1
            breakdown[k]["fuel_gal"] += cs["fuel_gal"]
            breakdown[k]["delay_min"] += cs["delay_min"]
            breakdown[k]["turb_min_avoided"] += cs["turb_min_avoided"]

    n = len(plans)
    delta_pct = ((z2["cost"] - z4["cost"]) / z2["cost"] * 100.0) if z2["cost"] > 0 else 0.0

    headline = (
        f"{n} flights avoidable, {z4['fuel_gal']:,.0f} gal / "
        f"{z4['delay_min']:,.0f} delay-min, 4D beats 2D by {delta_pct:.0f}%."
    )

    return {
        "asked_at": asked_at,
        "n_flights": n,
        "totals_4d": z4,
        "totals_2d": z2,
        "delta_pct": delta_pct,
        "breakdown": breakdown,
        "headline": headline,
        "plans": plans,
    }


def _zero_totals() -> dict[str, float]:
    return {"fuel_gal": 0.0, "co2_kg": 0.0, "usd": 0.0,
            "delay_min": 0.0, "turb_min_avoided": 0.0, "pax": 0.0, "cost": 0.0}


def _accumulate(tot: dict[str, float], s: dict[str, float]) -> None:
    for k in ("fuel_gal", "co2_kg", "usd", "delay_min", "turb_min_avoided", "pax"):
        tot[k] += s[k]


# ============================================================================
# Plan source: Window 1's generator if present, else a data-grounded stub
# ============================================================================

def _load_plans(asked_at: str, **stub_kw: Any) -> list[Any]:
    """Prefer Window 1's avoidance generator; fall back to the stub.

    Window 1 contract: expose one of `plans_for` / `fleet_plans` /
    `avoidance_plans` / `plans_for_scenario` on `agent.avoidance`, taking an
    asked_at and returning AvoidancePlan-like objects.
    """
    try:
        import agent.avoidance as av  # type: ignore
        for name in ("plans_for", "fleet_plans", "avoidance_plans", "plans_for_scenario"):
            fn = getattr(av, name, None)
            if callable(fn):
                plans = fn(asked_at)
                if plans:
                    return list(plans)
    except Exception:
        pass
    return _stub_plans(asked_at, **stub_kw)


# --- data-grounded stub: transiting flights from routes x storm cells --------

# Synthesized aircraft types, since routes.json carries no type. A US-fleet-ish
# mix so per-type fuel/seats vary realistically. Window 1's plan will carry the
# real type and this is bypassed.
_ACTYPE_POOL = ["B738", "A320", "A321", "B739", "E75L", "CRJ9",
                "A20N", "B752", "E190", "A21N", "BCS3", "E170"]


def _assign_actype(uid: str) -> str:
    h = int(hashlib.md5(uid.encode()).hexdigest()[:8], 16)
    return _ACTYPE_POOL[h % len(_ACTYPE_POOL)]


def _point_in_polygon(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
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


def _cell_geom(polygon: list[tuple[float, float]]) -> tuple[tuple[float, float, float, float], float]:
    """(bbox, cross_nm) for a (lat,lon) polygon. cross_nm ~ the smaller bbox span
    (the distance a route typically punches through the cell)."""
    lats = [p[0] for p in polygon]
    lons = [p[1] for p in polygon]
    lamin, lamax, lomin, lomax = min(lats), max(lats), min(lons), max(lons)
    mean_lat = math.radians((lamin + lamax) / 2)
    h_nm = (lamax - lamin) * NM_PER_DEG_LAT
    w_nm = (lomax - lomin) * NM_PER_DEG_LAT * max(math.cos(mean_lat), 1e-6)
    cross_nm = max(min(h_nm, w_nm), 5.0)
    return (lamin, lamax, lomin, lomax), cross_nm


def _cells_for_strip(asked_at: str, strip: Any) -> list[dict]:
    """Storm cells for one strip as {polygon, bbox, cross_nm, retop_ft, max_dbz}."""
    out = []
    for f in scenario_wx.hazard_polygons(asked_at, strip.valid_from, dbz=scenario_wx.DBZ_HEAVY):
        poly = f.metadata.get("polygon") or []
        if len(poly) < 3:
            continue
        bbox, cross_nm = _cell_geom(poly)
        out.append({
            "polygon": poly, "bbox": bbox, "cross_nm": cross_nm,
            "retop_ft": float(f.metadata.get("retop_ft") or 0.0),
            "max_dbz": float(f.metadata.get("max_dbz") or 0.0),
        })
    return out


def _hit_cell(lat: float, lon: float, alt_ft: float, cells: list[dict]) -> dict | None:
    """First cell whose polygon contains (lat,lon) with the aircraft at/below the
    echo top — i.e. scenario_wx's affects rule (alt<=retop and inside refc>=40)."""
    for c in cells:
        lamin, lamax, lomin, lomax = c["bbox"]
        if not (lamin <= lat <= lamax and lomin <= lon <= lomax):
            continue
        if alt_ft <= c["retop_ft"] and _point_in_polygon(lat, lon, c["polygon"]):
            return c
    return None


def _options_for(fl: Any, cell: dict) -> list[AvoidanceOption]:
    """Synthesize the three maneuver archetypes for one flight x hit cell."""
    perf = _perf(_assign_actype(fl.uid))
    cruise_kt = fl.cruise_speed_kt or perf["cruise_kt"] or 450.0
    cruise_alt = fl.cruise_altitude_ft
    cross_nm = cell["cross_nm"]
    retop = cell["retop_ft"]
    turb_min = (cross_nm / cruise_kt) * 60.0           # time spent in the cell

    # detour ~= cell width + a standoff on each side
    extra_nm = cross_nm + 2.0 * CONVECTIVE_STANDOFF_NM
    opts: list[AvoidanceOption] = [
        AvoidanceOption(kind="lateral", extra_nm=extra_nm,
                        turb_min_avoided=turb_min, label="fly around"),
    ]
    target = retop + CLIMB_CLEARANCE_FT
    if cruise_alt < retop and target <= perf["ceiling_ft"]:
        climb_min = max((target - cruise_alt) / max(perf["climb_fpm"], 1.0), 0.0)
        opts.append(AvoidanceOption(kind="climb", climb_min=climb_min, target_alt_ft=target,
                                    turb_min_avoided=turb_min, label="climb over"))
    opts.append(AvoidanceOption(kind="wait", delay_min=round(turb_min + 12.0),
                                turb_min_avoided=turb_min, label="let it pass"))
    return opts


def _stub_plans(asked_at: str, *, window_hours: float = 8.0, strip_stride: int = 1,
                max_flights: int | None = None) -> list[AvoidancePlan]:
    """Derive transiting flights (and their option menus) from the scenario data.

    Walks the convective strips covering [asked_at, asked_at+window_hours]; at
    each strip's time, samples every airborne flight's position and keeps those
    inside a storm cell below its echo top. One plan per unique flight (earliest
    hit). Clearly a stand-in for Window 1's avoidance.py — same AvoidancePlan
    shape, so the engine and CZML are unchanged when it lands.
    """
    scn = scenario_routes.load_scenario(asked_at)
    strips = scenario_wx.list_strips(asked_at, "refc")
    t0, t1 = scn.asked_at, scn.asked_at + window_hours * 3600.0
    window_strips = [s for s in strips if t0 <= s.valid_from.timestamp() <= t1][::max(strip_stride, 1)]

    plans: dict[str, AvoidancePlan] = {}
    for strip in window_strips:
        cells = _cells_for_strip(asked_at, strip)
        if not cells:
            continue
        t = strip.valid_from
        for fl in scn.airborne_at(t):
            if fl.uid in plans:
                continue
            pos = scenario_routes.position_at(fl, t)
            if pos is None:
                continue
            lat, lon, alt_ft = pos
            cell = _hit_cell(lat, lon, alt_ft, cells)
            if cell is None:
                continue
            actype = _assign_actype(fl.uid)
            plans[fl.uid] = AvoidancePlan(
                flight_id=fl.uid, actype=actype, options=_options_for(fl, cell),
                callsign=fl.flight_number, origin=fl.origin_airport_icao,
                dest=fl.destination_airport_icao, cruise_alt_ft=fl.cruise_altitude_ft,
                cruise_kt=fl.cruise_speed_kt, lats=list(fl.lats), lons=list(fl.lons),
                cell_polygon=cell["polygon"], hit_time=strip.valid_from.timestamp(),
            )
            if max_flights and len(plans) >= max_flights:
                return list(plans.values())
    return list(plans.values())


# ============================================================================
# Acceptance demo
# ============================================================================

def _demo(asked_at: str = "2025-08-22T18:00:00Z") -> None:
    res = fleet_impact(asked_at)
    print(res["headline"])
    print()
    t4, t2 = res["totals_4d"], res["totals_2d"]
    print(f"scenario: {asked_at}   transiting flights: {res['n_flights']}")
    print(f"  4D (cheapest):  {t4['fuel_gal']:>8,.0f} gal  "
          f"{t4['co2_kg']:>10,.0f} kg CO2  {t4['delay_min']:>7,.0f} delay-min  "
          f"${weighted_cost(t4) if False else t4['cost']:>12,.0f} cost")
    print(f"  2D (around):    {t2['fuel_gal']:>8,.0f} gal  "
          f"{t2['co2_kg']:>10,.0f} kg CO2  {t2['delay_min']:>7,.0f} delay-min  "
          f"${t2['cost']:>12,.0f} cost")
    print(f"  turbulence-min avoided: {t4['turb_min_avoided']:,.0f}   "
          f"pax protected: {t4['pax']:,.0f}")
    print("  per-maneuver (4D choices):")
    for k, b in res["breakdown"].items():
        print(f"    {k:<8} {b['n']:>4} flights   {b['fuel_gal']:>8,.0f} gal   "
              f"{b['delay_min']:>7,.0f} delay-min")


if __name__ == "__main__":
    import sys
    _demo(sys.argv[1] if len(sys.argv) > 1 else "2025-08-22T18:00:00Z")
