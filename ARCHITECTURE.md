# FlightStrain ATC Agent Architecture

## Data Flow (End-to-End)

```
┌─────────────────────────────────────────────────────────┐
│ OFFLINE PHASE (Training)                                │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  11 Scenarios (162k flights)                           │
│    ↓                                                    │
│  pattern_model.py                                      │
│    • Load flights from JSON                            │
│    • Detect conflicts (haversine distance)             │
│    • Extract 9 features per flight                     │
│    • Train RandomForest classifier                     │
│    ↓                                                    │
│  data/models/conflict_model.pkl                        │
│  data/models/scaler.pkl                                │
│                                                         │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ RUNTIME PHASE (Demo)                                    │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  Live Flight Stream (July 14 scenario)                 │
│    ↓                                                    │
│  Agent Server (:8000)                                  │
│    ↓                                                    │
│  ┌──────────────────────────────────────────────────┐  │
│  │ COORDINATOR ATC AGENT                            │  │
│  │                                                  │  │
│  │ For each incoming flight:                        │  │
│  │                                                  │  │
│  │  1. Extract ML Features (9 values)               │  │
│  │     • cruise_alt, cruise_speed, hour, month      │  │
│  │     • lat/lon position, distance traveled        │  │
│  │                                                  │  │
│  │  2. Load Model                                   │  │
│  │     conflict_model.pkl → predict(features)      │  │
│  │     → conflict_risk (0.0-1.0)                    │  │
│  │                                                  │  │
│  │  3. Query Sub-Agents (5 specialists)             │  │
│  │     ├─ Weather Agent                             │  │
│  │     │   "Is there turbulence near this alt?"     │  │
│  │     │                                            │  │
│  │     ├─ Traffic Agent                             │  │
│  │     │   "Other aircraft nearby?"                 │  │
│  │     │                                            │  │
│  │     ├─ Pattern Analyst Agent (NEW)               │  │
│  │     │   conflict_risk + scenario_patterns        │  │
│  │     │   "This looks like July 14 at 2pm"         │  │
│  │     │                                            │  │
│  │     ├─ Conflict Predictor Agent (NEW)            │  │
│  │     │   Geometric trajectory extrapolation       │  │
│  │     │   "Will violate 3nm separation in 8min"    │  │
│  │     │                                            │  │
│  │     ├─ Wind Router Agent (NEW)                   │  │
│  │     │   HRRR wind data                           │  │
│  │     │   "Climb to FL380 for +20kt tailwind"      │  │
│  │     │                                            │  │
│  │     ├─ Fleet Agent                               │  │
│  │     │   "AAL at capacity, suggest DAL route"     │  │
│  │     │                                            │  │
│  │     ├─ Safety Agent                              │  │
│  │     │   "No emergency squawks nearby"            │  │
│  │     │                                            │  │
│  │     └─ Narrator Agent                            │  │
│  │        "Rewrite findings for dispatcher"         │  │
│  │                                                  │  │
│  │  4. Synthesize Findings                          │  │
│  │     Only surface S3+ (significant/urgent)        │  │
│  │                                                  │  │
│  │  5. Output                                       │  │
│  │     • Alert to dispatcher (if S3+)               │  │
│  │     • Map actions (highlight flight, etc.)       │  │
│  │     • Chat response (streaming)                  │  │
│  │                                                  │  │
│  └──────────────────────────────────────────────────┘  │
│    ↓                                                    │
│  Frontend (Cesium 4D Map + Chat)                       │
│    • Shows flight trajectory                           │
│    • Displays agent recommendations                    │
│    • Streams chat responses                            │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

---

## File Structure (New)

```
FlightStrain/
├── agent/
│   ├── pattern_model.py           ← Train ML model
│   ├── specialists/
│   │   ├── base.py                (existing)
│   │   ├── coordinator.py          (existing, update to call model)
│   │   ├── weather.py              (existing)
│   │   ├── traffic.py              (existing)
│   │   ├── safety.py               (existing)
│   │   ├── fleet.py                (existing)
│   │   ├── narrator.py             (existing)
│   │   ├── pattern_analyst.py      ← NEW (uses ML model)
│   │   ├── conflict_predictor.py   ← NEW (geometry-based)
│   │   └── wind_router.py          ← NEW (HRRR data)
│   └── server.py                  (update to use specialists)
│
├── data/
│   ├── scenarios/                  (all 11 scenarios)
│   │   ├── asked_at_2025-05-29.../
│   │   ├── asked_at_2025-07-14.../
│   │   └── ...
│   └── models/                     ← NEW
│       ├── conflict_model.pkl      (trained RandomForest)
│       └── scaler.pkl              (StandardScaler)
│
├── STRATEGY.md                     (analysis)
└── ARCHITECTURE.md                 (this file)
```

---

## How Model is Used (Code)

### 1. Training (Offline)

```bash
cd ~/Documents/FlightStrain
.venv/bin/python agent/pattern_model.py
# → saves conflict_model.pkl + scaler.pkl
```

### 2. Prediction (Runtime)

```python
# In coordinator.py or pattern_analyst.py

