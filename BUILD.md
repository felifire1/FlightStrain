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

## Industry Landscape — turbulence prediction (pitch research, 2026-05-30)

Researched for pitch positioning. **The key strategic finding is the reframe in bold below.**

**Four tiers of what's already out there:**
1. **Forecast/nowcast (gov gold standard):** NOAA/NCAR **GTG** (EDR forecast, sfc→FL450, hourly to 18h) and **GTGN** — nowcast refreshed **every 15 min** that **already fuses in-situ EDR + PIREPs + radar (NTDA) + satellite with the forecast**.
2. **Crowdsourced obs:** **IATA Turbulence Aware** (28 airlines, 2,800 aircraft, ~25M EDR reports H1 2025, redistributed in seconds); **SkyPath** (software-only, 500M reports/day, 12h AI nowcast, auto-PIREPs); Weather Company **Total Turbulence**.
3. **Onboard:** EDR auto-reporting (Airbus/Honeywell), predictive weather radar.
4. **Decision support / optimization:** **ASI Flyways** — route optimization over weather/winds/turbulence/traffic, 8h+ lookahead, dispatcher-in-the-loop, saved Alaska **1.2M gal in 2023**.

**REFRAME (do not get this wrong in the pitch):** "PIREP + forecast fusion" is NOT novel — that is exactly what GTGN does, better. Do not claim the science. The white space is the **decision/communication last mile**: industry's own words — *"flight crews, controllers, and dispatchers do not always access the same information"*; dominant dispatch tactic is still crude (*"file lower or file normal with extra gas"*). No one ships an agent the dispatcher *talks to* that auto-correlates the turbulence field against *their specific live fleet* and returns per-flight action + drives the map.

**Positioning:** Don't say "we predict turbulence." Say "we close the last mile — the operator's conversational copilot that turns any turbulence field into per-flight action." PIREP = our wedge + validation primitive, not our prediction engine. Roadmap line: *"Today we anchor on PIREPs (pilot ground truth); the same engine ingests IATA Turbulence Aware EDR and NOAA GTGN as drop-in sources"* (= rungs 4–5 of the turbulence-volume ladder). Differentiators vs GTGN/SkyPath (which are map/alert products): conversational multi-agent, automatic fleet correlation with ETAs, multi-voice, agent-driven map.

**Headline numbers (closing slide):** turbulence = leading cause of Part 121 accidents (152/420 = 36%, 2008–2022); costs US airlines $150–500M/yr (Univ. Reading); flight attendants 24× injury risk; climate worsening CAT; Flyways saved Alaska 1.2M gal (2023).

