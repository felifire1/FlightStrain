"""Build a CZML of US airports from data/samples/airports_us.json.

Large airports get a brighter marker + label; medium are dimmer.
Always-visible (no availability) so they show no matter the clock.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    src = ROOT / "data" / "samples" / "airports_us.json"
    out = ROOT / "data" / "samples" / "airports.czml"
    airports = json.loads(src.read_text())

    czml = [{
        "id": "document",
        "name": "us-airports",
        "version": "1.0",
    }]

    for ap in airports:
        is_large = ap["type"] == "large_airport"
        czml.append({
            "id": f"ap-{ap['icao']}",
            "name": ap["iata"] or ap["icao"],
            "description": ap["name"],
            "position": {"cartographicDegrees": [ap["lon"], ap["lat"], 0]},
            "point": {
                "pixelSize": 9 if is_large else 5,
                "color": {"rgba": [255, 255, 255, 230] if is_large else [180, 180, 180, 160]},
                "outlineColor": {"rgba": [0, 0, 0, 200]},
                "outlineWidth": 1.5,
                "heightReference": "CLAMP_TO_GROUND",
            },
            "label": {
                "text": ap["iata"],
                "font": "10pt 'Inter', system-ui, sans-serif",
                "pixelOffset": {"cartesian2": [10, 0]},
                "showBackground": True,
                "backgroundColor": {"rgba": [0, 0, 0, 160]},
                "fillColor": {"rgba": [240, 240, 240, 230]},
                "show": is_large,  # only large airports show labels by default
                "scale": 0.9,
            },
        })

    out.write_text(json.dumps(czml))
    print(f"wrote {out}  airports={len(czml)-1}")


if __name__ == "__main__":
    sys.exit(main())
