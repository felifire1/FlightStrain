# Multi-Scenario ATC Agent Strategy

## Data Summary

**Per Scenario:**
- 14,712 flights (Phoenix to Boise, Seattle, LA corridor — Southwest US heavy)
- Waypoints: lat/lon/altitude trajectory per flight
- Cruise altitude: 11,100 ft typical
- Cruise speed: 400 kt typical
- Weather: 73 time-slices of refc (precipitation intensity) + retop (storm top altitude)
- Window: July 14, 2025 (and 10 other dates across 2025-2026)

**11 Scenarios = 162k flights** to analyze and learn patterns from

**Global:** Sectors GeoJSON (ATC sector boundaries with capacity)

---

## Agent Enhancement Opportunities

### Current Specialists (Already Built)
1. **Weather Agent** — reads G-AIRMET, PIREP, SIGMET
   - **Gap:** No wind routing optimization
   - **Gap:** Not using refc/retop (storm intensity/altitude)

2. **Traffic Agent** — detects loitering, descent fans
   - **Gap:** No conflict prediction
   - **Gap:** Not detecting congestion patterns

3. **Safety Agent** — emergency squawks, high descent rates
   - **Gap:** No stall/performance envelope checks
   - **Gap:** Not correlating with weather hazards

4. **Fleet Agent** — operator counts, holding pressure
   - **Gap:** No airline-specific routing rules
   - **Gap:** Not predicting cascade delays

5. **Narrator Agent** — rewrites findings
   - **Gap:** Already good, minimal changes needed

### What Open-Source Data We Can Add

**1. Wind Data (HRRR/GFS)**
```
→ U/V wind components at flight levels
→ Calculate optimal cruise altitude/routing
→ Detect jet stream opportunities
```
Source: NOAA HRRR (free, 15-min updates)

**2. Terrain & Airspace**
```
→ Minimum safe altitude (MSA) per sector
→ Restricted airspace (R-zones, MOAs)
→ Use existing sectors.geojson + FAA data
```
Source: FAA NOTAM API (free), OpenStreetMap elevation

**3. Aircraft Performance**
```
→ Climb/descent rate by aircraft type
→ Fuel burn vs altitude/speed
→ Stall speed, ceiling
→ Match flight_number → registration → type
```
Source: openap (free Python library)

**4. Historical Delay Patterns**
```
→ Each scenario is a "day"
→ Learn: "Summer afternoons see 40% more turbulence"
→ Learn: "Friday departures from LAX back up 2 hours"
→ Predict delays before they happen
```
Source: Your 11 scenarios themselves (built-in)

**5. Airline Rules**
```
→ Crew duty limits (affects routing)
→ Preferred airlines per airport
→ Gate capacity constraints
```
Source: Public airline schedules (FlightRadar24, public APIs)

---

## Proposed Multi-Scenario Agent Architecture

```
Live Incoming Flight Stream
    ↓
Multi-Scenario Coordinator
    ├→ Pattern Recognizer (learns from all 11 scenarios)
    │   ├ "July 14 pattern: 3pm turbulence spike in AZ"
    │   ├ "Summer vs Winter: 40% higher winds at FL350"
    │   └ "Southwest corridor: conflicts cluster near ABQ"
    │
    ├→ Wind Router (HRRR data)
    │   └ "Recommend FL380, +20kt tailwind, saves 15 min fuel"
    │
    ├→ Conflict Predictor (trajectory extrapolation)
    │   └ "SKW6242 & AAL1234 will be 2nm apart at FL310 in 8 min → recommend climb"
    │
    ├→ Delay Cascade Detector (fleet + weather correlation)
    │   └ "3 departures backing up at PHX → expect domino effect at SEA in 2 hours"
    │
    └→ Five Existing Specialists (weather, traffic, safety, fleet, narrator)

    ↓
Coordinator ATC Agent
    └ Synthesizes all inputs
    └ Decides severity (S0-S5)
    └ Raises alerts only on S3+ (significant/urgent)
    └ Streams to dispatcher
```

---

## Implementation Roadmap (3 hours)

**Phase 1: Data Load (20 min)**
- [ ] Copy all 11 scenarios into `/data/scenarios/`
- [ ] Index flights by timestamp for fast lookup
- [ ] Cache weather grids in memory (refc/retop)

**Phase 2: Pattern Recognizer (30 min)**
- [ ] Analyze all 11 scenarios → extract patterns
  - Time-of-day weather intensity
  - Corridor congestion patterns
  - Typical conflicts per sector
- [ ] Store as JSON lookup table

**Phase 3: New Agents (40 min)**
- [ ] Wind Router: fetch HRRR data, calculate optimal cruise
- [ ] Conflict Predictor: trajectory math + bounding box test
- [ ] Delay Cascade: flight chain analysis

**Phase 4: Wire & Test (30 min)**
- [ ] Integrate into Coordinator
- [ ] Test end-to-end on July 14 scenario
- [ ] Demo!

---

## Demo Narrative

**Old:** "Here's one night of data, here's what went wrong"

**New:** "Here are 11 days across a year. We found 3 decision types that recur every time:
1. Wind routing (saves 15 min fuel on avg)
2. Conflict avoidance (20% of delays preventable)
3. Cascade detection (predict domino failures 2h ahead)

Watch our agent make these decisions in real-time on July 14th — the worst day we have."

---

## Priority Order (What Matters Most)

1. **Pattern Recognizer** — Shows you learned from ALL data, not just one day
2. **Conflict Predictor** — Dispatcher-facing, actionable, real-time
3. **Wind Router** — Shows you optimize ops, not just alert
4. Delay Cascade — Nice-to-have, bonus

All 4 integrate into Coordinator seamlessly.
