"""Convert NOAA FB winds-aloft text into CZML wind barbs at altitude.

The Aviation Weather windtemp endpoint returns the classic FD/FB format:

    FT   45000  53000
    ABI 265164 262366
    ALB 315251 323355
    ...

Each 6-char block is `DDFFTT`:
    - DD * 10 = wind direction in degrees (FROM)
    - FF      = wind speed in knots
    - TT      = temperature in °C (always negative at FL240+, sign omitted)
    Special: if DD > 36, subtract 50 (wind >100kt encoding); else use as-is.

We map FB station codes → airport lat/lon and render each station's wind at
each forecast level as a polyline pointing in the direction the wind is
blowing TO (i.e. heading 180° from the FROM direction). Length is proportional
to wind speed.

Usage:
    .venv/bin/python scripts/winds_to_czml.py <windtemp.txt> <output.czml>
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path
from math import sin, cos, asin, atan2, radians, degrees

ROOT = Path(__file__).resolve().parent.parent
AIRPORTS_JSON = ROOT / "data" / "samples" / "airports_us.json"

FT_TO_M = 0.3048
EARTH_R_M = 6_371_000.0

# Visualization: how long (in meters) one knot of wind extends the barb.
KT_TO_M = 8_000   # 50kt = 400km arrow — easy to read at CONUS scale


def gc_project(lat: float, lon: float, bearing: float, dist_m: float) -> tuple[float, float]:
    """Project a point forward by dist_m along bearing using spherical earth.
    Returns (lat, lon) in degrees."""
    p1 = radians(lat); l1 = radians(lon); brg = radians(bearing)
    d = dist_m / EARTH_R_M
    p2 = asin(sin(p1) * cos(d) + cos(p1) * sin(d) * cos(brg))
    l2 = l1 + atan2(sin(brg) * sin(d) * cos(p1),
                    cos(d) - sin(p1) * sin(p2))
    return degrees(p2), degrees(l2)


def decode_ddfftt(token: str) -> tuple[int, int] | None:
    """Decode a single FB wind code 'DDFFTT' to (dir_from_deg, speed_kt).
    Drops temperature. Returns None for unparseable tokens."""
    if not token or len(token) < 4:
        return None
    if not token[:4].isdigit():
        return None
    dd = int(token[:2])
    ff = int(token[2:4])
    # 51..86 encoding: wind > 99kt; subtract 50 from dd, add 100 to ff.
    if dd > 36:
        dd -= 50
        ff += 100
    if dd > 36:
        return None  # still invalid
    return (dd * 10) % 360, ff


def parse_fb(text: str) -> dict:
    """Parse the FB text block into {station -> {altitude_ft: (dir, speed), ...}}.
    Returns (header_levels, station_winds)."""
    lines = text.strip().splitlines()
    # Find the "FT  <level1> <level2> ..." header
    levels: list[int] | None = None
    rows: list[str] = []
    for line in lines:
        if line.startswith("FT "):
            parts = line.split()
            levels = [int(x) for x in parts[1:] if x.isdigit()]
            continue
        if levels is not None and line and not line.startswith(("DATA ", "VALID ", "FB", "FD")):
            rows.append(line)
    if not levels:
        return {"levels": [], "stations": {}}

    stations: dict[str, dict[int, tuple[int, int]]] = {}
    for line in rows:
        parts = line.split()
        if len(parts) < 2:
            continue
        code = parts[0]
        for i, tok in enumerate(parts[1:]):
            if i >= len(levels):
                break
            decoded = decode_ddfftt(tok)
            if decoded is None:
                continue
            stations.setdefault(code, {})[levels[i]] = decoded
    return {"levels": levels, "stations": stations}


def load_station_lookup() -> dict[str, tuple[float, float]]:
    """Map FB 3-letter station codes to (lat, lon).

    FB stations are roughly the FAA forecast-point set — usually the IATA code
    of a major airport. We start with the OurAirports US set and match by IATA;
    a handful of FB codes that aren't IATA-typical we hardcode."""
    out: dict[str, tuple[float, float]] = {}
    try:
        airports = json.loads(AIRPORTS_JSON.read_text())
    except FileNotFoundError:
        airports = []
    for ap in airports:
        iata = (ap.get("iata") or "").strip().upper()
        if not iata or len(iata) != 3:
            continue
        # Prefer large airports if there are duplicates (rare in FB universe)
        if iata not in out or ap.get("type") == "large_airport":
            out[iata] = (float(ap["lat"]), float(ap["lon"]))
    # Common FB-only codes that aren't IATA airports. Approximate VOR positions.
    EXTRA = {
        "BAM": (40.59, -116.87),  # Battle Mountain
        "BCE": (37.71, -112.30),  # Bryce Canyon
        "BIH": (37.37, -118.36),  # Bishop
        "BKE": (44.84, -117.81),  # Baker City
        "DLN": (45.25, -112.55),  # Dillon
        "FMN": (36.74, -108.23),  # Farmington
        "HVR": (48.54, -109.76),  # Havre
        "LBL": (37.04, -100.96),  # Liberal
        "LBF": (41.13, -100.68),  # North Platte
        "LWS": (46.37, -117.02),  # Lewiston
        "MLS": (46.43, -105.89),  # Miles City
        "MOT": (48.26, -101.28),  # Minot
        "RAP": (44.05, -103.06),  # Rapid City
        "SLN": (38.79, -97.65),   # Salina
        "STL": (38.75, -90.37),   # St Louis
        "TBE": (37.26, -103.60),  # Thurman
        "TUS": (32.12, -110.94),  # Tucson
        "ABR": (45.45, -98.42),
        "AGC": (40.35, -79.93),
        "ALS": (37.43, -105.87),
        "BFF": (41.87, -103.59),
        "BLH": (33.62, -114.71),
        "BRL": (40.78, -91.13),
        "BTR": (30.53, -91.15),
        "CRP": (27.77, -97.50),
        "CVG": (39.04, -84.66),
        "DBQ": (42.40, -90.71),
        "DEN": (39.86, -104.67),
        "DLH": (46.84, -92.19),
        "DRO": (37.15, -107.75),
        "EKN": (38.89, -79.86),
        "EYW": (24.55, -81.76),
        "FAR": (46.92, -96.81),
        "FAT": (36.78, -119.72),
        "FOT": (40.66, -124.13),
        "FSD": (43.58, -96.74),
        "GCK": (37.93, -100.72),
        "GEG": (47.62, -117.53),
        "GTF": (47.48, -111.37),
        "HLN": (46.61, -112.00),
        "HOU": (29.65, -95.28),
        "IMB": (42.59, -98.43),
        "INK": (31.78, -103.20),
        "INL": (48.57, -93.40),
        "JAN": (32.31, -90.08),
        "JOT": (41.43, -88.18),
        "MCI": (39.30, -94.71),
        "MCN": (32.69, -83.65),
        "MEM": (35.04, -89.98),
        "MGM": (32.30, -86.39),
        "MKC": (39.12, -94.59),
        "MKE": (42.95, -87.90),
        "MLB": (28.10, -80.65),
        "MOB": (30.69, -88.24),
        "MRF": (30.37, -103.99),
        "MSP": (44.88, -93.22),
        "MSY": (29.99, -90.26),
        "OKC": (35.39, -97.60),
        "OMA": (41.30, -95.89),
        "ONT": (34.06, -117.60),
        "ORF": (36.89, -76.20),
        "PHL": (39.87, -75.24),
        "PHX": (33.43, -112.01),
        "PIH": (42.91, -112.60),
        "PIT": (40.49, -80.23),
        "PSB": (40.88, -78.09),
        "PWM": (43.65, -70.31),
        "RDU": (35.88, -78.79),
        "RIC": (37.51, -77.32),
        "ROA": (37.33, -79.97),
        "ROC": (43.12, -77.67),
        "SAC": (38.51, -121.49),
        "SAN": (32.73, -117.19),
        "SAT": (29.53, -98.47),
        "SBA": (34.43, -119.84),
        "SEA": (47.45, -122.31),
        "SFO": (37.62, -122.38),
        "SLC": (40.79, -111.98),
        "SPS": (33.99, -98.49),
        "SYR": (43.11, -76.11),
        "TLH": (30.40, -84.35),
        "TPA": (27.98, -82.53),
        "TUL": (36.20, -95.89),
        "TVC": (44.74, -85.58),
        "TYS": (35.81, -83.99),
        "ABQ": (35.04, -106.61),
        "AMA": (35.22, -101.71),
        "ATL": (33.64, -84.43),
        "AUS": (30.20, -97.67),
        "BHM": (33.56, -86.75),
        "BIL": (45.81, -108.54),
        "BIS": (46.77, -100.75),
        "BNA": (36.12, -86.68),
        "BOI": (43.56, -116.22),
        "BOS": (42.36, -71.01),
        "BTV": (44.47, -73.15),
        "BWI": (39.18, -76.67),
        "CHA": (35.04, -85.20),
        "CHS": (32.90, -80.04),
        "CLE": (41.41, -81.85),
        "CLT": (35.21, -80.94),
        "CMH": (39.99, -82.89),
        "COS": (38.81, -104.71),
        "CYS": (41.16, -104.81),
        "DAL": (32.85, -96.85),
        "DAY": (39.90, -84.22),
        "DCA": (38.85, -77.04),
        "DSM": (41.53, -93.66),
        "DTW": (42.21, -83.35),
        "ELP": (31.81, -106.38),
        "EUG": (44.12, -123.21),
        "EVV": (38.04, -87.53),
        "EWR": (40.69, -74.17),
        "FLG": (35.14, -111.67),
        "FLL": (26.07, -80.15),
        "GGW": (48.21, -106.61),
        "GJT": (39.12, -108.53),
        "GRB": (44.49, -88.13),
        "HNL": (21.32, -157.92),
        "HTS": (38.37, -82.55),
        "IAD": (38.94, -77.45),
        "IAH": (29.98, -95.34),
        "ICT": (37.65, -97.43),
        "ILM": (34.27, -77.90),
        "IND": (39.71, -86.29),
        "JAX": (30.49, -81.69),
        "JFK": (40.64, -73.78),
        "LAS": (36.08, -115.15),
        "LAX": (33.94, -118.41),
        "LBB": (33.66, -101.82),
        "LCH": (30.13, -93.22),
        "LEX": (38.04, -84.61),
        "LGA": (40.78, -73.87),
        "LGB": (33.82, -118.15),
        "LIT": (34.73, -92.22),
        "MIA": (25.79, -80.29),
        "MMU": (40.80, -74.41),
        "MOT": (48.26, -101.28),
        "MSL": (34.75, -87.61),
        "MSN": (43.14, -89.34),
        "MSO": (46.92, -114.09),
        "PDX": (45.59, -122.60),
        "PVD": (41.73, -71.43),
        "RNO": (39.50, -119.77),
        "SDF": (38.17, -85.74),
        "SDY": (47.71, -104.19),
        "SGF": (37.25, -93.39),
        "SHV": (32.45, -93.83),
        "SJC": (37.36, -121.93),
        "SLE": (44.91, -123.00),
        "SLI": (33.81, -118.04),
        "SMX": (34.90, -120.46),
        "SUX": (42.40, -96.38),
        "TUS": (32.12, -110.94),
        "ABI": (32.41, -99.68),
        "ALB": (42.75, -73.80),
        "ORD": (41.98, -87.91),
        "DFW": (32.90, -97.04),
    }
    out.update(EXTRA)
    return out


