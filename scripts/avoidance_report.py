#!/usr/bin/env python3
"""Turbulence-avoidance report builder.

Renders a readable markdown report from the fleet-level avoidance analysis:
which flights were on a path to transit convective weather, how each can avoid
it across the 4D field (climb / wait / lateral), and the fuel / delay / CO2 /
passenger impact — plus the headline "4D beats 2D by X%".

Decoupled by design (Window 1 owns agent/avoidance.py, Window 2 owns
agent/impact.py): this only *renders* the structured dict those produce. It
consumes `agent.impact.fleet_impact(asked_at)` when available; otherwise it
falls back to a clearly-labelled --dry-run synthetic fleet so the template is
locked today and just swaps in real numbers when the engine lands.

Runs fully offline on the cached scenario bundle — deterministic, demo-safe.

Usage:
    python scripts/avoidance_report.py                       # auto: real if available, else dry-run
    python scripts/avoidance_report.py --asked-at 2025-08-22T18:00:00Z
    python scripts/avoidance_report.py --dry-run             # force synthetic
    python scripts/avoidance_report.py --out report.md       # custom output path

Expected fleet_impact(asked_at) schema (the contract with agent/impact.py):
    {
      "asked_at": str,
      "n_transiting": int,
      "n_avoidable": int,
      "totals": {"fuel_gal", "delay_min", "co2_kg", "pax", "turb_min_avoided"},
      "twod_fuel_gal": float,          # baseline: everyone reroutes laterally (2D)
      "fourd_fuel_gal": float,         # cheapest-per-flight across the 4D field
      "savings_pct_vs_2d": float,
      "by_maneuver": { axis: {"flights", "avg_fuel_gal", "avg_delay_min", "note"} },
      "flights": [ {"callsign","actype","eta_min","maneuver","fuel_gal",
                    "delay_min","turb_min_avoided","pax","note"} ],
    }
Missing keys degrade gracefully (rendered as "—").
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_ASKED_AT = "2025-08-22T18:00:00Z"

# Maneuver display glyphs (axes from the AvoidanceOption contract).
_GLYPH = {"up": "↑ climb over", "down": "↓ descend under", "around": "↔ lateral",
          "wait": "⏱ wait it out", "speed": "⏩ speed up"}


def _glyph(axis: str) -> str:
    return _GLYPH.get((axis or "").lower(), axis or "—")


# --- data source: real impact model, else dry-run --------------------------

def get_fleet_impact(asked_at: str, force_dry_run: bool) -> tuple[dict, bool]:
    """Return (fleet_impact_dict, is_dry_run). Tries agent.impact.fleet_impact;
    on ImportError / empty / failure, builds a synthetic fleet."""
    if not force_dry_run:
        try:
            from agent.impact import fleet_impact  # type: ignore
            data = fleet_impact(asked_at)
            if data and data.get("n_transiting"):
                return data, False
        except Exception as e:  # not built yet, or errored — fall back
            print(f"[report] agent.impact unavailable ({type(e).__name__}: {e}); using --dry-run",
                  file=sys.stderr)
    return _synthetic_fleet(asked_at), True


def _synthetic_fleet(asked_at: str) -> dict:
    """Deterministic, realistic-looking fleet using actual aircraft_perf fuel
    rates. Clearly flagged illustrative in the rendered report — NOT real
    analysis. Just exercises the template end-to-end."""
    try:
        from agent.specialists import aircraft_perf as ap
    except Exception:
        ap = None

    LB_PER_GAL = 6.7
    # (callsign, type, eta_min, axis, climb_ft, extra_nm, delay_min, turb_min)
    rows = [
        ("DAL412", "B739", 8, "up", 3000, 0, 1, 14),
        ("AAL892", "B789", 14, "wait", 0, 0, 9, 22),
        ("UAL1503", "B738", 6, "up", 2000, 0, 1, 11),
        ("SWA2218", "B737", 19, "wait", 0, 0, 7, 9),
        ("JBU641", "A320", 11, "around", 0, 46, 6, 17),
        ("SKW6242", "CRJ9", 22, "wait", 0, 0, 12, 8),
        ("DAL77", "A359", 9, "up", 4000, 0, 2, 19),
        ("AAL215", "B77W", 16, "around", 0, 58, 7, 25),
        ("UAL88", "B752", 13, "up", 3000, 0, 1, 12),
        ("FFT901", "A20N", 24, "wait", 0, 0, 8, 6),
        ("ASA612", "B739", 7, "up", 2000, 0, 1, 13),
        ("NKS445", "A321", 18, "around", 0, 51, 7, 15),
    ]

    def fuel_rate(actype: str) -> float:
        if ap:
            info = ap.lookup(actype)
            if info:
                return info["fuel_lb_hr"]
        return 5500.0

    def seats(actype: str) -> int:
        if ap:
            info = ap.lookup(actype)
            if info:
                return info["seats"]
        return 150

    def cruise(actype: str) -> float:
        if ap:
            info = ap.lookup(actype)
            if info:
                return info["cruise_kt"]
        return 450.0

    flights = []
    twod_fuel = 0.0  # everyone-goes-lateral baseline
    by: dict[str, dict] = {}
    for cs, ac, eta, axis, climb_ft, extra_nm, delay, turb in rows:
        fr = fuel_rate(ac)
        # economic model (mirrors the agent/impact.py ticket constants)
        if axis == "up":
            # extra burn during climb: (climb_min/60) * fuel_lb_hr * 0.5, climb_fpm≈2400
            fuel_gal = (climb_ft / 2400 / 60) * fr * 0.5 / LB_PER_GAL
        elif axis in ("around", "speed"):
            fuel_gal = (extra_nm / cruise(ac)) * fr / LB_PER_GAL
        else:  # wait
            fuel_gal = 0.0
        # 2D baseline: assume a ~50 nm lateral reroute for everyone
        twod_fuel += (50.0 / cruise(ac)) * fr / LB_PER_GAL
        f = {
            "callsign": cs, "actype": ac, "eta_min": eta, "maneuver": axis,
            "fuel_gal": round(fuel_gal, 1), "delay_min": delay,
            "turb_min_avoided": turb, "pax": seats(ac), "note": "",
        }
        flights.append(f)
        b = by.setdefault(axis, {"flights": 0, "_fuel": 0.0, "_delay": 0.0})
        b["flights"] += 1
        b["_fuel"] += fuel_gal
        b["_delay"] += delay

    for axis, b in by.items():
        n = b["flights"]
        b["avg_fuel_gal"] = round(b.pop("_fuel") / n, 1)
        b["avg_delay_min"] = round(b.pop("_delay") / n, 1)
    by_notes = {"up": "echo tops below service ceiling",
                "wait": "cell clears the fix within minutes",
                "around": "tall cell, no room to climb"}
    for axis in by:
        by[axis]["note"] = by_notes.get(axis, "")

    fourd_fuel = sum(f["fuel_gal"] for f in flights)
    co2 = fourd_fuel * 9.6
    savings = round((1 - fourd_fuel / twod_fuel) * 100, 0) if twod_fuel else 0
    return {
        "asked_at": asked_at,
        "n_transiting": len(flights),
        "n_avoidable": len(flights),
        "totals": {
            "fuel_gal": round(fourd_fuel, 0),
            "delay_min": sum(f["delay_min"] for f in flights),
            "co2_kg": round(co2, 0),
            "pax": sum(f["pax"] for f in flights),
            "turb_min_avoided": sum(f["turb_min_avoided"] for f in flights),
        },
        "twod_fuel_gal": round(twod_fuel, 0),
        "fourd_fuel_gal": round(fourd_fuel, 0),
        "savings_pct_vs_2d": savings,
        "by_maneuver": by,
        "flights": flights,
    }


# --- rendering --------------------------------------------------------------

def _g(d: dict, *keys, default="—"):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
    return d


def render(data: dict, is_dry_run: bool) -> str:
    t = data.get("totals", {})
    L: list[str] = []
    L.append(f"# Turbulence Avoidance Report — Scenario {data.get('asked_at', '—')}")
    L.append("")
    if is_dry_run:
        L.append("> ⚠️ **DRY-RUN / ILLUSTRATIVE** — synthetic numbers. The avoidance engine "
                 "(`agent/avoidance.py`) + impact model (`agent/impact.py`) are not wired in yet; "
                 "this shows the report shape. Numbers update automatically when they land.")
    else:
        L.append("_Generated from cached HRRR refc/retop forecast + planned routes. "
                 "Deterministic, offline._")
    L.append("")

    # Headline
    L.append("## Headline")
    L.append(f"**{_g(data,'n_transiting')} flights** were on a path to transit ≥40 dBZ convective "
             f"weather; **{_g(data,'n_avoidable')}** are avoidable. Acting on the 4D field saves an "
             f"estimated **{_g(t,'fuel_gal'):,} gal fuel · {_g(t,'delay_min')} delay-min · "
             f"{_g(t,'co2_kg'):,} kg CO₂**, sparing **~{_g(t,'pax'):,} passengers** "
             f"({_g(t,'turb_min_avoided')} min of moderate-or-worse chop avoided).")
    L.append("")

    # 4D vs 2D callout — the headline insight
    two_d = _g(data, "twod_fuel_gal")
    four_d = _g(data, "fourd_fuel_gal")
    pct = _g(data, "savings_pct_vs_2d")
    L.append(f"> **4D beats 2D by {pct}%.** A lateral-only (2D) system would burn ~**{two_d:,} gal** "
             f"rerouting every flight *around* the storm. Using the full 4D field — climb over, "
             f"wait it out, or deviate — costs ~**{four_d:,} gal**. Time is the cheap escape route "
             f"that 2D tools can't see.")
    L.append("")

    # Per-maneuver breakdown
    L.append("## How they avoid")
    L.append("| Maneuver | Flights | Avg fuel | Avg delay | Why it won |")
    L.append("|---|---:|---:|---:|---|")
    by = data.get("by_maneuver", {}) or {}
    order = ["up", "wait", "around", "speed", "down"]
    for axis in sorted(by, key=lambda a: order.index(a) if a in order else 99):
        m = by[axis]
        L.append(f"| {_glyph(axis)} | {_g(m,'flights')} | {_g(m,'avg_fuel_gal')} gal | "
                 f"{_g(m,'avg_delay_min')} min | {_g(m,'note')} |")
    L.append("")

    # Per-flight detail
    L.append("## Per-flight detail")
    L.append("| Flight | Type | ETA | Maneuver | Δfuel | Δtime | Chop avoided | Pax |")
    L.append("|---|---|---:|---|---:|---:|---:|---:|")
    for f in data.get("flights", []) or []:
        L.append(f"| {_g(f,'callsign')} | {_g(f,'actype')} | {_g(f,'eta_min')} min | "
                 f"{_glyph(f.get('maneuver'))} | {_g(f,'fuel_gal')} gal | {_g(f,'delay_min')} min | "
                 f"{_g(f,'turb_min_avoided')} min | {_g(f,'pax')} |")
    L.append("")

    # Insights
    L.append("## Insights")
    L.append("- The cheapest fleet-wide strategy leans **temporal** (wait/speed) — invisible to "
             "2D lateral-only avoidance.")
    L.append("- High-ceiling widebodies almost always **climb over**; regionals more often "
             "**wait** or **divert**.")
    L.append("- _Stretch:_ altitude shifts vs contrail-forming layers (climate), and sector-load "
             "shift from holds (capacity).")
    L.append("")
    L.append("---")
    L.append("_Extends ASI Flyways' route optimization into the turbulence-decision space, "
             "leveraging a 4D (lat·lon·alt·time) hazard volume._")
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the turbulence-avoidance markdown report.")
    ap.add_argument("--asked-at", default=DEFAULT_ASKED_AT, help="scenario timestamp")
    ap.add_argument("--dry-run", action="store_true", help="force synthetic data")
    ap.add_argument("--out", default=None, help="output path (default: data/reports/...)")
    args = ap.parse_args()

    data, is_dry = get_fleet_impact(args.asked_at, args.dry_run)
    md = render(data, is_dry)

    out = Path(args.out) if args.out else (
        ROOT / "data" / "reports" / f"turbulence_avoidance_{args.asked_at.replace(':', '')}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    tag = " (DRY-RUN)" if is_dry else ""
    print(f"[report]{tag} wrote {out}  ({len(md)} bytes, {len(data.get('flights', []))} flights)")


if __name__ == "__main__":
    main()
