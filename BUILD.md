# BUILD.md — Live build log & research

Engineering reality on *this* machine (felipequiroz worktree, branch `feature/research`).
Strategy/context lives in `CLAUDE.md`. This file is the running build log.

---

## Research Notes — 2026-05-30 10:55 EDT (T+1h40m after problem drop)

Written in planning/research mode (no code changes). Two questions answered below.

### (2) Has the problem statement dropped? — YES, but we don't have its text.

- It's **10:55 EDT**. Per CLAUDE.md the problem drops **9:15am EDT at the venue**, so
  it dropped **~1h40m ago**. Judging criteria were released alongside it at 9:15.
- **The actual problem text is NOT publicly available.** Web search returns only the
  event marketing blurb, not the prompt — hackathon problems drop in-room and aren't
  published. Confirmed across the event listings (Sero, Partiful, Crypto Nomads) and
  Boston Tech Week coverage. All they say is the generic framing: *"reinvent air
  traffic control; AI tokens provided; demos + cash prizes."*
- **Action required from a human in the room:** paste the real problem statement +
  judging criteria into this file. Everything below assumes problem-fit lands near the
  CLAUDE.md predicted framings (dispatcher copilot / agent-watching-airspace /
  disruption replan / weather-heavy). If the prompt is a hard left turn (passenger app,
  stadium-drone airspace), the recommendation in (1) still partially holds — the map +
  multi-agent chat is reusable — but budget ~30 min to re-skin.

