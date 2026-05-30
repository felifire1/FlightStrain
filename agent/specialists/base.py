"""Base types for specialists, events, and findings.

Three dataclasses + one ABC. Keep them small and JSON-serializable so the
same shapes can travel over the chat WebSocket, the JSONL log, and (later)
between kagent pods on the message bus.

Severity scale (mirrors NWS/SIGMET conventions, lightly adapted):
    0  informational         "12 flights inbound JFK"
    1  notable               "winds at FL340 picking up"
    2  advisory              "convective cell developing 60 nm west of ORD"
    3  significant           "AAL1767 will enter MOD turbulence in 6 min"
    4  urgent                "convective SIGMET issued for arrival corridor"
    5  emergency             "squawk 7700 from N123AB, descending rapidly"

Each Finding carries `map_actions` — same shape the chat dispatcher already
understands (see frontend executeMapAction). This lets a specialist's
recommendation literally drive the camera or load a layer.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable
import time


Severity = int  # 0..5, see module docstring


@dataclass
class Event:
    """Something the system noticed that *may* warrant a specialist's attention.

    Examples:
        Event(type="sigmet.issued", payload={"hazard": "CONVECTIVE", ...})
        Event(type="traffic.snapshot", payload={"states": [...], ...})
        Event(type="pirep.received", payload={...})
        Event(type="user.question", payload={"text": "any chop near BOS?"})

    Events are routed to specialists whose `interests()` matches `type`. A
    specialist may emit zero or more Findings in response.
    """
    type: str
    payload: dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    source: str = "system"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Finding:
    """A specialist's observation, opinion, or recommendation.

    Findings are the *output* of a specialist and the *input* to the Coordinator.
    They render in chat as a voiced message ("WEATHER:", "TRAFFIC:", etc.)
    when severity >= 3, or are silently accumulated for synthesis.
    """
    specialist: str
    severity: Severity
    summary: str                            # plain-English headline (one sentence)
    detail: str = ""                        # optional longer-form
    recommended_action: str | None = None   # what to do, if anything
    map_actions: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # data points referenced
    timestamp: float = field(default_factory=time.time)
    # Free-form metadata for downstream rendering (icao24, station, etc.)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def chat_render(self) -> dict[str, Any]:
        """Serialize for the chat panel. Mirrors the shape of /api/chat
        responses (text + tool_trace + map_actions)."""
        return {
            "specialist": self.specialist,
            "severity": self.severity,
            "text": self.summary + (f"\n\n{self.detail}" if self.detail else "") +
                    (f"\n\nRecommended: {self.recommended_action}" if self.recommended_action else ""),
            "map_actions": self.map_actions,
            "tool_trace": [{"name": s, "args": {}} for s in self.sources],
            "timestamp": self.timestamp,
        }


@dataclass
class Manifest:
    """Declarative metadata about a specialist. Tonight: read by Python.
    Tomorrow: a kagent CRD with the same fields.

    Mapping to kagent CRD (apiVersion: kagent.dev/v1alpha1, kind: Agent):
        name           -> metadata.name
        description    -> spec.description
        model          -> spec.modelConfig.providerRef
        system_prompt  -> spec.systemMessage
        tool_refs      -> spec.tools[].toolServer.name + tool[].name
        interests      -> custom annotation (event-routing config)
    """
    name: str
    description: str
    model: str                              # e.g. "claude-sonnet-4-6"
    system_prompt: str
    tool_refs: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Specialist(ABC):
    """Base class for a specialist agent.

    Subclasses declare a `manifest` and implement `formulate(event)`. Mode
    can be `"stub"` (deterministic templates, tonight) or `"llm"` (real
    Claude call, tomorrow — wired by the other session via inject_llm()).
    """

    # Override in subclass
    manifest: Manifest

    def __init__(self, mode: str = "stub"):
        self.mode = mode  # "stub" | "llm"
        self._llm = None  # injected later

    @property
    def name(self) -> str:
        return self.manifest.name

    def interests(self) -> tuple[str, ...]:
        return tuple(self.manifest.interests)

    def inject_llm(self, llm_fn) -> None:
        """Wire in a real LLM caller. Signature:
            llm_fn(system_prompt: str, user_msg: str, tools: list) -> str
        The other session does this from agent/server.py with the existing
        Anthropic client and tool dispatcher."""
        self._llm = llm_fn
        self.mode = "llm"

    @abstractmethod
    def formulate(self, event: Event, context: dict[str, Any] | None = None) -> Iterable[Finding]:
        """Given an event, produce zero or more findings.

        For mode='stub': use deterministic heuristics + templates.
        For mode='llm': construct a prompt, call self._llm, parse, emit.
        """
        raise NotImplementedError

    # --- helpers usable by all specialists ---

    def _stub_or_llm(self, stub_text: str, llm_prompt: str | None = None) -> str:
        """Return the stub text now; tomorrow the same call returns LLM output."""
        if self.mode == "llm" and self._llm and llm_prompt:
            try:
                return self._llm(self.manifest.system_prompt, llm_prompt, [])
            except Exception:
                return stub_text  # graceful fallback
        return stub_text
