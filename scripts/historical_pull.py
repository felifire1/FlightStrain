"""Pull historical OpenSky state vectors via pyopensky's Trino backend.

OpenSky's Trino endpoint requires a password-grant token (issued for the
`trino-client` audience from a real user account). Our API-client OAuth2
credentials don't work there — they're only good for the live REST API.
pyopensky reads OPENSKY_USERNAME / OPENSKY_PASSWORD from env (via dotenv) and
handles the password-grant flow itself.

Usage:
    .venv/bin/python scripts/historical_pull.py \
        --start "2026-05-29 22:00" --end "2026-05-29 22:05" \
        --bbox 41.5 43.0 -72.0 -70.0 \
        --out data/historical/bos_test.jsonl

Notes:
- `state_vectors_data4` is hour-partitioned. The library filters by `hour` for
  you when you pass a time range, but we keep an explicit `BETWEEN` to be sure.
- pyopensky caches Trino responses to ~/Library/Caches/opensky — second call
  with the same window is free.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Python 3.14 on macOS doesn't pick up certifi via the usual env vars when
# trino-python-client builds its own requests.Session. Force it by poking the
# requests defaults *before* the trino/sqlalchemy stack imports.
import certifi
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
os.environ["SSL_CERT_FILE"] = certifi.where()
import requests.utils
import requests.adapters
requests.utils.DEFAULT_CA_BUNDLE_PATH = certifi.where()

# Last line of defense: monkeypatch Session.merge_environment_settings so any
# session built downstream gets verify=certifi by default.
_orig = requests.Session.merge_environment_settings
def _patched(self, url, proxies, stream, verify, cert):
    settings = _orig(self, url, proxies, stream, verify, cert)
    if not settings.get("verify"):
        settings["verify"] = certifi.where()
    return settings
requests.Session.merge_environment_settings = _patched

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))


def parse_iso(s: str) -> datetime:
    """Parse '2026-05-29 22:00' (UTC assumed) -> aware datetime."""
    s = s.strip().replace("T", " ")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="UTC, e.g. '2026-05-29 22:00'")
    ap.add_argument("--end", required=True, help="UTC, e.g. '2026-05-29 22:05'")
    ap.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("LAMIN", "LAMAX", "LOMIN", "LOMAX"))
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    t0 = parse_iso(args.start)
    t1 = parse_iso(args.end)
    lamin, lamax, lomin, lomax = args.bbox

    print(f"query  t=[{t0.isoformat()}, {t1.isoformat()}]  bbox=[{lamin},{lamax}]x[{lomin},{lomax}]",
          file=sys.stderr)

    from pyopensky.trino import Trino

    trino = Trino()
    # bounds=(west, south, east, north); same convention as traffic.
    df = trino.history(
        start=t0,
        stop=t1,
        bounds=(lomin, lamin, lomax, lamax),
    )

    if df is None or len(df) == 0:
        print("no rows returned", file=sys.stderr)
        sys.exit(1)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_json(args.out, orient="records", lines=True, date_format="iso")

    n_aircraft = df["icao24"].nunique() if "icao24" in df.columns else -1
    print(f"wrote {args.out}  rows={len(df)}  aircraft={n_aircraft}  cols={list(df.columns)}")


if __name__ == "__main__":
    main()