def main(in_path: Path, out_path: Path) -> None:
    text = in_path.read_text()
    parsed = parse_fb(text)
    if not parsed["levels"]:
        print("no FT header found in input", file=sys.stderr)
        sys.exit(1)

    lookup = load_station_lookup()
    levels = parsed["levels"]

    # Pick a single representative level for visualization. Prefer FL300, fall
    # back to whatever's closest.
    target = 30_000
    level = min(levels, key=lambda lv: abs(lv - target))
    print(f"levels={levels}  using={level} ft", file=sys.stderr)

    alt_m = level * FT_TO_M

    czml = [{
        "id": "document",
        "name": f"winds-aloft-FL{level // 100}",
        "version": "1.0",
    }]

    used = 0
    skipped = 0
    for code, by_level in parsed["stations"].items():
        if level not in by_level:
            skipped += 1
            continue
        wind_dir_from, speed_kt = by_level[level]
        if speed_kt < 5:
            continue  # skip near-calm
        latlon = lookup.get(code)
        if not latlon:
            skipped += 1
            continue
        lat, lon = latlon

        # Arrow points in direction wind is blowing TO. Wind FROM 270° (west)
        # blows TO 090° (east), so arrow bearing = (dir_from + 180) % 360.
        bearing_to = (wind_dir_from + 180) % 360
        length = max(50_000, speed_kt * KT_TO_M)  # min length so weak arrows are visible
        end_lat, end_lon = gc_project(lat, lon, bearing_to, length)

        # Color by speed band: cyan = light, yellow = moderate, magenta = jet stream
        if speed_kt < 30:
            color = [120, 200, 240, 200]
        elif speed_kt < 70:
            color = [240, 220, 100, 220]
        else:
            color = [230, 80, 200, 240]

        czml.append({
            "id": f"wind-{code}",
            "name": f"{code} · FL{level // 100} · {wind_dir_from:03d}°/{speed_kt}kt",
            "description": f"<div style='font:12px ui-monospace;color:#e6ebef;padding:4px'>"
                           f"<div style='font-size:11px;color:#8a929b;letter-spacing:0.15em'>› WINDS ALOFT</div>"
                           f"<h2 style='margin:4px 0;font:600 16px Inter,sans-serif;color:#fff'>{code} · FL{level // 100}</h2>"
                           f"<div>Direction: <b>{wind_dir_from:03d}°</b> (from)</div>"
                           f"<div>Speed: <b>{speed_kt} kt</b></div>"
                           f"</div>",
            "polyline": {
                "positions": {"cartographicDegrees": [lon, lat, alt_m, end_lon, end_lat, alt_m]},
                "width": 2.5,
                "material": {
                    "polylineArrow": {"color": {"rgba": color}}
                },
                "arcType": "GEODESIC",
            },
        })
        used += 1

    out_path.write_text(json.dumps(czml))
    print(f"wrote {out_path}  arrows={used}  level=FL{level // 100}  skipped_unknown={skipped}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: winds_to_czml.py <windtemp.txt> <output.czml>", file=sys.stderr)
        sys.exit(2)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
