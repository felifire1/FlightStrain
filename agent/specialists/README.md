# `agent/specialists/` — Multi-agent ATC operations room

A constellation of specialized agents, each watching one slice of NAS data,
emitting findings onto a shared bus. The **Coordinator** is the only one that
speaks to the user.

Designed to be **kagent-shaped**: each specialist exposes a `Manifest` dict
that mirrors a kagent CRD spec (name, model, system_prompt, tool_refs). One
Python process today; one CRD per specialist on Kubernetes tomorrow, with no
code changes to the agents themselves.

---

## File layout

| File              | What it is                                                      |
|---                |---                                                              |
| `base.py`         | `Specialist` ABC, `Event`, `Finding`, `Manifest`                |
| `bus.py`          | In-process pub/sub with JSONL persistence                       |
| `weather.py`      | `WeatherAgent` — METAR/TAF/SIGMET/PIREP/G-AIRMET                |
| `traffic.py`      | `TrafficAgent` — ADS-B, conflict prediction, flow               |
| `safety.py`       | `SafetyAgent` — emergency squawks, anomalous trajectories       |
| `fleet.py`        | `FleetAgent` — per-airline operational picture                  |
| `narrator.py`     | `NarratorAgent` — rewrites findings for audience                |
| `coordinator.py`  | `Coordinator` — the agent that talks to the user                |

---

## Contract: how `agent/server.py` integrates

### 1. Construction (at server startup)

```python
from agent.specialists.bus import bus
from agent.specialists.coordinator import Coordinator
from agent.specialists.weather import WeatherAgent
from agent.specialists.traffic import TrafficAgent
from agent.specialists.safety import SafetyAgent
from agent.specialists.fleet import FleetAgent
from agent.specialists.narrator import NarratorAgent

specialists = [
    WeatherAgent(), TrafficAgent(), SafetyAgent(), FleetAgent(), NarratorAgent(),
]
coordinator = Coordinator(specialists=specialists)

# OPTIONAL: switch to LLM mode by injecting your existing Anthropic caller
def llm_call(system_prompt: str, user_msg: str, tools: list) -> str:
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")

for s in specialists + [coordinator]:
    s.inject_llm(llm_call)
```

### 2. Handle a chat message (`/api/chat`)

```python
@app.post("/api/chat")
async def chat(req: ChatRequest):
    result = coordinator.handle_user(req.message, history=req.history)
    return result  # already shaped like the existing response
```

Return shape (drop-in compatible with the existing chat panel):

```json
{
  "text": "...synthesized reply...",
  "history": [...],
  "tool_trace": [{"name": "get_metar", "args": {}}],
  "map_actions": [{"action": "fly_to", "lat": ..., "lon": ...}],
  "voices": [
    {"specialist": "weather", "severity": 4, "summary": "..."},
    {"specialist": "traffic", "severity": 3, "summary": "..."}
  ]
}
```

The new `voices` field is what enables multi-voice rendering in the chat panel
("WEATHER:" / "TRAFFIC:" / "SAFETY:" labels next to each contributing message).

### 3. Proactive push via SSE (`/api/alerts/stream`)

A long-running task on the server tails the bus and pushes high-severity
findings to subscribed clients:

```python
@app.get("/api/alerts/stream")
async def alerts():
    async def event_stream():
        for finding in bus.subscribe():  # blocks
            if coordinator.should_push(finding):
                yield f"data: {json.dumps(finding.chat_render())}\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

Frontend subscribes once at page load:

```javascript
new EventSource("/api/alerts/stream").onmessage = (e) => {
  const f = JSON.parse(e.data);
  addMsg("ai", f.text, { specialist: f.specialist, severity: f.severity });
  for (const a of (f.map_actions || [])) await executeMapAction(a);
};
```

### 4. Wiring the data watchers

The bus needs *somebody* to publish raw events. Two options:

**(a) Server-side watcher.** A FastAPI background task that polls the same
data the recorder fetches and publishes events to the bus:

```python
async def watcher():
    while True:
        states = await fetch_traffic()
        bus.publish(Event(type="traffic.snapshot", payload={"states": states}))
        await asyncio.sleep(30)
```

**(b) Read the recorder's output.** Cleaner — `scripts/record_overnight.py`
is already polling. We add a tailing task that watches `data/overnight/`
for new files and republishes as events. No duplicate API calls.

Either works; I'd recommend (b) since it doesn't double the OpenSky cost.

---

## Severity scale

| Sev | Meaning           | Example                                  | Pushed? |
|-----|-------------------|------------------------------------------|---------|
| 0   | informational     | "12 flights inbound JFK"                 | No      |
| 1   | notable           | "winds at FL340 picking up"              | No      |
| 2   | advisory          | "convective cell developing 60 nm w/ORD" | No      |
| 3   | significant       | "AAL1767 will enter MOD turb in 6 min"   | **Yes** |
| 4   | urgent            | "convective SIGMET issued for arrival"   | **Yes** |
| 5   | emergency         | "squawk 7700 from N123AB"                | **Yes** |

Push threshold is `Coordinator.PUSH_THRESHOLD` (currently 3).

---

## Migration to kagent

Each specialist's `manifest` dict maps directly to a kagent `Agent` CRD:

```yaml
apiVersion: kagent.dev/v1alpha1
kind: Agent
metadata:
  name: weather   # ← manifest.name
