"""Aircraft performance lookup — hardcoded reference data for common ICAO types.

Numbers are typical / mid-range values appropriate for dispatcher reasoning.
Sourced from manufacturer publications and FAA TBL pubs (Airbus / Boeing /
Embraer / Bombardier). Not precision performance tables — they're "what an
experienced dispatcher would say off the top of their head."

Used by specialists to enrich findings with type-appropriate context:
    "AAL892 is a B789 — service ceiling FL430, could climb 2,000 ft to
     clear MOD turbulence at minimal fuel cost"

To extend: add a new entry; everything is a lookup.
"""
from __future__ import annotations
from typing import Any


# icao_type -> performance dict
PERF: dict[str, dict[str, Any]] = {
    # ---- Narrow-body Airbus / Boeing ----
    "A319": dict(name="Airbus A319",  cruise_kt=447, ceiling_ft=39800, climb_fpm=2500, fuel_lb_hr=4900, seats=140),
    "A320": dict(name="Airbus A320",  cruise_kt=447, ceiling_ft=39800, climb_fpm=2500, fuel_lb_hr=5200, seats=180),
    "A321": dict(name="Airbus A321",  cruise_kt=447, ceiling_ft=39800, climb_fpm=2200, fuel_lb_hr=5800, seats=220),
    "A20N": dict(name="A320neo",      cruise_kt=455, ceiling_ft=39800, climb_fpm=2700, fuel_lb_hr=4400, seats=180),
    "A21N": dict(name="A321neo",      cruise_kt=455, ceiling_ft=39800, climb_fpm=2400, fuel_lb_hr=4900, seats=240),
    "B737": dict(name="Boeing 737",   cruise_kt=453, ceiling_ft=41000, climb_fpm=2500, fuel_lb_hr=5500, seats=150),
    "B738": dict(name="737-800",      cruise_kt=453, ceiling_ft=41000, climb_fpm=2500, fuel_lb_hr=5700, seats=180),
    "B739": dict(name="737-900",      cruise_kt=453, ceiling_ft=41000, climb_fpm=2300, fuel_lb_hr=6000, seats=215),
    "B38M": dict(name="737 MAX 8",    cruise_kt=453, ceiling_ft=41000, climb_fpm=2700, fuel_lb_hr=4900, seats=180),
    "B39M": dict(name="737 MAX 9",    cruise_kt=453, ceiling_ft=41000, climb_fpm=2500, fuel_lb_hr=5200, seats=200),
    "B752": dict(name="757-200",      cruise_kt=470, ceiling_ft=42000, climb_fpm=3500, fuel_lb_hr=7600, seats=200),
    "B753": dict(name="757-300",      cruise_kt=470, ceiling_ft=42000, climb_fpm=3000, fuel_lb_hr=8100, seats=240),

    # ---- Wide-body Airbus / Boeing ----
    "A332": dict(name="A330-200",     cruise_kt=470, ceiling_ft=41000, climb_fpm=2000, fuel_lb_hr=12000, seats=250),
    "A333": dict(name="A330-300",     cruise_kt=470, ceiling_ft=41000, climb_fpm=1900, fuel_lb_hr=12500, seats=300),
    "A339": dict(name="A330-900neo",  cruise_kt=470, ceiling_ft=41000, climb_fpm=2100, fuel_lb_hr=10800, seats=310),
    "A359": dict(name="A350-900",     cruise_kt=488, ceiling_ft=43100, climb_fpm=2400, fuel_lb_hr=11500, seats=325),
    "A35K": dict(name="A350-1000",    cruise_kt=488, ceiling_ft=43100, climb_fpm=2200, fuel_lb_hr=12500, seats=370),
    "A388": dict(name="A380-800",     cruise_kt=490, ceiling_ft=43000, climb_fpm=1500, fuel_lb_hr=24000, seats=525),
    "B763": dict(name="767-300",      cruise_kt=470, ceiling_ft=43000, climb_fpm=2400, fuel_lb_hr=11500, seats=270),
    "B764": dict(name="767-400ER",    cruise_kt=470, ceiling_ft=43000, climb_fpm=2200, fuel_lb_hr=12200, seats=300),
    "B772": dict(name="777-200",      cruise_kt=487, ceiling_ft=43100, climb_fpm=2400, fuel_lb_hr=15000, seats=315),
    "B77L": dict(name="777-200LR",    cruise_kt=487, ceiling_ft=43100, climb_fpm=2200, fuel_lb_hr=15500, seats=315),
    "B77W": dict(name="777-300ER",    cruise_kt=487, ceiling_ft=43100, climb_fpm=2000, fuel_lb_hr=17000, seats=400),
    "B788": dict(name="787-8",        cruise_kt=487, ceiling_ft=43000, climb_fpm=2500, fuel_lb_hr=11000, seats=240),
    "B789": dict(name="787-9",        cruise_kt=487, ceiling_ft=43000, climb_fpm=2400, fuel_lb_hr=11500, seats=290),
    "B78X": dict(name="787-10",       cruise_kt=487, ceiling_ft=43000, climb_fpm=2200, fuel_lb_hr=12000, seats=330),
    "B748": dict(name="747-8",        cruise_kt=493, ceiling_ft=43000, climb_fpm=2000, fuel_lb_hr=23500, seats=410),

    # ---- Regional jets ----
    "CRJ2": dict(name="CRJ-200",      cruise_kt=453, ceiling_ft=41000, climb_fpm=2700, fuel_lb_hr=2400, seats=50),
    "CRJ7": dict(name="CRJ-700",      cruise_kt=447, ceiling_ft=41000, climb_fpm=2400, fuel_lb_hr=2700, seats=70),
    "CRJ9": dict(name="CRJ-900",      cruise_kt=447, ceiling_ft=41000, climb_fpm=2200, fuel_lb_hr=3000, seats=90),
    "CRJX": dict(name="CRJ-1000",     cruise_kt=447, ceiling_ft=41000, climb_fpm=2000, fuel_lb_hr=3200, seats=100),
    "E145": dict(name="ERJ-145",      cruise_kt=450, ceiling_ft=37000, climb_fpm=2300, fuel_lb_hr=2300, seats=50),
    "E170": dict(name="E170",         cruise_kt=460, ceiling_ft=41000, climb_fpm=2700, fuel_lb_hr=3000, seats=78),
    "E75L": dict(name="E175 LR",      cruise_kt=460, ceiling_ft=41000, climb_fpm=2700, fuel_lb_hr=3100, seats=88),
    "E75S": dict(name="E175",         cruise_kt=460, ceiling_ft=41000, climb_fpm=2700, fuel_lb_hr=3100, seats=78),
    "E190": dict(name="E190",         cruise_kt=470, ceiling_ft=41000, climb_fpm=2400, fuel_lb_hr=3400, seats=100),
    "E195": dict(name="E195",         cruise_kt=470, ceiling_ft=41000, climb_fpm=2300, fuel_lb_hr=3600, seats=124),
    "E290": dict(name="E190-E2",      cruise_kt=470, ceiling_ft=41000, climb_fpm=2600, fuel_lb_hr=2900, seats=106),
    "E295": dict(name="E195-E2",      cruise_kt=470, ceiling_ft=41000, climb_fpm=2400, fuel_lb_hr=3100, seats=132),
    "BCS1": dict(name="A220-100",     cruise_kt=470, ceiling_ft=41000, climb_fpm=2500, fuel_lb_hr=3500, seats=110),
    "BCS3": dict(name="A220-300",     cruise_kt=470, ceiling_ft=41000, climb_fpm=2300, fuel_lb_hr=3800, seats=130),

    # ---- Turboprops ----
    "ATR4": dict(name="ATR 42",       cruise_kt=300, ceiling_ft=25000, climb_fpm=1400, fuel_lb_hr=1100, seats=48),
    "AT72": dict(name="ATR 72",       cruise_kt=275, ceiling_ft=25000, climb_fpm=1300, fuel_lb_hr=1500, seats=72),
    "DH8D": dict(name="Dash 8 Q400",  cruise_kt=360, ceiling_ft=27000, climb_fpm=1700, fuel_lb_hr=1700, seats=78),

    # ---- Business jets ----
    "GLF5": dict(name="Gulfstream V", cruise_kt=488, ceiling_ft=51000, climb_fpm=3500, fuel_lb_hr=3800, seats=14),
    "GLF6": dict(name="G650",         cruise_kt=518, ceiling_ft=51000, climb_fpm=3800, fuel_lb_hr=4000, seats=18),
    "GLEX": dict(name="Global Express",cruise_kt=488,ceiling_ft=51000, climb_fpm=3700, fuel_lb_hr=4200, seats=14),
    "C56X": dict(name="Citation Excel",cruise_kt=433,ceiling_ft=45000, climb_fpm=3500, fuel_lb_hr=1300, seats=8),
    "C68A": dict(name="Citation Latitude",cruise_kt=446,ceiling_ft=45000,climb_fpm=3700,fuel_lb_hr=1500,seats=9),

    # ---- Cargo specialty ----
    "MD11": dict(name="MD-11F",       cruise_kt=478, ceiling_ft=43000, climb_fpm=2200, fuel_lb_hr=18000, seats=0),
    "A124": dict(name="An-124",       cruise_kt=432, ceiling_ft=39000, climb_fpm=1700, fuel_lb_hr=29000, seats=0),
}


def lookup(icao_type: str) -> dict[str, Any] | None:
    """Return performance dict for an ICAO type code, or None if unknown."""
    if not icao_type:
        return None
    return PERF.get(icao_type.upper().strip())


def describe_for_advice(icao_type: str) -> str:
    """One-line, dispatcher-voice description used inside Finding summaries."""
    p = lookup(icao_type)
    if not p:
        return f"unknown type ({icao_type})"
    return (f"{p['name']} — cruise {p['cruise_kt']}kt, ceiling FL{p['ceiling_ft']//100}, "
            f"climb {p['climb_fpm']}fpm, ~{p['fuel_lb_hr']:,}lb/hr")


def known_types() -> list[str]:
    return sorted(PERF.keys())