**Sources:** [GTGN (NCAR RAL)](https://ral.ucar.edu/solutions/products/graphical-turbulence-guidance-nowcast-gtgn), [IATA Turbulence Aware](https://www.iata.org/en/services/data/safety/turbulence-platform/), [SkyPath](https://skypath.io/), [Weather Company Total Turbulence](https://www.weathercompany.com/wp-content/uploads/2024/01/Total-Turbulence-solution-sheet-FINAL.pdf), [NTSB turbulence safety study SS2101](https://www.ntsb.gov/safety/safety-studies/Documents/SS2101.pdf), [Alaska/ASI Flyways](https://news.alaskaair.com/sustainability/how-ai-is-helping-alaska-airlines-plan-better-flight-routes-and-lower-emissions/).

### Track 2 (other window) — GTG/EDR feasibility spike
Independent, no code overlap. Verify programmatic GTG/GTGN EDR grid access (NOMADS GRIB
vs tile service), pull one FL slice for the NE corridor, write up endpoint/format/cadence
+ a `(lat,lon,fl,edr,valid_time)` voxel schema under a "## GTG Feasibility" section.
Throwaway `scripts/probe_gtg.py`. This is rung 4 of the turbulence-volume ladder.

---

## Coordinator Handoff — 2026-05-30 ~11:15 EDT (from Felipe integration window)

Context dump from the coordinator window so this tab can run a recap off the live
tickets. **main is shared with Vishal; Felipe is the integration/staging branch.**

### What's on `main` right now (`c35e697`)
Three things landed and are live + import-verified:
1. **`9836644`** — city-aware traffic resolution in `/api/chat` (named city → live OpenSky bbox; else cached NE snapshot). *(Felipe / ex-Worker A, reconciled to preserve Vishal's PR#1.)*
2. **`c85d318`** — proactive alerts: `GET /api/alerts/stream` (SSE) + 30s background watcher + frontend subscribe (passive map actions only). *(Felipe / ex-Worker B, reconciled onto LLM-mode specialists.)*
3. **`c35e697`** — Vishal's PR#2: **3 new agents** + `ARCHITECTURE.md` + `STRATEGY.md`. Server now runs **8 specialists**.

### Vishal's 3 new agents (PatternAnalyst / ConflictPredictor / WindRouter)
- **PatternAnalyst** — RandomForest conflict-risk ML (loads `data/models/conflict_model.pkl`).
- **ConflictPredictor** — pairwise trajectory extrapolation, 5nm/1000ft separation geometry.
- **WindRouter** — altitude/fuel optimization from seasonal wind lookup tables.

### ⚠ VERIFIED: the new engines are DORMANT (only emit canned chat lines)
- All three declare `interests=["user.question"]` only → **the watcher never fires them** (it emits `traffic.snapshot`).
- `ConflictPredictor.predict_conflicts()` is **never called anywhere** → `active_conflicts` always empty → always answers "no separation violations." Real geometry never runs.
- `predict_risk()` / `analyze_routing()` need a `flight` payload **nothing feeds**; `data/models/conflict_model.pkl` **does not exist** → `predict_risk` returns flat 0.5.
- **Schema mismatch**: new agents want `lats[]/lons[]/cruise_altitude_ft/flight_number`; live OpenSky gives single-point `_STATE_FIELDS`. The two data worlds don't connect.

### 🔑 The strategic pivot this exposes (needs a human decision)
`STRATEGY.md`/`ARCHITECTURE.md` describe an **event-provided dataset we did NOT build for**:
**11 scenarios / 162k flights, Southwest-US corridors** (Phoenix→Boise/Seattle/LA),
`refc`/`retop` weather grids, ATC-sector GeoJSON w/ capacity. Our whole pipeline
(auditor, recorder, traffic resolution, watcher, your turbulence-area work) is **live
OpenSky Boston/NE-corridor**. **`data/scenarios/` does not exist on disk.** Open question
that gates the rest of the build: *is the demo supposed to run on the provided scenario
dataset or on our live BOS data?*

### Live ticket board (mirrors coordinator BUILD.md)
| ID | Owner | Status | Note |
|----|-------|--------|------|
| T1 | Worker A | ✅ done | multi-agent into `/api/chat` + LLM mode — main `c35e697` |
| T2 | Worker B | ✅ done | SSE alerts + watcher — main `c85d318` |
| T3 | Research | ⚠ partial / **BLOCKING** | problem dropped ~9:15 EDT but **text still not captured** — needs a human in the room |
| T4 | Research | open | **get real problem text + confirm the SW-US scenario dataset** (did we receive it? where? demo on it or live BOS?) — unblocks everything |
| T5 | Worker A | open | make the 3 agents actually fire (fix `interests`, watcher calls `predict_conflicts()` / feeds per-flight events) |
| T6 | Worker B | open | one schema adapter so the same traffic feeds both worlds |
| T7 | Worker A | open | produce or stub `conflict_model.pkl` so PatternAnalyst stops returning flat 0.5 |

**Ask for this tab's recap:** synthesize the above against your existing Research Notes
(esp. the turbulence-area Track 1 + the "last-mile" pitch reframe) and tell us: given the
data-world question in T4, which demo path do we double down on, and what's the minimum
wiring (T5–T7) that makes Vishal's agents real for the pitch rather than canned.