spec:
  description: # ← manifest.description
  modelConfig:
    providerRef: anthropic-sonnet  # ← manifest.model
  systemMessage: |
    # ← manifest.system_prompt
  tools:
    # ← manifest.tool_refs, each wrapped in a toolServer ref
```

The bus becomes a kagent event channel (or NATS, Redis Streams, etc.). The
Coordinator becomes its own Agent CRD that subscribes via inter-agent calls.

Estimated migration: 1–2 days, no rewrites of specialist logic.

---

## Cross-correlation (the Coordinator's killer feature)

The Coordinator doesn't just synthesize — it correlates signals across agents.
`Coordinator.correlate(traffic_states, hazard_findings)`:

1. For each high-severity weather finding with a polygon in its metadata
2. For each airborne aircraft in `traffic_states`
3. Project the aircraft's position 0/5/10/15/20/30 min ahead along its heading at reported velocity
4. Point-in-polygon test against the hazard
5. If transit predicted, mark affected with the earliest ETA
6. Emit a S4 Finding listing the top 5 affected flights with `recommended_action` + `map_actions`

The result reads like:

> ⚠ 9 flight(s) projected to transit NY NJ CT MA RI polygon in next 30 min:
> ABX3106@FL110 in 0min, DAL865@FL079 in 0min, CPA843@FL225 in 0min, … (+4 more)
> → Issue reroute advisory to listed flights. Vector north/south of polygon …

The `map_actions` are `{action: "load_layer", layer: "sigmet"}` followed by
`{action: "highlight_flight", icao24: ...}` for each affected aircraft. The
frontend's `executeMapAction` already knows how to run these.

This is run automatically in `dev_server.py`'s watcher loop after each tick.
For the 🎯 Demo Correlate button in the standalone UI, we drop a synthetic
SIGMET over the NE corridor (matching the recorder's bbox) to guarantee
correlation hits — useful for demos.

---

## Location-aware queries

`Coordinator.KNOWN_AREAS` maps city/airport names to (lat, lon, radius_deg):

```python
KNOWN_AREAS = {
    "boston":     (42.36, -71.01, 0.5),
    "bos":        (42.36, -71.01, 0.5),
    "new york":   (40.78, -73.87, 0.5),
    "jfk":        (40.64, -73.78, 0.4),
    # ... etc — BOS, NYC, JFK, LGA, EWR, ORD, ATL, DFW, DEN, LAX, SFO, etc.
}
```

The chat path checks if a known area name appears in the user message. If so:
- Via `dev_server.py`: live-fetches OpenSky for that bbox (~1 credit / call)
- Then `Coordinator._try_location_query` filters airborne aircraft within the radius
- Returns a clean table of callsign / FL / speed / heading + `fly_to` + `highlight_flight` map_actions for top 3

Tomorrow with LLM tokens, this static lookup is replaced with LLM parsing of
arbitrary city/airport mentions. The infrastructure is the same.

---

## Background watcher

`dev_server.py:_watcher_loop` polls every 30s (configurable):

1. Live-fetch G-AIRMET / SIGMET / PIREP from NOAA (always free, no key)
2. If a `FOCUS` is set, live-fetch OpenSky for that bbox; else use recorder snapshot
3. Fire events through every specialist whose `interests()` matches
4. Pull high-severity weather findings from the bus and run `Coordinator.correlate`

Cost: ~30 ticks/hr × ~5 NOAA calls each = 150 free calls + ~30 OpenSky calls
(if a focus is set) = trivial.

Bus dedup (120s window keyed on `(specialist, summary)`) keeps the same
finding from re-publishing on every tick.

---

## Stub vs LLM mode

Every specialist runs in one of two modes:

**stub** (default): deterministic Python templates. Zero token cost, zero
latency, identical output each time. Useful for development and as a
production fallback.

**llm**: calls Claude with the specialist's `system_prompt` + the event
payload. Reasoning quality much higher; cost ~$0.012 per call on Sonnet.

The flip is one method call. From `agent/server.py` at startup:

```python
def llm_call(system, user, tools):
    resp = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=512,
        system=system, messages=[{"role":"user","content":user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")

for s in specialists + [coordinator]:
    s.inject_llm(llm_call)
```

Specialists that benefit most from LLM mode:
- **Coordinator** — synthesis across signals is the biggest quality win
- **NarratorAgent** — audience rewriting is where prompts shine
- **WeatherAgent** — turning a SIGMET into a dispatcher-voice advisory

Specialists where stub is almost as good:
- **SafetyAgent** — emergencies are deterministic, just want fast/reliable output
- **TrafficAgent** baseline pulse, **FleetAgent** count summary — already terse
