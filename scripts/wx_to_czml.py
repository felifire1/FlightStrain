"""Convert HRRR convective strips (refc + retop) into a time-dynamic CZML overlay.

Each 15-minute forecast strip becomes a set of extruded storm-cell volumes:

  - footprint  = the refc>=dbz contour (convex storm-cell polygon, lat/lon)
  - height     = the cell's echo top (retop, feet -> meters) — the depth axis
  - color      = composite reflectivity (hotter dBZ = redder)
  - availability = the strip's [valid_from, valid_to) window

Because every packet carries `availability`, the cells appear and disappear as
the Cesium clock advances — the storms animate in lock-step with the flight
tracks. A `clock` packet spans the whole forecast so the timeline lands on the
weather by default.

Geometry + contouring are reused from agent.scenario_wx (no reimplementation).

Usage:
    .venv/bin/python scripts/wx_to_czml.py \
        --asked-at 2025-05-29T21:00:00Z \
        --out data/samples/wx.czml \
        [--dbz 40] [--stride 1] [--max-strips N] \
        [--bbox LAMIN LAMAX LOMIN LOMAX] [--bundle DIR] [--multiplier 300]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from agent import scenario_wx as wx  # noqa: E402

FT_TO_M = wx.FT_TO_M


def _cell_in_bbox(polygon, bbox) -> bool:
    """True if any vertex of the (lat, lon) polygon falls within bbox."""
    if not bbox:
        return True
    lamin, lamax, lomin, lomax = bbox
    return any(lamin <= lat <= lamax and lomin <= lon <= lomax for (lat, lon) in polygon)


def build_czml(asked_at: str, dbz: float = 40.0, stride: int = 1,
               max_strips: int | None = None, bbox=None, multiplier: int = 300) -> list[dict]:
    strips = wx.list_strips(asked_at, "refc")
    if not strips:
        raise SystemExit(f"no refc strips for asked_at={asked_at}")

    chosen = strips[::stride]
    if max_strips:
        chosen = chosen[:max_strips]

    clock_from = min(s.valid_from for s in chosen)
    clock_to = max(s.valid_to for s in chosen)
    czml = [{
        "id": "document",
        "name": f"wx-convection-{asked_at}",
        "version": "1.0",
        "clock": {
            "interval": f"{_iso(clock_from)}/{_iso(clock_to)}",
            "currentTime": _iso(clock_from),
            "multiplier": multiplier,
            "range": "LOOP_STOP",
            "step": "SYSTEM_CLOCK_MULTIPLIER",
        },
    }]

    n_cells = 0
    for si, strip in enumerate(chosen):
        refc = wx.load_strip(asked_at, "refc", strip.valid_from)
        retop = wx.load_strip(asked_at, "retop", strip.valid_from)
        availability = f"{strip.iso_from()}/{strip.iso_to()}"
        for ci, cell in enumerate(wx.extract_cells(refc, retop, dbz=dbz)):
            if not _cell_in_bbox(cell["polygon"], bbox):
                continue
            top_m = cell["retop_ft"] * FT_TO_M
            color = wx.dbz_color(cell["max_dbz"], alpha=120)
            outline = [color[0], color[1], color[2], 220]
            fl = int(round(cell["retop_ft"] / 100.0))
            positions: list[float] = []
            for lat, lon in cell["polygon"]:
                positions.extend([lon, lat, 0.0])  # cartographicDegrees: lon, lat, height
            czml.append({
                "id": f"wx-{si}-{ci}",
                "name": f"{cell['max_dbz']:.0f} dBZ · tops FL{fl:03d}",
                "description": (
                    f"<pre style='color:#e6ebef;font:11px ui-monospace,monospace'>"
                    f"composite reflectivity {cell['max_dbz']:.0f} dBZ\n"
                    f"echo top {cell['retop_ft']:.0f} ft (FL{fl:03d})\n"
                    f"cells {cell['n_cells']}\n"
                    f"valid {availability}</pre>"
                ),
                "availability": availability,
                "polygon": {
                    "positions": {"cartographicDegrees": positions},
                    "height": 0.0,
                    "extrudedHeight": top_m,
                    "material": {"solidColor": {"color": {"rgba": color}}},
                    "outline": True,
                    "outlineColor": {"rgba": outline},
                },
            })
            n_cells += 1

    build_czml.last_stats = {  # type: ignore[attr-defined]
        "strips": len(chosen), "cells": n_cells,
        "from": _iso(clock_from), "to": _iso(clock_to),
    }
    return czml


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    ap = argparse.ArgumentParser(description="HRRR refc/retop strips -> time-dynamic CZML storm volumes")
    ap.add_argument("--asked-at", default=None,
                    help="scenario timestamp, e.g. 2025-05-29T21:00:00Z (default: first available)")
    ap.add_argument("--out", default="data/samples/wx.czml", help="output CZML path")
    ap.add_argument("--dbz", type=float, default=40.0, help="reflectivity threshold (default 40)")
    ap.add_argument("--stride", type=int, default=1, help="use every Nth strip (default 1 = all)")
    ap.add_argument("--max-strips", type=int, default=None, help="cap number of strips emitted")
    ap.add_argument("--bbox", type=float, nargs=4, metavar=("LAMIN", "LAMAX", "LOMIN", "LOMAX"),
                    default=None, help="only emit cells with a vertex in this bbox")
    ap.add_argument("--bundle", default=None, help="override bundle dir (else WX_BUNDLE_DIR / default)")
    ap.add_argument("--multiplier", type=int, default=300, help="CZML clock playback multiplier")
    args = ap.parse_args()

    if args.bundle:
        os.environ["WX_BUNDLE_DIR"] = args.bundle
        wx.BUNDLE_DIR = Path(args.bundle).expanduser()

    asked_at = args.asked_at or wx.default_asked_at()
    czml = build_czml(asked_at, dbz=args.dbz, stride=args.stride,
                      max_strips=args.max_strips, bbox=args.bbox, multiplier=args.multiplier)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(czml))

    st = build_czml.last_stats  # type: ignore[attr-defined]
    print(f"wrote {out_path}")
    print(f"  asked_at={asked_at}  strips={st['strips']}  storm-cell volumes={st['cells']}")
    print(f"  clock {st['from']} -> {st['to']}  (multiplier {args.multiplier}x)")


if __name__ == "__main__":
    main()