Sources:
- [Hacking the Fourth Dimension with ASI at Fenway Park — Sero](https://se.ro/events/wexpU3KNPTj7wJEaPa8PD)
- [RSVP — Partiful](https://partiful.com/e/5dBDeFo8TOIjJftLaGPF)
- [Boston Tech Week 2026 — TechTimes](https://www.techtimes.com/articles/317151/20260525/boston-tech-week-2026-opens-tomorrow-a16z-brings-572-events-kendall-square.htm)

### State of the build on THIS machine (verified, not assumed)

What CLAUDE.md describes was built on *Vishal's* laptop. This worktree differs:

- ✅ **Code is all here** — `agent/`, `agent/specialists/` (5 agents + coordinator),
  `scripts/`, both frontends. Both servers are **already running** (`:8000` main,
  `:8765` specialists, since 10:29am).
- ✅ **Pre-baked CZMLs exist** in `data/samples/`: `traffic.czml` (**751 entities**),
  `decisions.czml` (**6 flagged flights, headline "N314RH 737-8 · 139.4 min MOD"**),
  plus `sigmets/pireps/gairmet/winds/airports`. **The map demo works standalone.**
- ❌ **`data/overnight/` does not exist; `data/cache/aircraft_db.csv` is missing; no
  `*.jsonl` anywhere.** The recorder is **NOT running** here.
- ❌ **`agent/auditor.py` reads `data/overnight/traffic/*.jsonl` + `.../weather/gairmet_*.json`**
  (auditor.py:21-22, 95, 151). Both empty → the `audit_recorded_flights` tool returns
  nothing. **So the scripted chat line "worst chop tonight" returns empty on this box.**
- ❌ **`agent/server.py` does NOT import the specialists.** It wires only
  `chat, chat_stream` from `agent/loop.py` (server.py:22). The cross-correlation
  "wow moment" — CLAUDE.md's documented demo backbone — lives **only** on the siloed
  `:8765` dev_server, disconnected from the main Cesium map.

Net: we have **two half-demos** — a gorgeous map (`:8000`) with a chat whose auditor is
data-starved, and a multi-agent ops room (`:8765`) with no map. The win is uniting them.

### (1) Single highest-impact thing for the next 2 hours

**Wire `Coordinator.handle_user` (with cross-correlation) into the main app's
`/api/chat`, so the "N flights will transit this hazard in 30 min" moment fires
*on the main Cesium map*.** This is CLAUDE.md's own connection plan, Steps 1–2.

Why this and not anything else:
1. **It's the documented wow moment AND it foregrounds the agent**, which judges weight
   (they handed out tokens). One screen now shows: live map → multi-voice agent reply →
   camera flies + flights highlight automatically. That's a product, not two demos.
2. **It routes around the missing-data showstopper.** `correlate()` runs on a *live*
   OpenSky bbox + a *synthetic* SIGMET (the 🎯 Demo Correlate path), so it does **not**
   depend on the empty `data/overnight/` JSONLs. It works on bad Fenway wifi because the
   hazard polygon is injected, not fetched.
3. **It's small and low-risk.** `Coordinator.handle_user(message, history, traffic_states)`
   already returns the drop-in shape `{text, history, tool_trace, map_actions, voices}`
   (coordinator.py:111-161), and the frontend's `executeMapAction` already handles every
   action it emits (`fly_to`, `highlight_flight`, `load_layer`, `set_time`, `set_speed`).
   Realistic effort: **30–45 min**, leaving buffer for polish + pitch practice.

**Concrete change set:**
- **File:** `agent/server.py`
  - At startup, build the constellation:
    `SPECS=[WeatherAgent(),TrafficAgent(),SafetyAgent(),FleetAgent(),NarratorAgent()]`,
    `COORD=Coordinator(specialists=SPECS)` (import from `agent.specialists.*`).
  - In `/api/chat` and `/api/chat/stream`, call **`COORD.handle_user(msg, history, traffic_states)`**
    instead of (or before falling back to) `chat()`. Return shape is already compatible.
  - **Function to call:** `agent/specialists/coordinator.py::handle_user` →
    internally `::correlate(traffic_states, hazard_findings)` (coordinator.py:245-322).
  - **Flip LLM mode on:** loop `for s in SPECS+[COORD]: s.inject_llm(llm_call)` using a
    Claude `messages.create` closure (Sonnet for specialists). Use **ASI's token at the door**.
  - **Data feeding correlate:** live OpenSky NE-corridor states via the existing authed
    client + the synthetic SIGMET from the Demo Correlate path; `KNOWN_AREAS["boston"]`
    (coordinator.py:177-204) anchors location queries. Map layers come from the
    already-baked `data/samples/*.czml`.

**Hard prerequisite / parallelizable must-fix (do FIRST or alongside):**
The scripted auditor line is data-starved on this machine. Pick one (10 min):
- **(a) Restart the recorder now** so ≥1h of fresh NE-corridor data exists by 5pm:
  `caffeinate -is .venv/bin/python scripts/record_overnight.py &`. (Also need
  `data/cache/aircraft_db.csv` for tail/operator enrichment, or accept ICAO-only labels.)
- **(b) Or add a fallback in `agent/auditor.py`** so an empty `data/overnight/` reads the
  pre-baked sample data, guaranteeing `audit_recorded_flights` returns the known headline.
- Either way, the **map already carries the number**: `decisions.czml` encodes
  "**139.4 min in MOD turbulence (N314RH, 737-8)**" — lead the close with that even if
  the live chat auditor is empty.

**Explicitly do NOT do in the next 2 hours:** new specialists, kagent/K8s, new map
layers, the SSE unprompted-alert stream (Steps 3–4). They're lower marginal value than
unifying the demo + locking the pitch. Per CLAUDE.md: no new features after hour 3.

**One-line demo after the change:** set LOCATION=boston → ask *"any flights heading into
weather near Boston?"* → multi-voice reply names real callsigns with ETAs, camera flies,
flights light up red on the main map → close on the 139-minute headline.

---

## PIREP → Turbulence Area  (Track 1 — DONE on feature/research, 2026-05-30 11:3x)

The wedge toward the 3D turbulence-volume north star. PIREP = human-validated dot;
this turns the dot into an *area*, then projects traffic through it.

**New file:** `agent/turbulence_area.py` (self-contained, no shared-file edits → zero
conflict with feature/B's `server.py`/`index.html` work).

- `pirep_to_hazard(pirep, advisories)` — **corroborate**: 3D + time point-in-polygon test
  of the PIREP against active G-AIRMET/SIGMET turbulence polygons. Match → "pilot-confirmed"
  Finding carrying that polygon. No match → **synthesize** a buffer-circle "inferred" area.
- `pireps_to_hazards(...)` — batch + de-dup confirmed areas sharing one advisory.
- Output `Finding.metadata["polygon"]` is `(lat, lon)` → feeds the **existing**
  `Coordinator.correlate()` untouched. `map_actions` reuse feature/B's `draw_polygon` +
  `highlight_flight` + `fly_to` verbs → renders with no frontend edits.

**Verified end-to-end against `data/samples/`:**
- 247 PIREPs + 52 advisories → 29 areas (1 confirmed, 28 inferred).
- Confirmed: *"CRJ9 reported MOD at FL180 inside an active TURB-HI polygon (MOD,
  FL180–FL300)."*
- Fed into `correlate()` with synthetic traffic → *"2 flights projected to transit hazard
  polygon in next 30 min: AAL892@FL219 in 0min, DAL412@FL229 in 0min."*
- Run it: `python -m agent.turbulence_area`

**Caveats baked in:** generous `time_slack_h` (cached sample advisories predate cached
PIREPs; live data aligns), `fl_buffer` for fuzzy vertical bands, inferred areas clearly
labelled speculative.

**Remaining to wire (small):** call `pireps_to_hazards` from the live PIREP path
(`WeatherAgent._on_pireps` or directly in the chat/alert loop) and pass current
`traffic_states` into `correlate()`. Backend logic + render contract are ready.

### Track 2 (other window) — GTG/EDR feasibility spike
Independent, no code overlap. Verify programmatic GTG/GTGN EDR grid access (NOMADS GRIB
vs tile service), pull one FL slice for the NE corridor, write up endpoint/format/cadence
+ a `(lat,lon,fl,edr,valid_time)` voxel schema under a "## GTG Feasibility" section.
Throwaway `scripts/probe_gtg.py`. This is rung 4 of the turbulence-volume ladder.
