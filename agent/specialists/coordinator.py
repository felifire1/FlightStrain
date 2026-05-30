"""Coordinator — the only specialist that talks to the user.

Also does cross-agent correlation: when a high-severity weather finding lands,
it looks at the latest traffic snapshot, intersects predicted aircraft tracks
with the hazard polygon, and emits an *actionable* finding listing affected
flights with map_actions to highlight them.

Two modes of speech:

1. **Reactive (on user message).**
   User sends a chat message. Coordinator pulls latest findings from each
   specialist, synthesizes into one reply (with map_actions composed across
   findings), and returns. This is what `handle_user()` does.

2. **Proactive (push).**
   A specialist publishes a high-severity finding. Coordinator decides
   whether to forward it to the chat surface unprompted. This is what
   `should_push()` decides.

In stub mode the synthesis is template-based. With `inject_llm()` it becomes
a real LLM call that the other session wires up from server.py using the
existing Anthropic client.
"""
from __future__ import annotations
import json
import math
from typing import Any, Callable

from .base import Event, Finding, Manifest, Specialist
from .bus import bus


# --- cross-correlation helpers ----------------------------------------------

def _point_in_polygon(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting test. polygon is list of (lat, lon). Handles convex/concave."""
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


def _project_forward(lat: float, lon: float, heading_deg: float, speed_ms: float, dt_sec: float) -> tuple[float, float]:
    """Spherical-earth dead reckoning. Returns (lat, lon) after dt_sec at speed/heading."""
    if heading_deg is None or speed_ms is None:
        return lat, lon
    R = 6_371_000.0
    d = speed_ms * dt_sec
    brg = math.radians(heading_deg)
    p1 = math.radians(lat); l1 = math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d / R) + math.cos(p1) * math.sin(d / R) * math.cos(brg))
    l2 = l1 + math.atan2(math.sin(brg) * math.sin(d / R) * math.cos(p1),
                         math.cos(d / R) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


def _polygon_bbox(polygon: list[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    if not polygon:
        return None
    lats = [p[0] for p in polygon]
    lons = [p[1] for p in polygon]
    return (min(lats), max(lats), min(lons), max(lons))


class Coordinator(Specialist):
    manifest = Manifest(
        name="coordinator",
        description="The operations supervisor. Talks to the dispatcher (user). Synthesizes findings from specialists. Decides when an unprompted alert is warranted.",
        model="claude-opus-4-7",  # the smartest agent, since it integrates the others
        system_prompt=(
            "You are the Coordinator — the operations supervisor in a multi-agent "
            "ATC operations room. Five specialists watch the data: WeatherAgent, "
            "TrafficAgent, SafetyAgent, FleetAgent, and NarratorAgent. You are "
            "the only one that speaks to the user. When the user asks a question, "
            "you pull the latest findings from your specialists, integrate them, "
            "and answer concisely. When a specialist surfaces something high-"
            "severity unprompted, you decide whether to surface it to the user. "
            "Always credit which specialist contributed which insight."
        ),
        tool_refs=[],
        interests=["user.question", "finding.published"],
    )

    # severity at which we push unprompted to the chat surface
    PUSH_THRESHOLD = 3

    def __init__(self, mode: str = "stub", specialists: list[Specialist] | None = None):
        super().__init__(mode)
        self.specialists = specialists or []

    def add_specialist(self, s: Specialist) -> None:
        self.specialists.append(s)

    # --- Specialist ABC compliance (used if Coordinator itself subscribes to events) ---

    def formulate(self, event: Event, context: dict[str, Any] | None = None):
        if event.type == "user.question":
            yield self._answer(event.payload.get("text", ""))
        # other event types not synthesized; the chat server polls handle_user directly

    # --- Public API the other session calls from agent/server.py ---

    def handle_user(self, message: str, history: list | None = None,
                    traffic_states: list[dict] | None = None) -> dict[str, Any]:
        """Synchronous request/response for /api/chat.

        Returns the same shape the existing chat panel already understands:
            {text, history, tool_trace, map_actions}
        Plus a `voices` list — one entry per contributing specialist.

        Pass `traffic_states` so location-aware questions can filter aircraft.
        """
        # Fast-path for "who is over <city>" / "traffic near <city>" patterns.
        if traffic_states:
            loc = self._try_location_query(message, traffic_states)
            if loc:
                bus.publish(loc)
                return {
                    "text": loc.summary,
                    "history": (history or []) + [
                        {"role": "user", "content": message},
                        {"role": "assistant", "content": loc.summary},
                    ],
                    "tool_trace": [{"name": "get_traffic", "args": {}}],
                    "map_actions": loc.map_actions,
                    "voices": [{"specialist": loc.specialist, "severity": loc.severity, "summary": loc.summary[:60]}],
                }

        # 1. Route the question to each interested specialist as an event
        event = Event(type="user.question", payload={"text": message}, source="user")
        per_specialist_findings: list[Finding] = []
        for s in self.specialists:
            if "user.question" in s.interests():
                for f in s.formulate(event):
                    bus.publish(f)
                    per_specialist_findings.append(f)

        # 2. Also pull any recent findings from the bus (background watchers)
        recent = bus.latest(n=10)

        # 3. Synthesize
        finding = self._answer(message, contributing=per_specialist_findings + recent)
        bus.publish(finding)

        return {
            "text": finding.summary + (f"\n\n{finding.detail}" if finding.detail else ""),
            "history": (history or []) + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": finding.summary},
            ],
            "tool_trace": [{"name": s, "args": {}} for s in finding.sources],
            "map_actions": finding.map_actions,
            "voices": [
                {"specialist": f.specialist, "severity": f.severity, "summary": f.summary}
                for f in per_specialist_findings + recent
            ],
        }

    def should_push(self, finding: Finding) -> bool:
        """Called by the chat server when a finding lands on the bus. If True,
        the server pushes it as an unprompted chat message."""
        return finding.severity >= self.PUSH_THRESHOLD

    # --- cross-agent correlation ---------------------------------------------

    LOOKAHEAD_SEC = 30 * 60  # how far forward we project aircraft tracks

    # Hard-coded for the demo. Tomorrow the LLM can free-form parse city names.
    KNOWN_AREAS = {
        "boston":     (42.36, -71.01, 0.5),
        "bos":        (42.36, -71.01, 0.5),
        "new york":   (40.78, -73.87, 0.5),
        "nyc":        (40.78, -73.87, 0.5),
        "jfk":        (40.64, -73.78, 0.4),
        "lga":        (40.78, -73.87, 0.4),
        "ewr":        (40.69, -74.17, 0.4),
        "chicago":    (41.98, -87.91, 0.5),
        "ord":        (41.98, -87.91, 0.5),
        "atlanta":    (33.64, -84.43, 0.5),
        "atl":        (33.64, -84.43, 0.5),
        "dallas":     (32.90, -97.04, 0.5),
        "dfw":        (32.90, -97.04, 0.5),
        "denver":     (39.86, -104.67, 0.5),
        "los angeles":(33.94, -118.41, 0.5),
        "lax":        (33.94, -118.41, 0.5),
        "san francisco":(37.62, -122.38, 0.5),
        "sfo":        (37.62, -122.38, 0.5),
    }

    def _try_location_query(self, message: str, traffic_states: list[dict]) -> Finding | None:
        """If the message looks like 'who is over X' or 'traffic near X',
        filter traffic to that area and produce a targeted reply."""
        msg = message.lower()
        if not any(w in msg for w in ("over ", "near ", "around ", "above ", "in ")):
            return None
        for name, (lat, lon, rad_deg) in self.KNOWN_AREAS.items():
            if name in msg:
                hits = []
                for s in traffic_states:
                    s_lat = s.get("lat"); s_lon = s.get("lon")
                    if s_lat is None or s_lon is None:
                        continue
                    if s.get("on_ground"):
                        continue  # "over X" means airborne
                    if abs(s_lat - lat) <= rad_deg and abs(s_lon - lon) <= rad_deg:
                        hits.append(s)
                hits.sort(key=lambda s: -(s.get("baro_alt") or 0))
                shown = hits[:8]
                if not hits:
                    summary = f"No aircraft tracked within {rad_deg:.0f}° of {name.title()} right now."
                else:
                    lines = []
                    for s in shown:
                        cs = (s.get("callsign") or "").strip() or s.get("icao24")
                        alt = int((s.get("baro_alt") or 0) * 3.28084)
                        vel = int((s.get("velocity") or 0) * 1.94384)
                        hdg = int(s.get("heading") or 0)
                        lines.append(f"  {cs:10}  FL{alt//100:03d}  {vel}kt  hdg {hdg:03d}°")
                    extra = "" if len(hits) <= 8 else f"\n  …and {len(hits)-8} more"
                    summary = (
                        f"{len(hits)} aircraft within {rad_deg:.0f}° of {name.title()}:\n"
                        + "\n".join(lines) + extra
                    )
                map_actions = []
                if shown:
                    map_actions.append({"action": "fly_to", "lat": lat, "lon": lon, "alt_m": 200_000, "pitch_deg": -55})
                    for s in shown[:3]:
                        if s.get("icao24"):
                            map_actions.append({"action": "highlight_flight", "icao24": s["icao24"]})
                return Finding(
                    specialist=self.name, severity=1,
                    summary=summary, map_actions=map_actions,
                    sources=["get_traffic"], metadata={"location": name, "matched": len(hits)},
                )
        return None

    def correlate(self, traffic_states: list[dict], hazard_findings: list[Finding]) -> list[Finding]:
        """For each weather hazard finding with a polygon, find aircraft whose
        current OR projected position falls inside it. Emit one Finding per
        hazard listing the affected flights, with map_actions ready to fire.

        Returns the list of new findings (already published to the bus)."""
        out: list[Finding] = []
        if not traffic_states or not hazard_findings:
            return out

        for hf in hazard_findings:
            polygon = hf.metadata.get("polygon") or []
            if len(polygon) < 3:
                continue
            bbox = _polygon_bbox(polygon)
            affected: list[dict] = []
            for s in traffic_states:
                lat = s.get("lat"); lon = s.get("lon")
                if lat is None or lon is None or s.get("on_ground"):
                    continue
                # Fast bbox pre-filter (8x larger to catch close approaches)
                if bbox:
                    lamin, lamax, lomin, lomax = bbox
                    pad = 2.0  # degrees ~ 120 nm of pre-filter slack
                    if not (lamin - pad <= lat <= lamax + pad and
                            lomin - pad <= lon <= lomax + pad):
                        continue
                vel = s.get("velocity") or 0
                hdg = s.get("heading")
                # Sample 0/10/20/30 min ahead; earliest intersection wins
                eta_min = None
                for t_min in (0, 5, 10, 15, 20, 30):
                    plat, plon = _project_forward(lat, lon, hdg or 0, vel, t_min * 60)
                    if _point_in_polygon(plat, plon, polygon):
                        eta_min = t_min
                        break
                if eta_min is not None:
                    affected.append({
                        "icao24": s.get("icao24"),
                        "callsign": (s.get("callsign") or "").strip(),
                        "eta_min": eta_min,
                        "alt_ft": int((s.get("baro_alt") or 0) * 3.28084),
                    })

            if not affected:
                continue
            # Sort by ETA so the most urgent show first
            affected.sort(key=lambda a: a["eta_min"])
            shown = affected[:5]
            tail = "" if len(affected) <= 5 else f" (+{len(affected) - 5} more)"
            lines = ", ".join(
                f"{a['callsign'] or a['icao24']}@FL{a['alt_ft']//100:03d} in {a['eta_min']}min"
                for a in shown
            )
            hazard_label = hf.metadata.get("states") or hf.metadata.get("sigmet_id") or "hazard"
            summary = (
                f"⚠ {len(affected)} flight(s) projected to transit "
                f"{hazard_label} polygon in next 30 min: {lines}{tail}."
            )
            map_actions = [{"action": "load_layer", "layer": "sigmet"}]
            # Propose an "avoid zone" polygon that's the hazard polygon, drawn
            # by the agent so the dispatcher sees the visual recommendation.
            polygon = hf.metadata.get("polygon") or []
            if polygon:
                # polygon stored as (lat, lon); map_action wants (lon, lat)
                pts = [[lon, lat] for (lat, lon) in polygon]
                map_actions.append({
                    "action": "draw_polygon",
                    "id": f"avoid-{hf.metadata.get('sigmet_id') or 'hazard'}",
                    "points": pts,
                    "color": [255, 60, 60, 90],
                    "label": "AVOID — proposed by coordinator",
                    "height_m": 0,
                    "extruded_m": 12000,
                })
            for a in shown:
                if a["icao24"]:
                    map_actions.append({"action": "highlight_flight", "icao24": a["icao24"]})

            f = Finding(
                specialist=self.name,
                severity=4,
                summary=summary,
                recommended_action=(
                    "Issue reroute advisory to listed flights. Vector north/south of "
                    "polygon depending on filed route; coordinate with TMU."
                ),
                map_actions=map_actions,
                sources=["get_traffic", "get_sigmets"],
                metadata={
                    "hazard_id": hf.metadata.get("sigmet_id"),
                    "affected_count": len(affected),
                    "flights": affected,
                },
            )
            if bus.publish(f):
                out.append(f)
        return out

    # --- internal synthesis ---

    def _answer(self, message: str, contributing: list[Finding] | None = None) -> Finding:
        contributing = contributing or []

        if self.mode == "llm" and self._llm:
            # The other session provides _llm; signature is (system, user, tools) -> str
            ctx = json.dumps([f.to_dict() for f in contributing], default=str)
            llm_user = (
                f"User: {message}\n\n"
                f"Latest specialist findings (most recent first):\n{ctx}\n\n"
                "Integrate these into one concise answer. Credit the specialist for each insight. "
                "If any have map_actions, prefer the highest-severity one."
            )
            try:
                text = self._llm(self.manifest.system_prompt, llm_user, [])
            except Exception as e:
                text = self._stub_synthesis(message, contributing) + f"\n\n(llm error: {e})"
        else:
            text = self._stub_synthesis(message, contributing)

        # Compose map actions: take the highest-severity finding's actions
        contributing_sorted = sorted(contributing, key=lambda f: -f.severity)
        map_actions = []
        for f in contributing_sorted:
            if f.map_actions:
                map_actions = f.map_actions
                break

        return Finding(
            specialist=self.name,
            severity=max([f.severity for f in contributing] + [0]),
            summary=text,
            map_actions=map_actions,
            sources=list({s for f in contributing for s in f.sources}),
            metadata={"contributed_by": [f.specialist for f in contributing]},
        )

    def _stub_synthesis(self, message: str, contributing: list[Finding]) -> str:
        if not contributing:
            return ("No active findings from the specialists for that question. "
                    "Try asking about traffic, weather, or recent advisories.")
        # Group by specialist
        by_spec: dict[str, list[Finding]] = {}
        for f in contributing:
            by_spec.setdefault(f.specialist, []).append(f)
        lines = [f"Multi-agent synthesis re: '{message[:80]}'"]
        for spec, findings in by_spec.items():
            top = max(findings, key=lambda f: f.severity)
            lines.append(f"\n[{spec.upper()}] {top.summary}")
            if top.recommended_action:
                lines.append(f"  → {top.recommended_action}")
        return "\n".join(lines)
