# Setup — fresh laptop

Last validated: 2026-05-30 on macOS 15 (Darwin 25.5), Python 3.14.3.

This is the exact bring-up sequence for moving to another machine the morning
of the hack. Allow ~15 minutes if all the keys/data sync cleanly; longer if
you need to re-record overnight data.

## 1. Clone + venv

```bash
cd ~/Documents/hacks   # or wherever the folder lives
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

The `traffic` library pulls cartopy/shapely/pyproj — wheel install takes 2–5
minutes the first time. cartopy may need to build from source; if it does and
fails, try `brew install proj geos` first.

## 2. Credentials

Copy `.env.example` to `.env` and fill in:

```
ANTHROPIC_API_KEY=<from console.anthropic.com — for the morning use ASI's token>
CESIUM_ION_TOKEN=<not used directly by Python; the frontend reads from frontend/config.js>
OPENSKY_CLIENT_ID=<from opensky-network.org -> Account -> API Clients>
OPENSKY_CLIENT_SECRET=<same>
OPENSKY_USERNAME=<your OpenSky account login>
OPENSKY_PASSWORD=<same>
```

Then create `frontend/config.js` from the example:

```bash
cp frontend/config.example.js frontend/config.js
# edit, paste your Cesium Ion token between the quotes
```

**Two different OpenSky auth methods are needed:**

- **CLIENT_ID/SECRET (OAuth2 client credentials)** — for the live REST API
  (`/api/states/all`, `/api/tracks/all`, `/api/flights/arrival`). 4000 credits/day.
  Used by `agent/tools.py:get_traffic`, `scripts/record_overnight.py`,
  `agent/tools.py:get_flight_track`, etc.
- **USERNAME/PASSWORD** — for the historical Trino backend
  (`trino.opensky-network.org`). The Trino server rejects client-credentials
  tokens; it only accepts password-grant tokens from real accounts. Used by
  `scripts/historical_pull.py`. **Note**: as of 2026-05-30, Trino access is
  also gated by a separate research-access form approval. Without it the SQL
  errors with `PERMISSION_DENIED` even with valid creds. Don't count on Trino
  being available on hack-day — use the REST fallbacks instead.

## 3. SSL on Python 3.14 / macOS

`trino-python-client` builds a `requests.Session` that doesn't pick up
certifi automatically on this Python. Either:

```bash
export REQUESTS_CA_BUNDLE=$(.venv/bin/python -c "import certifi;print(certifi.where())")
export SSL_CERT_FILE=$REQUESTS_CA_BUNDLE
```

…or rely on `scripts/historical_pull.py`, which patches `requests.Session`
defaults explicitly.

## 4. One-time data prep

```bash
# Aircraft DB (~100 MB, ~5 min). Required for callsign-to-operator/model.
mkdir -p data/cache
curl -sL -o data/cache/aircraft_db.csv \
  "https://s3.opensky-network.org/data-samples/metadata/aircraft-database-complete-2024-10.csv"
```

## 5. Smoke tests

```bash
# OAuth2 live API
.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv('.env')
from agent.opensky_auth import authed_get
r = authed_get('https://opensky-network.org/api/states/all',
               params={'lamin':41.5,'lamax':43,'lomin':-72,'lomax':-70})
print('status:', r.status_code, 'aircraft:', len(r.json().get('states') or []))
print('credits left:', r.headers.get('X-Rate-Limit-Remaining'))
"

# Aircraft DB
.venv/bin/python -c "
from agent.aircraft_db import db_size, describe
print('db size:', db_size())
print(describe('a73dfe'))  # should print 'N566JB · A320-232'
"

# Agent loop (cheap model, no map)
CLAUDE_MODEL=claude-haiku-4-5-20251001 \
  .venv/bin/python -m agent.loop "what is the current weather at KBOS"