import pickle
import numpy as np

class PatternAnalystAgent(Specialist):
    def __init__(self):
        with open("data/models/conflict_model.pkl", "rb") as f:
            self.model, self.scaler = pickle.load(f)
        self.scenario_patterns = self.load_patterns()  # From analysis
    
    def predict_risk(self, flight):
        """Extract features and get conflict risk."""
        features = extract_features(flight)  # 9 values
        features_scaled = self.scaler.transform([features])
        risk_score = self.model.predict_proba(features_scaled)[0][1]
        return risk_score
    
    def formulate(self, mode="llm"):
        """Decide what to tell dispatcher."""
        # Called by Coordinator for each incoming flight
        
        if mode == "stub":
            return f"Conflict risk: {self.risk_score:.0%}"
        
        if mode == "llm":
            # Use Claude
            system = f"""
You are a pattern recognition expert who studied 11 days of airspace.
Here's what you learned: {self.scenario_patterns}

Flight: {flight_data}
ML Model says: Conflict risk is {self.risk_score:.0%}

What pattern does this match? What should the dispatcher do?
"""
            return self.llm_call(system, user="Analyze this flight.")
```

---

## The Three New Agents Explained

### Pattern Analyst Agent
- **Input:** ML model's conflict risk + flight data
- **Output:** "This looks like July 14 2pm pattern → high conflict risk"
- **Data:** scenario_patterns.json (extracted offline)

### Conflict Predictor Agent
- **Input:** Two flights, their trajectories
- **Output:** "Will violate 3nm separation in 8 minutes"
- **Data:** Pure geometry (no ML needed)

### Wind Router Agent
- **Input:** Flight cruise altitude, path
- **Output:** "Climb to FL380 for +20kt tailwind, saves 300 gal"
- **Data:** HRRR wind grid (fetched live or cached)

---

## Sequence (Single Flight)

```
1. Flight arrives: SKW6242, PHX→BOI, FL310, 2:45pm

2. Coordinator extracts features:
   [0.69, 0.80, 0.114, 0.583, 0.857, ...]
   
3. Coordinator loads model & predicts:
   conflict_risk = 0.68 (68% risk)
   
4. Coordinator queries sub-agents in parallel:
   
   a) Pattern Analyst:
      "68% risk + hour=14 + month=7 → matches July 14 pattern"
      Severity: S3 (significant)
   
   b) Conflict Predictor:
      "Found AAL1234 at FL320, 5nm away"
      "Will be 2.8nm apart in 8min → violation"
      Severity: S4 (urgent)
   
   c) Wind Router:
      "HRRR shows +25kt tailwind at FL350"
      "Recommend climb for fuel savings"
      Severity: S1 (advisory)
   
   d) Weather Agent:
      "G-AIRMET shows moderate turb FL300-320"
      Severity: S2 (advisory)
   
   e) Others (traffic, safety, fleet, narrator)

5. Coordinator synthesizes:
   Max severity = S4 (urgent)
   
6. Output to dispatcher:
   ALERT: "Conflict developing with AAL1234
   Recommend immediate climb to FL350
   (Also: +25kt tailwind, saves fuel)"
   
7. Map actions:
   - Highlight both flights
   - Show separation vector
   - Show recommended flight path
```

---

## Timeline (4 hours)

| Time | Task | File |
|------|------|------|
| 0:00-0:20 | Train ML model | pattern_model.py |
| 0:20-0:40 | Build Pattern Analyst | specialists/pattern_analyst.py |
| 0:40-1:00 | Build Conflict Predictor | specialists/conflict_predictor.py |
| 1:00-1:20 | Build Wind Router | specialists/wind_router.py |
| 1:20-1:40 | Wire into Coordinator | coordinator.py |
| 1:40-2:00 | Test end-to-end | |
| 2:00-3:15 | Load July 14, practice demo | |
| 3:15-4:00 | Buffer + final polish | |

---

## Key Questions

**Q: What if model is bad?**
A: Model is just a risk scorer. Pattern Analyst uses it + Claude reasoning. Even 60% accuracy is useful.

**Q: Will it run fast enough?**
A: Model prediction is <1ms per flight. All agents run in parallel (async).

**Q: How do we demo it?**
A: Chat: "What conflicts are developing?" → Coordinator queries all agents → streams response with findings.
