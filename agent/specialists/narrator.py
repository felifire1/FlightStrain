"""NarratorAgent — translates findings into stakeholder-specific language.

Unlike the other specialists, the Narrator doesn't watch raw data. It listens
for `finding.published` events and rewrites them for a given audience:
    - "dispatcher"  : terse, technical (default)
    - "passenger"   : empathetic, plain English
    - "journalist"  : factual, explanatory
    - "regulator"   : formal, compliance-oriented

This is what unlocks the multi-stakeholder demo angle ("the same model serves
the dispatcher, the passenger, and the regulator").

The dispatcher chat panel pulls the dispatcher version. A separate "passenger"
view could pull the passenger version for the same finding.
"""
from __future__ import annotations
from typing import Any, Iterable
from .base import Specialist, Manifest, Event, Finding


PERSONA_HINTS = {
    "dispatcher": "Terse, technical, FAA-style. ICAO codes, FLs, knots.",
    "passenger": "Empathetic, plain English. No jargon. Acknowledge inconvenience.",
    "journalist": "Factual, explanatory. Provide the *why*. Avoid speculation.",
    "regulator": "Formal, compliance-focused. Cite regulations or advisories if relevant.",
}


class NarratorAgent(Specialist):
    manifest = Manifest(
        name="narrator",
        description="Rewrites operational findings into language tailored to a specified audience (dispatcher, passenger, journalist, regulator).",
        model="claude-sonnet-4-6",
        system_prompt=(
            "You are the NarratorAgent. You translate operational findings into "
            "language for a specific audience. Never invent facts. Never sanitize "
            "severity. Keep length proportional to severity: 1-2 sentences for "
            "advisory, a short paragraph for urgent."
        ),
        tool_refs=[],
        interests=["finding.published"],
    )

    def formulate(self, event: Event, context: dict[str, Any] | None = None) -> Iterable[Finding]:
        if event.type != "finding.published":
            return
        original = event.payload.get("finding")
        audience = (context or {}).get("audience", "dispatcher")
        if not original:
            return
        # The "finding" payload is a dict (Finding.to_dict())
        source_text = original.get("summary", "")
        sev = original.get("severity", 0)
        hint = PERSONA_HINTS.get(audience, PERSONA_HINTS["dispatcher"])

        # Stub: lightly remap based on audience
        if audience == "passenger":
            stub = self._passenger_rewrite(source_text, sev)
        elif audience == "journalist":
            stub = self._journalist_rewrite(source_text)
        elif audience == "regulator":
            stub = self._regulator_rewrite(source_text)
        else:
            stub = source_text  # dispatcher already gets the original

        llm_prompt = f"Audience: {audience}. {hint}\n\nOriginal finding (severity {sev}): {source_text}\n\nRewrite for this audience."
        text = self._stub_or_llm(stub, llm_prompt)

        yield Finding(
            specialist=f"narrator.{audience}",
            severity=max(0, sev - 1),  # narrations are notifications, not alarms
            summary=text,
            sources=[original.get("specialist", "")],
            metadata={"audience": audience, "from_specialist": original.get("specialist")},
        )

    # --- stub rewriters (deterministic, audience-shaped templates) ---

    def _passenger_rewrite(self, text: str, sev: int) -> str:
        if sev >= 4:
            return f"We're tracking a significant operational issue and are working with air traffic control to keep everyone safe. {text}"
        return f"There may be a brief delay or rerouting for some flights. {text}"

    def _journalist_rewrite(self, text: str) -> str:
        return f"Operational note: {text} This kind of advisory is routine in active weather and resolves as conditions evolve."

    def _regulator_rewrite(self, text: str) -> str:
        return f"Advisory issued per standard procedure. {text} Status to be reviewed at next coordination cycle."
