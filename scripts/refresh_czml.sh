#!/usr/bin/env bash
# Rebuild the visualization CZMLs from whatever the overnight recorder has captured so far.
# Run this anytime, then reload the Cesium page.
set -euo pipefail
cd "$(dirname "$0")/.."

# Concatenate every hourly traffic file
TMP_TRAFFIC=$(mktemp)
trap 'rm -f "$TMP_TRAFFIC"' EXIT
cat data/overnight/traffic/*.jsonl > "$TMP_TRAFFIC" 2>/dev/null || { echo "no traffic data yet"; exit 1; }

.venv/bin/python scripts/opensky_to_czml.py "$TMP_TRAFFIC" data/samples/traffic.czml

# Pick the G-AIRMET snapshot whose advisory validity overlaps the traffic
# window. G-AIRMETs re-issue every 3hrs; the latest snapshot's validity has
# usually drifted past the recording window. Match by validTime instead.
PICKED_GAIRMET=$(.venv/bin/python - <<'PY'
import glob, json, datetime, sys, pathlib

# Get traffic time bounds
mn = mx = None
for p in glob.glob("data/overnight/traffic/*.jsonl"):
    with open(p) as f:
        for line in f:
            if not line.strip(): continue
            t = json.loads(line).get("api_time")
            if t is None: continue
            mn = t if mn is None else min(mn, t)
            mx = t if mx is None else max(mx, t)
if mn is None:
    sys.exit("no traffic")
mid = (mn + mx) / 2

def parse_vt(s):
    if not s: return None
    return datetime.datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()

# Score each gairmet by how close its validTime is to traffic midpoint
best, best_score = None, float("inf")
for p in sorted(glob.glob("data/overnight/weather/gairmet_*.json")):
    with open(p) as f:
        data = json.load(f)
    turb = [g for g in data if (g.get("hazard") or "").startswith("TURB")]
    if not turb: continue
    vt = parse_vt(turb[0].get("validTime"))
    if vt is None: continue
    # G-AIRMET is valid for ~3hr from validTime. We want validTime <= mid <= validTime+3hr
    end = vt + 3*3600
    if vt <= mid <= end:
        score = 0  # perfect overlap
    else:
        score = min(abs(vt - mid), abs(end - mid))
    if score < best_score:
        best, best_score = p, score
print(best or "")
PY
)

if [ -n "${PICKED_GAIRMET:-}" ]; then
  echo "selected G-AIRMET: $(basename $PICKED_GAIRMET)"
  .venv/bin/python scripts/gairmet_to_czml.py "$PICKED_GAIRMET" data/samples/gairmet.czml
fi

# Convective SIGMETs — pick the most recent snapshot (these refresh every ~5min
# in the recorder; latest is fine since the data IS current state).
LATEST_SIGMET=$(ls -t data/overnight/weather/airsigmet_*.json 2>/dev/null | head -1)
if [ -n "${LATEST_SIGMET:-}" ]; then
  .venv/bin/python scripts/sigmets_to_czml.py "$LATEST_SIGMET" data/samples/sigmets.czml || true
fi

# PIREPs — pilot reports. The recorder doesn't capture these (separate API),
# so fetch live across CONUS bbox here. age=6 covers a useful window.
echo "fetching live PIREPs..."
mkdir -p data/samples
curl -s "https://aviationweather.gov/api/data/pirep?bbox=24,-125,50,-66&format=json&age=6" \
  -o data/samples/pireps_us.json
.venv/bin/python scripts/pireps_to_czml.py data/samples/pireps_us.json data/samples/pireps.czml || true

# Winds aloft — fetch high-level forecast (FL450/FL530, jet stream level).
echo "fetching winds aloft..."
curl -s "https://aviationweather.gov/api/data/windtemp?fcst=06&region=us&level=high&format=raw" \
  -o data/samples/winds_hi.txt
.venv/bin/python scripts/winds_to_czml.py data/samples/winds_hi.txt data/samples/winds.czml || true

echo ""
echo "refreshed. reload http://127.0.0.1:8000/frontend/index.html"
