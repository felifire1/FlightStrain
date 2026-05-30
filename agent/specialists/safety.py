"""SafetyAgent — watches for emergencies and abnormal trajectories.

Heuristics:
    - Squawk 7700 (general emergency) -> severity 5
    - Squawk 7600 (radio failure) -> severity 5
    - Squawk 7500 (hijack) -> severity 5
    - Rapid altitude loss (> 2000 fpm sustained) -> severity 4
    - Heading change > 90° with no flight plan deviation reason -> severity 3

Tonight: stub heuristics. In production, this is the agent you can NEVER
tolerate hallucinations from, so deterministic detection drives the alarm
and the LLM only formats the human-readable explanation.
"""
from __future__ import annotations
from typing import Any, Iterable
from .base import Specialist, Manifest, Event, Finding


EMERG_SQUAWKS = {"7500": "hijack", "7600": "radio failure", "7700": "general emergency"}


class SafetyAgent(Specialist):
    manifest = Manifest(
        name="safety",
        description="Detects emergency squawks, rapid descents, anomalous trajectories. Never silent on a real emergency.",
        model="claude-sonnet-4-6",
        system_prompt=(
            "You are the SafetyAgent. You are conservative: prefer false-positives "
            "to missed alarms. Format detected anomalies into clear, urgent advisories. "
            "Never invent an anomaly the data does not show."
        ),
        tool_refs=["get_traffic"],
        interests=["traffic.snapshot"],
    )

    def formulate(self, event: Event, context: dict[str, Any] | None = None) -> Iterable[Finding]:
        if event.type != "traffic.snapshot":
            return
        states = event.payload.get("states") or []
        for s in states:
            sq = s.get("squawk")
            if sq in EMERG_SQUAWKS:
                cs = s.get("callsign") or s.get("icao24") or "unknown"
                kind = EMERG_SQUAWKS[sq]
                stub = f"⚠ SQUAWK {sq} ({kind}) from {cs}. Alert facility supervisor immediately."
                map_actions = []
                if s.get("icao24"):
                    map_actions = [{"action": "highlight_flight", "icao24": s["icao24"]}]
                yield Finding(
                    specialist=self.name,
                    severity=5,
                    summary=stub,
                    recommended_action="Coordinate with sector; clear airspace; standby for EMERG handoff.",
                    map_actions=map_actions,
                    sources=["get_traffic"],
                    metadata={"squawk": sq, "kind": kind, "icao24": s.get("icao24")},
                )

            # Rapid descent: vert_rate is m/s, < -10 m/s ≈ > 2000 fpm down
            vr = s.get("vert_rate")
            alt = s.get("baro_alt") or 0
            if vr is not None and vr < -10.0 and alt > 3000:
                cs = s.get("callsign") or s.get("icao24") or "unknown"
                fpm = int(-vr * 196.85)
                stub = f"{cs} descending {fpm} fpm at FL{int(alt * 0.00328):03d}. Verify operational descent vs anomaly."
                yield Finding(
                    specialist=self.name, severity=4,
                    summary=stub,
                    sources=["get_traffic"],
                    metadata={"icao24": s.get("icao24"), "fpm": fpm, "alt_m": alt},
                )
