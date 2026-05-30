"""TrafficAgent — watches aircraft state vectors, surfaces flow & conflict.

Heuristics:
    - Aircraft predicted to enter active turbulence polygon in next 15 min -> 3
    - Holding pattern detected (loitering > 8 min near terminal area) -> 2
    - Sudden traffic clustering near an airport (>N inbound in N min) -> 2-3
    - Sector load forecast exceeds threshold -> 2
"""
from __future__ import annotations
from typing import Any, Iterable
from .base import Specialist, Manifest, Event, Finding


class TrafficAgent(Specialist):
    manifest = Manifest(
        name="traffic",
        description="Watches ADS-B state vectors. Identifies conflicts with hazard polygons, sector overload, holding patterns, and arrival pinch points.",
        model="claude-sonnet-4-6",
        system_prompt=(
            "You are the TrafficAgent. You watch live aircraft positions and flag "
            "operational issues: predicted hazard intersections, holding patterns, "
            "arrival flow saturation. Be quantitative — name aircraft, minutes, "
            "altitudes. Cite the trajectory + the polygon."
        ),
        tool_refs=["get_traffic"],
        interests=["traffic.snapshot", "trajectory.predicted"],
    )

    def formulate(self, event: Event, context: dict[str, Any] | None = None) -> Iterable[Finding]:
        if event.type == "traffic.snapshot":
            yield from self._on_snapshot(event)
        elif event.type == "trajectory.predicted":
            yield from self._on_predicted(event)

    def _on_snapshot(self, event: Event) -> Iterable[Finding]:
        states = event.payload.get("states") or []
        if not states:
            return

        airborne = [s for s in states if not s.get("on_ground")]
        ground = len(states) - len(airborne)
        cruise = [s for s in airborne if (s.get("baro_alt") or 0) >= 9000]  # FL295+
        slow_low = [
            s for s in airborne
            if (s.get("baro_alt") or 0) < 4600        # ~FL150
            and (s.get("velocity") or 0) < 130        # ~250 kt
        ]

        # Baseline pulse — always-on summary so the agent reads as "alive"
        stub = (f"Tracking {len(states)} aircraft "
                f"({len(airborne)} airborne, {len(cruise)} at cruise, {ground} on ground). "
                f"{len(slow_low)} flying slow/low.")
        yield Finding(
            specialist=self.name, severity=1, summary=stub,
            sources=["get_traffic"],
            metadata={"total": len(states), "airborne": len(airborne),
                      "cruise": len(cruise), "ground": ground, "slow_low": len(slow_low)},
        )

        # Loitering stack (lowered threshold for quieter hours)
        if len(slow_low) >= 4:
            stub = (f"{len(slow_low)} aircraft loitering below FL150 — possible holding stack. "
                    f"Check arrival flow at nearby hub.")
            yield Finding(
                specialist=self.name, severity=2, summary=stub,
                sources=["get_traffic"], metadata={"count": len(slow_low)},
            )

        # Descent fan: many aircraft below FL100 in a small area = arrival pinch
        descent = [s for s in airborne
                   if 1500 < (s.get("baro_alt") or 0) < 3000
                   and (s.get("vert_rate") or 0) < -2.0]
        if len(descent) >= 3:
            stub = f"{len(descent)} aircraft on descent below FL100 — arrival sequencing in progress."
            yield Finding(
                specialist=self.name, severity=1, summary=stub,
                sources=["get_traffic"], metadata={"count": len(descent)},
            )

    def _on_predicted(self, event: Event) -> Iterable[Finding]:
        # Expected payload shape (other code emits this; we just react):
        #   {flight: {icao24, callsign}, hazard: {id, base_ft, top_ft, ...}, eta_min: 6}
        flight = event.payload.get("flight") or {}
        hazard = event.payload.get("hazard") or {}
        eta_min = event.payload.get("eta_min")
        if not flight or eta_min is None:
            return
        cs = flight.get("callsign") or flight.get("icao24")
        stub = (f"{cs} predicted to enter {hazard.get('hazard', 'hazard')} polygon "
                f"at FL{(hazard.get('base_ft') or 0) // 100}-{(hazard.get('top_ft') or 0) // 100} "
                f"in {eta_min:.0f} min.")
        recommended = None
        if hazard.get("hazard", "").startswith("TURB"):
            recommended = "Check PIREPs at adjacent flight levels; advise altitude change if smoother layer available."
        # The finding can also drive the map: highlight the flight + load turb layer
        map_actions = []
        if flight.get("icao24"):
            map_actions = [
                {"action": "load_layer", "layer": "turb"},
                {"action": "highlight_flight", "icao24": flight["icao24"]},
            ]
        yield Finding(
            specialist=self.name, severity=3,
            summary=stub, recommended_action=recommended,
            map_actions=map_actions,
            sources=["get_traffic", "get_turbulence_advisories"],
            metadata={"flight": flight, "hazard": hazard, "eta_min": eta_min},
        )
