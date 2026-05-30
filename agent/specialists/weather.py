"""WeatherAgent — watches METAR/TAF/SIGMET/G-AIRMET/PIREP, flags hazards.

Heuristics (stub mode):
    - New convective SIGMET issued -> severity 4
    - METAR flight category degrading (VFR -> MVFR -> IFR -> LIFR) -> severity 2-3
    - Multiple PIREPs reporting MOD-or-worse turbulence in same region -> 3
    - G-AIRMET TURB-HI severity SEV polygon active -> 3
"""
from __future__ import annotations
from typing import Any, Iterable
from .base import Specialist, Manifest, Event, Finding


CAT_RANK = {"VFR": 0, "MVFR": 1, "IFR": 2, "LIFR": 3}


class WeatherAgent(Specialist):
    manifest = Manifest(
        name="weather",
        description="Watches weather products (METAR, TAF, SIGMETs, PIREPs, G-AIRMETs) and surfaces hazards relevant to airspace operations.",
        model="claude-sonnet-4-6",
        system_prompt=(
            "You are the WeatherAgent in a multi-agent ATC operations room. "
            "You watch weather data feeds and emit concise, quantitative findings "
            "about hazards. Each finding is one sentence + an optional recommended "
            "action. Never speculate beyond the data. Always cite the source "
            "product (METAR/TAF/SIGMET/PIREP/G-AIRMET)."
        ),
        tool_refs=["get_metar", "get_taf", "get_sigmets", "get_turbulence_advisories", "get_pireps", "get_nws_alerts"],
        interests=[
            "sigmet.issued", "sigmet.snapshot",
            "metar.updated",
            "pirep.received", "pirep.snapshot",
            "gairmet.snapshot",
            "nws.alert",
        ],
    )

    def __init__(self, mode: str = "stub"):
        super().__init__(mode)
        self._last_metar_cat: dict[str, str] = {}  # icao -> last fltCat seen

    def formulate(self, event: Event, context: dict[str, Any] | None = None) -> Iterable[Finding]:
        if event.type == "sigmet.issued":
            yield from self._on_sigmet(event)
        elif event.type == "metar.updated":
            yield from self._on_metar(event)
        elif event.type in ("pirep.received", "pirep.snapshot"):
            yield from self._on_pireps(event)
        elif event.type == "gairmet.snapshot":
            yield from self._on_gairmet(event)
        elif event.type == "nws.alert":
            yield from self._on_nws_alert(event)

    # NWS severity -> our scale. Most beach hazards/wind advisories are
    # operationally irrelevant; thunderstorms/tornadoes are critical.
    _SEV_MAP = {"Minor": 1, "Moderate": 2, "Severe": 3, "Extreme": 4}
    # Aviation-relevant alert types. NWS covers a lot of marine/beach hazards
    # we should ignore; this is the operationally-relevant subset.
    _OP_EVENTS = (
        "Thunderstorm", "Tornado", "Severe", "Wind", "Hurricane",
        "Tropical", "Ice", "Snow", "Blizzard", "Winter", "Freezing",
        "Dense Fog", "Low Visibility", "Hail", "Convective",
        "High Wind", "Gale", "Flood",  # flood = airport ramp issues + ground ops
    )

    def _on_nws_alert(self, event: Event) -> Iterable[Finding]:
        p = event.payload
        ev = p.get("event") or "alert"
        # Only flag operationally relevant categories — skip beach hazards, etc.
        if not any(kw in ev for kw in self._OP_EVENTS):
            return
        sev = self._SEV_MAP.get(p.get("severity"), 2)
        # Tornado warnings always bump to sev 4 regardless of NWS classification
        if "Tornado" in ev and "Warning" in ev:
            sev = max(sev, 4)
        area = p.get("area") or "?"
        stub = f"NWS {ev}: {area}. Issued by {p.get('sender') or 'NWS'}. Expires {p.get('expires') or '?'}."
        yield Finding(
            specialist=self.name, severity=sev,
            summary=stub,
            detail=(p.get("description") or "")[:300],
            sources=["get_nws_alerts"],
            metadata={"event": ev, "area": area, "nws_id": p.get("id"),
                      "severity_raw": p.get("severity")},
        )

    # --- handlers ---

    def _on_sigmet(self, event: Event) -> Iterable[Finding]:
        s = event.payload
        if s.get("hazard") != "CONVECTIVE":
            return
        tops = s.get("tops_ft", 40_000)
        states = s.get("states", "")
        stub = f"Convective SIGMET issued for {states}, tops FL{tops // 100}. Reroute candidates: any flights filed through this airspace in next {(s.get('duration_min', 120))} min."
        llm_prompt = f"A new convective SIGMET was issued: {s}. Produce a one-sentence dispatcher advisory."
        yield Finding(
            specialist=self.name,
            severity=4,
            summary=self._stub_or_llm(stub, llm_prompt),
            sources=["get_sigmets"],
            metadata={
                "sigmet_id": s.get("id"),
                "hazard": "CONVECTIVE",
                "polygon": s.get("polygon") or [],   # passed through for the Coordinator
                "tops_ft": tops,
                "states": states,
                "valid_to": s.get("valid_to"),
            },
        )

    def _on_metar(self, event: Event) -> Iterable[Finding]:
        icao = event.payload.get("icao")
        cat = event.payload.get("flight_category")
        if not icao or cat not in CAT_RANK:
            return
        prev = self._last_metar_cat.get(icao)
        self._last_metar_cat[icao] = cat
        if prev is None or CAT_RANK[cat] <= CAT_RANK.get(prev, 0):
            return  # not a degradation
        sev = 2 if CAT_RANK[cat] - CAT_RANK[prev] == 1 else 3
        stub = f"{icao} flight category degraded {prev} -> {cat}. Approach minima may tighten; expect arrival flow restriction."
        yield Finding(
            specialist=self.name,
            severity=sev,
            summary=stub,
            sources=["get_metar"],
            metadata={"icao": icao, "from": prev, "to": cat},
        )

    def _on_pireps(self, event: Event) -> Iterable[Finding]:
        reports = event.payload.get("reports") or []
        bad = [r for r in reports if (r.get("worst_intensity") or "") in ("MOD", "MOD-SEV", "SEV", "EXTM")]
        if len(bad) < 3:
            return
        # Cluster simple: count per coarse region
        from collections import Counter
        regions = Counter()
        for r in bad:
            lat = r.get("lat") or 0
            lon = r.get("lon") or 0
            regions[(round(lat), round(lon))] += 1
        top = regions.most_common(1)[0]
        (lat, lon), n = top
        if n < 3:
            return
        stub = f"{n} PIREPs reporting MOD-or-worse turbulence clustered near ({lat:.0f}°N, {lon:.0f}°W). Consider altitude advisory for transit traffic."
        yield Finding(
            specialist=self.name,
            severity=3,
            summary=stub,
            sources=["get_pireps"],
            metadata={"lat": lat, "lon": lon, "count": n},
        )

    def _on_gairmet(self, event: Event) -> Iterable[Finding]:
        polygons = event.payload.get("advisories") or []
        severe = [g for g in polygons if (g.get("severity") or "").upper() in ("SEV", "MOD-SEV")]
        if not severe:
            return
        g = severe[0]
        stub = f"Severe-class turbulence advisory active FL{g.get('base')}-FL{g.get('top')}. {len(severe)} severe polygons in current set."
        yield Finding(
            specialist=self.name,
            severity=3,
            summary=stub,
            sources=["get_turbulence_advisories"],
            metadata={"count": len(severe)},
        )