```

## 6. Start the stack

**(a) Start the recorder** (if no overnight data is present). Output goes to
`data/overnight/traffic/*.jsonl` + `data/overnight/weather/*.json`.

```bash
caffeinate -is .venv/bin/python scripts/record_overnight.py > /tmp/recorder.log 2>&1 &
```

Wait 5 minutes minimum before generating CZMLs — you need 4-5 snapshots for
the dead-reckoning to look smooth.

**(b) Generate CZMLs from recorded data**:

```bash
# Traffic CZML (NE corridor filter; multi-file is OK)
.venv/bin/python scripts/opensky_to_czml.py \
  --bbox 40.0 44.0 -74.0 -69.0 \
  data/overnight/traffic/*.jsonl \
  data/samples/traffic.czml

# Turbulence advisory polygons
.venv/bin/python scripts/gairmet_to_czml.py \
  $(ls -t data/overnight/weather/gairmet_*.json | head -1) \
  data/samples/gairmet.czml

# Auditor-flagged decision moments
.venv/bin/python scripts/decisions_to_czml.py --hours-back 4 --top-n 5
```

**(c) Start the FastAPI server** (one origin: serves frontend + chat):

```bash
CLAUDE_MODEL=claude-haiku-4-5-20251001 \
  .venv/bin/uvicorn agent.server:app --host 127.0.0.1 --port 8000
```

Open **http://localhost:8000** in a browser. The 4D Cesium twin loads with
the chat panel bottom-right.

## 7. Demo prompts (rehearsal)

Try these in the chat panel:

- `worst chop tonight` — runs auditor, names worst flight + headline number, no map change
- `show me the storms` — loads SIGMET layer + narrates active advisories
- `any pilots reporting chop near boston` — fetches PIREPs, quotes specific reports
- `weather at KBOS` — METAR readout
- `show me N920PD` — highlights worst flight on map + camera tracks it
- `zoom to BOS and load airports` — composite camera + layer command

## 8. Known gotchas

- **Sonnet 4.6 is slow on tool loops** — ~6s per response. Haiku 4.5 is ~1.5-3s. Set `CLAUDE_MODEL=claude-haiku-4-5-20251001` for the demo.
- **Multi-turn history needs `_clean_block`** — the SDK's `model_dump()` emits `parsed_output`/`citations`/`caller` which the API rejects on input. `agent/loop.py:_clean_block` strips these. Already fixed; just don't bypass it.
- **OpenSky `/tracks/all` is ~390 credits/call** — expensive. Use sparingly; not in loops.
- **OpenSky `/flights/*` is ~7+ days lagged** — yesterday's flights aren't there; last week's are.
- **Cesium World Terrain over CONUS is heavy** — frontend uses lazy terrain (flat by default; real terrain on BOS zoom only). Don't undo that.
- **Recorder bbox should stay narrow** — 40-44N, -74 to -69W = ~1 cred/call. Continental US bbox is 4 cred/call and drains the daily budget in hours.

## 9. Quick troubleshooting

```bash
# Is the recorder running?
ps -eo pid,etime,command | grep record_overnight | grep -v grep

# Is the server alive?
curl -s http://127.0.0.1:8000/api/health
# expect: {"status":"ok","anthropic_key":true,"opensky_authed":true,"model":"..."}

# How much overnight data do we have?
.venv/bin/python -c "
import json, glob
flights=set(); n_snap=0
for f in glob.glob('data/overnight/traffic/*.jsonl'):
    for line in open(f):
        if line.strip():
            n_snap += 1
            for st in json.loads(line).get('states', []): flights.add(st[0])
print(f'snapshots: {n_snap}, unique flights: {len(flights)}')
"

# How many credits left for the day?
.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv('.env')
from agent.opensky_auth import authed_get
r = authed_get('https://opensky-network.org/api/states/all', params={'lamin':42,'lamax':43,'lomin':-72,'lomax':-71})
print('credits left:', r.headers.get('X-Rate-Limit-Remaining'))
"
```

## 10. Multi-agent specialists system

The repo also has a parallel multi-agent demo (built 2026-05-30 ~03:30 PT)
that runs *independently* of `agent/server.py`. Don't have to wire it for the
demo, but it's a strong unlock if you do (see CLAUDE.md "connection plan").

### CLI

Fastest way to test specialists without the browser:

```bash
.venv/bin/python scripts/specialists_demo.py
```

REPL commands:
- `scenario all` — feeds real cached weather + traffic into every specialist
- `scenario weather` / `scenario traffic` — just one slice
- `fire emergency` — synthetic 7700 squawk → SafetyAgent fires sev 5
- `findings` — list everything on the bus right now
- `voices` — show what each specialist would emit for a sample question
- *anything else* — gets routed to the Coordinator as a user question
- `quit`

### Standalone dev console (web UI, separate port)

```bash
.venv/bin/uvicorn agent.specialists.dev_server:app --host 127.0.0.1 --port 8765
# open http://127.0.0.1:8765/
```

What's on the page:
- Top-left **LOCATION** dropdown — select an airport/city to focus on (default: NE recorder)
- Top-bar buttons:
  - `▶ Start Watcher` — polls fresh data every 30s, fires through specialists, runs cross-correlation
  - `⛅ Weather` / `✈ Traffic` / `↻ All` — fire one snapshot manually
  - `🎯 Demo Correlate` — drops a synthetic SIGMET over NE traffic, runs correlation (guaranteed hits)
  - `⚠ Fire Emergency` — synthetic 7700 squawk
  - `Clear` — wipe the bus
- **Left pane:** live findings stream, color-coded by specialist (weather=blue, traffic=green, safety=red, fleet=magenta, narrator=cyan, coordinator=white) and severity (S0–S5)
- **Right pane:** chat with the Coordinator. Replies show voice chips for contributing specialists + map_actions.

Endpoints (for scripting / debugging):
```
GET  /api/health
GET  /api/specialists           — list manifests
GET  /api/freshness             — data age per source
POST /api/scenario {name}       — fire weather|traffic|all|correlate|emergency
POST /api/clear                 — wipe bus
GET  /api/findings?since=N      — long-poll for findings (1-second wait)
POST /api/chat {message, history}
GET  /api/watcher/status
POST /api/watcher/start {interval_sec}
POST /api/watcher/stop
GET  /api/focus
POST /api/focus {name}          — set/clear active location
```

### Stub vs LLM mode

Specialists run in `stub` mode by default — deterministic Python templates,
no API cost, no tokens needed. To enable LLM mode (any specialist call →
Claude reasoning):

```python
# In server.py (or wherever you wire it):
from anthropic import Anthropic
client = Anthropic()

def llm_call(system, user, tools):
    resp = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=512,
        system=system, messages=[{"role":"user","content":user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")

for s in specialists + [coordinator]:
    s.inject_llm(llm_call)
```

Cost: ~$0.04/hr on Sonnet at default cadence; ~$0.50/hr on Opus. ASI's tokens
will cover the demo easily.

### Demo path for the multi-agent system

```bash
# 1. start the dev server
.venv/bin/uvicorn agent.specialists.dev_server:app --host 127.0.0.1 --port 8765

# 2. open http://127.0.0.1:8765/

# 3. pick LOCATION = boston
# 4. click ▶ Start Watcher
# 5. wait 30s — findings populate from real NOAA + cached traffic
# 6. click 🎯 Demo Correlate — Coordinator emits "9 flights will transit polygon" finding
# 7. chat: "who is over boston now" — live OpenSky fetch + list
# 8. chat: "any storms" — pulls weather findings
# 9. chat: "summarize the state" — multi-voice synthesis
```

### Where the multi-agent code lives

- `agent/specialists/` — all 5 specialists + coordinator + bus
- `agent/specialists/README.md` — integration contract for `agent/server.py`
- `agent/specialists/dev_server.py` — the standalone FastAPI app
- `scripts/specialists_demo.py` — CLI
- `frontend/specialists.html` — the standalone UI
- `data/events/findings.jsonl` — persisted bus findings (gitignored)
