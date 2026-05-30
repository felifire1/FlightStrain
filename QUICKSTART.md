# FlightStrain — ASI Hackathon 2026

Multi-agent flight-tracking app with Claude, Cesium 4D visualization, and real ADS-B data.

## Quick Start (15 min)

### 1. Setup

```bash
python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### 2. Credentials

**Claude API key (secure local file):**
```bash
mkdir -p ~/.claude
echo "your-api-key-here" > ~/.claude/api_key
chmod 600 ~/.claude/api_key
```

**Other credentials (in .env):**
```bash
cp .env.example .env
# edit .env with:
#   OPENSKY_CLIENT_ID=<from opensky-network.org>
#   OPENSKY_CLIENT_SECRET=<same>
#   OPENSKY_USERNAME=<opensky login>
#   OPENSKY_PASSWORD=<opensky password>

cp frontend/config.example.js frontend/config.js
# edit with your Cesium Ion token
```

Note: The app loads the Claude API key from `~/.claude/api_key` (recommended for security), with fallback to `ANTHROPIC_API_KEY` env var.

### 3. Start Services

**Terminal 1 — Recorder** (captures fresh NE corridor traffic):
```bash
caffeinate -is .venv/bin/python scripts/record_overnight.py > /tmp/recorder.log 2>&1 &
```

**Terminal 2 — Main dashboard** (:8000, Cesium + chat):
```bash
CLAUDE_MODEL=claude-haiku-4-5-20251001 \
  .venv/bin/uvicorn agent.server:app --host 127.0.0.1 --port 8000
```

**Terminal 3 — Specialists dashboard** (:8765, multi-agent ops room):
```bash
.venv/bin/uvicorn agent.specialists.dev_server:app --host 127.0.0.1 --port 8765
```

### 4. Open Dashboards

- **http://localhost:8000** — 4D Cesium map + auditor-mode chat
  - Try: `worst chop tonight`, `show me the storms`, `any pilots reporting chop near boston`
  
- **http://localhost:8765** — Multi-agent specialists console
  - Pick `LOCATION = boston`, click `▶ Start Watcher`, then `🎯 Demo Correlate`

## Architecture

| Component | Purpose |
|---|---|
| `agent/server.py` | FastAPI backend + Cesium frontend |
| `agent/loop.py` | Claude tool-using agent (single-agent mode) |
| `agent/specialists/` | 5-agent constellation (weather, traffic, safety, fleet, narrator) |
| `agent/tools.py` | 10 ATC tools (METAR, traffic, aircraft lookup, etc.) |
| `scripts/opensky_to_czml.py` | Convert ADS-B traffic → Cesium 4D format |
| `scripts/record_overnight.py` | Poll OpenSky + NOAA, cache to overnight/ |
| `frontend/index.html` | Cesium 3D map + chat panel |

## Key Files

- **CLAUDE.md** — Full problem context, strategy, cost projections, demo tactics
- **docs/SETUP.md** — Detailed bring-up and troubleshooting
- **agent/specialists/README.md** — Integration contract for wiring multi-agent → main chat

## Demo Tactics

**Headline number:** ~5,500 chop-minutes across 250 flights (grows through morning).

**3-minute demo:**
1. Open http://localhost:8000, click **Traffic** → loads recorded NE flights
2. Click **⚠ Decisions** → highlights 5 flights that flew through turbulence
3. Chat: *"worst chop tonight"* → ATC-voice response with worst flight (N920PD, 139 min MOD turb)
4. Chat: *"explain this to a passenger on AAL892"* → NarratorAgent rewrites in plain English
5. Close: *"This runs in one Python process today. Each specialist is kagent-shaped — production is 5 Kubernetes CRDs, ~1 week."*

## Cost + OpenSky Budget

- **Anthropic:** ~$3 for 30 min on Sonnet, ~$15 on Opus (use ASI's tokens)
- **OpenSky:** 4000 credits/day cap. Recorder uses ~60/hr. Comfortable buffer.

## Troubleshooting

```bash
# Is the server healthy?
curl -s http://127.0.0.1:8000/api/health | jq .

# How much overnight data do we have?
python3 -c "import json,glob; flights=set(); n_snap=0
for f in glob.glob('data/overnight/traffic/*.jsonl'):
    for line in open(f):
        if line.strip(): n_snap+=1; flights.add(json.loads(line)['states'][0][0] if json.loads(line).get('states') else None)
print(f'snapshots: {n_snap}, unique flights: {len(flights)}')"

# OpenSky credits left?
.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv('.env')
from agent.opensky_auth import authed_get
r = authed_get('https://opensky-network.org/api/states/all', params={'lamin':42,'lamax':43,'lomin':-72,'lomax':-71})
print('credits left:', r.headers.get('X-Rate-Limit-Remaining'))
"
```

---

**Built for ASI Hackathon, 2026-05-30. See CLAUDE.md for full context.**
