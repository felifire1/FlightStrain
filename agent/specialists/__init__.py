"""Multi-agent ATC operations room.

A constellation of specialized agents, each watching one slice of the NAS data,
emitting findings onto a shared bus. The Coordinator is the only one that
speaks to the user — it subscribes to the bus and synthesizes.

This module is kagent-shaped on purpose: each specialist exposes a `manifest`
dict that mirrors a kagent CRD spec (name, model, system_prompt, tool_refs).
Tonight we run them in one Python process; tomorrow they could deploy
unchanged as separate kagent CRDs on Kubernetes.

Layout:
    base.py         — Specialist ABC, Finding/Event types
    bus.py          — pub/sub message bus
    weather.py      — WeatherAgent
    traffic.py      — TrafficAgent
    safety.py       — SafetyAgent
    fleet.py        — FleetAgent
    narrator.py     — NarratorAgent
    coordinator.py  — synthesizer that talks to the chat panel

Contract with agent/server.py:

    from agent.specialists.bus import bus
    from agent.specialists.coordinator import Coordinator

    coord = Coordinator()
    # On user message:
    reply = coord.handle_user(message, history)
    # On chat startup, subscribe for proactive pushes:
    for finding in bus.subscribe():
        if finding.severity >= 3:
            push_to_chat(finding)

See README.md for the full contract and the kagent migration path.
"""
