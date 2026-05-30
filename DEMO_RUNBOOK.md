# DEMO RUNBOOK — how to run everything (5pm demo day)

All commands assume the main checkout with **latest `main`** and the venv at
`/Users/felipequiroz/Desktop/FlightStrain/.venv`. The data bundle lives at
`~/Downloads/hackathon_data_bundle`. Run from the repo root.

```bash
cd /Users/felipequiroz/Desktop/FlightStrain
git checkout main && git pull origin main      # ensure impact/avoidance/demo_inject are present
PY=.venv/bin/python ; UVI=.venv/bin/uvicorn
```

---

## 0. Sanity check (do this first)
```bash
curl -s http://127.0.0.1:8000/api/health | $PY -m json.tool
```
Expect `"mode":"llm"`, 8 specialists incl. `pattern_analyst/conflict_predictor/wind_router`,
`"watcher_running":true`. If not → you're on old code; restart the server (Step 1).

---

## 1. Main app — the live demo (Cesium 4D map + conversational agent + proactive alerts)
```bash
$UVI agent.server:app --host 127.0.0.1 --port 8000
#   faster/cheaper LLM:  CLAUDE_MODEL=claude-haiku-4-5-20251001 $UVI agent.server:app --port 8000
#   convective storms on: WX_ASKED_AT=2025-08-22T18:00:00Z $UVI agent.server:app --port 8000
```
→ open **http://localhost:8000/**. Map loads; chat panel bottom-right; high-severity alerts
stream in unprompted. Cold start: the first watcher tick lags a few seconds — **warm it before
judges arrive** (load the page, ask one question).

**On screen:** ask *"any flights heading into weather?"* → answer + camera flies + flights highlight.

## 2. demo_inject — the visual WOW ("always works" trigger)
In a second terminal (server still running):
```bash
$PY scripts/demo_inject.py            # injects a synthetic MOD PIREP inside a real G-AIRMET polygon
                                      # → bus → chat alert + map: "N flights projected to transit"
```
Pre-verify the number you'll see (deterministic against cached traffic):
```bash
$PY scripts/demo_inject.py --dry-run --json | $PY -c "import json,sys;print(json.load(sys.stdin)['transit_summary'])"
```
⚠️ Today this prints **"445 flights … (cached traffic.czml)"**. Use it as a **visual alarm** — let
the map show it; **don't quote 445 as the headline** (it's ~445 of ~750 cached low-alt flights, a
weak number under scrutiny). Land on the avoidance number instead ↓.

## 3. Avoidance report — THE HEADLINE NUMBER (deterministic, offline, bulletproof)
```bash
$PY scripts/avoidance_report.py --asked-at 2025-08-22T18:00:00Z
#   → writes data/reports/turbulence_avoidance_2025-08-22T180000Z.md
```
This is your defensible number. Open the .md, keep it on screen / on paper.

## 4. (optional) Specialists console — multi-agent dev view on :8765
```bash
$UVI agent.specialists.dev_server:app --host 127.0.0.1 --port 8765   # → http://127.0.0.1:8765/
```

## 5. (optional) Avoidance route viz — original vs avoided on the map
```bash
$PY scripts/avoidance_to_czml.py      # → data/samples/avoidance.czml
# load on the map via browser console:
#   Cesium.CzmlDataSource.load('/data/samples/avoidance.czml').then(ds=>{viewer.dataSources.add(ds);viewer.flyTo(ds);})
```

## 6. Backup video (DO THIS — wifi will be bad)
Record a clean run of Steps 1–3 with QuickTime (File → New Screen Recording). Keep it cued
fullscreen in a browser tab. If anything flakes on stage: *"let me show you the recorded run."*

---

## Verified numbers cheat-sheet (say these; they're real)
| Claim | Value | Source |
|---|---|---|
| Turbulence = leading cause of Part 121 accidents | **36%** (152/420, 2008–2022) | NTSB SS2101 |
| Cost to US airlines | **$150–500M/yr** | Univ. of Reading / FAA |
| Flight attendants vs passengers injury risk | **24×** | NTSB |
| ASI Flyways fuel saved (Alaska, 2023) | **1.2M gal** | Alaska/ASI |
| **Flights into convective wx (scenario 2025-08-22)** | **85** | `avoidance_report.py` |
| **4D avoidance fuel cut vs 2D reroute** | **~64%** (6,786→2,449 gal) | `avoidance_report.py` |
| 4D net total-cost cut (fuel traded for delay) | **~18%** ($28.3k vs $34.6k) | `avoidance_report.py` |
| Passengers protected | **~13,500** | `avoidance_report.py` |
| Maneuver mix | **35 wait / 38 lateral / 12 climb** | `avoidance_report.py` |

**Re-verify on the day:** re-run Steps 2 & 3 once during setup so the numbers on your tongue match
the screen. The avoidance numbers are deterministic; demo_inject's count depends on loaded traffic.

---

## How this is different from ASI (the question you WILL get)
- **Conversational, not a dashboard.** Flyways is a route-optimization product a dispatcher *reads*;
  ours is *talked to* and **speaks first** when something's urgent (multi-agent + proactive alerts).
- **A 4D insight, not just a reroute.** Optimizers avoid weather in 2D — go around. We model the
  storm as a 4D volume (width · height · **time**) and show most flights should **climb over or wait**,
  not divert — **64% less fuel.** That reframing is the creative/insight core.
- **We own the decision, not the forecast.** We explicitly *don't* reinvent the science — NOAA's GTGN
  already fuses EDR + PIREPs + radar. We sit on top: per-flight, conversational, actionable.
- **Built on their data.** Straight off the challenge bundle — a day of US flight plans + HRRR weather
  + airspace — directly answering "make the system better."

**Maps to the judging criteria:** Approach (multi-agent + 4D correlate + economics) · Insight (time as
the escape route) · Communication (you talk to it; clean report) · Creativity (talk-to-the-airspace + wait-it-out).
