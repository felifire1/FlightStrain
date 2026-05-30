"""FleetAgent — per-airline operational view.

Heuristics:
    - Multiple flights of one airline inbound to a degrading airport -> 2
    - One airline accounts for > N% of holding aircraft -> 2
    - Fuel-state concern: callsign pattern + sustained holding -> 3

Distinct from WeatherAgent and TrafficAgent because the *primary key* is the
operator (airline ICAO prefix). Dispatcher questions like "what's American's
exposure tonight?" route here.
"""
from __future__ import annotations
from collections import Counter
from typing import Any, Iterable
from .base import Specialist, Manifest, Event, Finding


AIRLINE_NAMES = {
    "AAL": "American", "DAL": "Delta", "UAL": "United", "JBU": "JetBlue",
    "SWA": "Southwest", "ASA": "Alaska", "FFT": "Frontier", "NKS": "Spirit",
    "SKW": "SkyWest", "ENY": "Envoy", "RPA": "Republic", "EDV": "Endeavor",
    "FDX": "FedEx", "UPS": "UPS",
}


class FleetAgent(Specialist):
    manifest = Manifest(
        name="fleet",
        description="Per-airline operational picture: exposure to current hazards, holding concentration, fuel-state risk.",
        model="claude-sonnet-4-6",
        system_prompt=(
            "You are the FleetAgent. You group operations by airline (ICAO callsign "
            "prefix) and surface per-operator concerns. Speak in airline terms: "
            "'American has 4 holds, Delta has 2.' Pull numbers from the data; don't "
            "estimate."
        ),
        tool_refs=["get_traffic"],
        interests=["traffic.snapshot"],
    )

    def formulate(self, event: Event, context: dict[str, Any] | None = None) -> Iterable[Finding]:
        if event.type != "traffic.snapshot":
            return
        states = event.payload.get("states") or []
        if not states:
            return

        # All-airline activity counter (airborne only — ground = parked)
        airline_count: Counter = Counter()
        holders: Counter = Counter()
        for s in states:
            cs = (s.get("callsign") or "").strip().upper()
            if not cs or len(cs) < 3 or not cs[:3].isalpha():
                continue
            prefix = cs[:3]
            if s.get("on_ground"):
                continue
            airline_count[prefix] += 1
            alt = s.get("baro_alt") or 0
            vel = s.get("velocity") or 0
            if alt < 4600 and vel < 130:
                holders[prefix] += 1

        if not airline_count:
            return

        # Baseline pulse — top operators visible right now
        top3 = airline_count.most_common(3)
        names = [f"{AIRLINE_NAMES.get(p, p)}={n}" for p, n in top3]
        yield Finding(
            specialist=self.name, severity=1,
            summary=f"Operators airborne: {', '.join(names)}. Tracking {len(airline_count)} distinct airlines.",
            sources=["get_traffic"],
            metadata={"top": [{"prefix": p, "name": AIRLINE_NAMES.get(p, p), "count": n} for p, n in top3]},
        )

        # Holding-pressure alert (lowered threshold)
        if holders:
            top_prefix, n = holders.most_common(1)[0]
            if n >= 2:
                name = AIRLINE_NAMES.get(top_prefix, top_prefix)
                total = sum(holders.values())
                pct = round(100 * n / total) if total else 0
                yield Finding(
                    specialist=self.name, severity=2,
                    summary=(f"{name} accounts for {n}/{total} ({pct}%) of holding aircraft. "
                             f"Likely arrival-flow pressure on their hub."),
                    sources=["get_traffic"],
                    metadata={"airline_prefix": top_prefix, "name": name,
                              "count": n, "total_holding": total, "pct": pct},
                )
