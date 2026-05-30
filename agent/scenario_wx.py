"""Weather-side ingest for the HRRR convective bundle (refc + retop strips).

The companion to the PIREP/turbulence track. Where `turbulence_area.py` turns
pilot reports into hazard areas, this turns the HRRR forecast grids into the
*convective* hazard areas:

  - **refc** — composite reflectivity (dBZ): precip intensity, one number per
    column. >= 40 dBZ is "heavy" (the problem's affects threshold).
  - **retop** — echo top (feet): how high the precip column reaches. This is the
    depth axis — a flight above the local echo top is in the clear.

The problem's rule (documentation/wx/FILE_FORMAT.md): weather affects a flight at
(lat, lon, alt_ft) iff `alt_ft <= retop` AND `refc >= 40`.

`hazard_polygons()` contours the refc>=40 cells into convex storm-cell polygons
and returns `Finding`s whose `metadata["polygon"]` is a list of `(lat, lon)`
tuples — the exact shape `Coordinator.correlate()` and `turbulence_area` already
consume — with a representative echo top as the band top. So projecting traffic
forward through these polygons ("N flights will transit this cell") is free reuse,
and they render via the existing `draw_polygon` map verb.

Grid (from the docs): regular equirectangular lat/lon, 256 rows x 358 cols.
Row 0 = north (lat 55.7765), last row = south (lat 21.943). Col 0 = west
(lon -135.0), last col = east (lon -67.5).

Run `python -m agent.scenario_wx` to print max dBZ + storm-cell count for a strip
and demonstrate an `affects()` true case.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np

from agent.specialists.base import Finding

# --- grid geometry (documentation/wx/FILE_FORMAT.md) ------------------------
LAT_MIN, LAT_MAX = 21.943, 55.7765
LON_MIN, LON_MAX = -135.0, -67.5
ROWS, COLS = 256, 358

# nodata sentinels: refc clear/none is very negative; retop is feet >= 0.
REFC_NODATA = -50.0   # mask m <= -50
RETOP_NODATA = 0.0    # mask m < 0

DBZ_HEAVY = 40.0      # the problem's "affects" reflectivity threshold
FT_TO_M = 0.3048


# --- bundle / forecast directory resolution ---------------------------------

def _find_bundle() -> Path:
    """Locate the hackathon data bundle. Override with WX_BUNDLE_DIR."""
    env = os.environ.get("WX_BUNDLE_DIR")
    if env:
        return Path(env).expanduser()
    repo_root = Path(__file__).resolve().parent.parent
    candidates = [
        repo_root / "data" / "wx_bundle",
        repo_root / "data" / "hackathon_data_bundle",
        Path.home() / "Downloads" / "hackathon_data_bundle",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    # Fall back to the conventional location even if missing, so error messages
    # point somewhere sensible.
    return candidates[-1]


BUNDLE_DIR = _find_bundle()


def list_asked_at() -> list[str]:
    """Available asked_at scenarios, e.g. ['2025-08-22T18:00:00Z', ...]."""
    if not BUNDLE_DIR.is_dir():
        return []
    out = []
    for p in sorted(BUNDLE_DIR.glob("asked_at_*")):
        if (p / "wx").is_dir():
            out.append(p.name[len("asked_at_"):])
    return out


def default_asked_at() -> str:
    """A sensible default scenario: WX_ASKED_AT env, else the first available."""
    env = os.environ.get("WX_ASKED_AT")
    if env:
        return env
    avail = list_asked_at()
    if not avail:
        raise FileNotFoundError(
            f"No asked_at_* scenarios under {BUNDLE_DIR}. "
            "Set WX_BUNDLE_DIR to the hackathon_data_bundle path."
        )
    return avail[0]


def _asked_at_dir(asked_at: str) -> Path:
    asked_at = asked_at.strip()
    if asked_at.startswith("asked_at_"):
        asked_at = asked_at[len("asked_at_"):]
    return BUNDLE_DIR / f"asked_at_{asked_at}"


# --- time parsing -----------------------------------------------------------

def _to_dt(t) -> datetime:
    """Normalize a datetime or ISO/strip-style string to naive UTC datetime."""
    if isinstance(t, datetime):
        if t.tzinfo is not None:
            t = t.astimezone(timezone.utc).replace(tzinfo=None)
        return t
    s = str(t).strip().replace("Z", "").replace("T", " ").replace("_", " ")
    # tolerate "2025-08-22 22:30:00" and "2025-08-22 22:30"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparseable time: {t!r}")


def _parse_strip_name(name: str) -> tuple[datetime, datetime, datetime]:
    """`{based_at}_{valid_from}_{valid_to}.npz` -> (based_at, valid_from, valid_to).

    Each timestamp is `YYYY-MM-DD_HH:MM:SS` (two underscore-joined tokens), so the
    stem splits into 6 tokens: 3 (date, time) pairs.
    """
    stem = name[:-4] if name.endswith(".npz") else name
    parts = stem.split("_")
    if len(parts) != 6:
        raise ValueError(f"unexpected strip filename: {name}")
    based = datetime.strptime(f"{parts[0]}_{parts[1]}", "%Y-%m-%d_%H:%M:%S")
    vfrom = datetime.strptime(f"{parts[2]}_{parts[3]}", "%Y-%m-%d_%H:%M:%S")
    vto = datetime.strptime(f"{parts[4]}_{parts[5]}", "%Y-%m-%d_%H:%M:%S")
    return based, vfrom, vto


class Strip:
    """One 15-minute forecast strip on disk."""
    __slots__ = ("path", "kind", "based_at", "valid_from", "valid_to")

    def __init__(self, path: Path, kind: str):
        self.path = path
        self.kind = kind
        self.based_at, self.valid_from, self.valid_to = _parse_strip_name(path.name)

    def covers(self, t: datetime) -> bool:
        return self.valid_from <= t < self.valid_to

    def iso_from(self) -> str:
        return self.valid_from.strftime("%Y-%m-%dT%H:%M:%SZ")

    def iso_to(self) -> str:
        return self.valid_to.strftime("%Y-%m-%dT%H:%M:%SZ")


def list_strips(asked_at: str, kind: str = "refc") -> list[Strip]:
    """All strips of `kind` ('refc'|'retop') for a scenario, sorted by valid_from."""
    d = _asked_at_dir(asked_at) / "wx" / kind
    if not d.is_dir():
        raise FileNotFoundError(f"missing {kind} directory: {d}")
    strips = [Strip(p, kind) for p in d.glob("*.npz")]
    strips.sort(key=lambda s: s.valid_from)
    return strips


def _select_strip(strips: list[Strip], t: datetime) -> Strip:
    """The strip whose [valid_from, valid_to) covers t; clamp to nearest edge
    strip if t falls outside the forecast horizon (keeps animation robust)."""
    if not strips:
        raise FileNotFoundError("no strips available")
    for s in strips:
        if s.covers(t):
            return s
    if t < strips[0].valid_from:
        return strips[0]
    if t >= strips[-1].valid_to:
        return strips[-1]
    # gap between strips (shouldn't happen — strips are contiguous): nearest start
    return min(strips, key=lambda s: abs((s.valid_from - t).total_seconds()))


# --- loading / masking ------------------------------------------------------

def load_strip(asked_at: str, kind: str, t) -> np.ndarray:
    """Load the refc/retop matrix whose window covers `t`, with nodata -> NaN.

    refc nodata: m <= -50.  retop nodata: m < 0.  Returns float64 (256, 358).
    """
    t = _to_dt(t)
    strip = _select_strip(list_strips(asked_at, kind), t)
    m = np.load(strip.path)["matrix"].astype(np.float64)
    if kind == "refc":
        m = np.where(m <= REFC_NODATA, np.nan, m)
    elif kind == "retop":
        m = np.where(m < RETOP_NODATA, np.nan, m)
    else:
        raise ValueError(f"kind must be 'refc' or 'retop', got {kind!r}")
    return m


def strip_window(asked_at: str, t) -> tuple[datetime, datetime]:
    """(valid_from, valid_to) of the refc strip covering t."""
    s = _select_strip(list_strips(asked_at, "refc"), _to_dt(t))
    return s.valid_from, s.valid_to


# --- grid <-> lat/lon -------------------------------------------------------

def corner_latlon(ci: float, cj: float) -> tuple[float, float]:
    """Lat/lon of grid *corner* (ci in [0, ROWS], cj in [0, COLS]). Pixel [i, j]'s
    top-left is corner (i, j); this matches the docs' pixel_top_left_latlon."""
    lat = LAT_MAX - ci / ROWS * (LAT_MAX - LAT_MIN)
    lon = LON_MIN + cj / COLS * (LON_MAX - LON_MIN)
    return lat, lon


def latlon_to_rc(lat: float, lon: float) -> tuple[int, int] | None:
    """Row/col of the cell containing (lat, lon), or None if outside the grid."""
    if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
        return None
    i = int((LAT_MAX - lat) / (LAT_MAX - LAT_MIN) * ROWS)
    j = int((lon - LON_MIN) / (LON_MAX - LON_MIN) * COLS)
    i = min(max(i, 0), ROWS - 1)
    j = min(max(j, 0), COLS - 1)
    return i, j


# --- point sampling ---------------------------------------------------------

def sample(lat: float, lon: float, t, asked_at: str | None = None) -> tuple[float, float]:
    """Sample (refc_dbz, retop_ft) at a location and time. NaN where nodata or
    off-grid. `asked_at` defaults to the module default scenario."""
    asked_at = asked_at or default_asked_at()
    rc = latlon_to_rc(lat, lon)
    if rc is None:
        return float("nan"), float("nan")
    i, j = rc
    refc = load_strip(asked_at, "refc", t)
    retop = load_strip(asked_at, "retop", t)
    return float(refc[i, j]), float(retop[i, j])


def affects(lat: float, lon: float, alt_ft: float, t, asked_at: str | None = None) -> bool:
    """The problem's rule: a flight at alt_ft is affected iff it's at/below the
    local echo top AND the local composite reflectivity is heavy (>= 40 dBZ)."""
    refc_dbz, retop_ft = sample(lat, lon, t, asked_at=asked_at)
    if np.isnan(refc_dbz) or np.isnan(retop_ft):
        return False
    return alt_ft <= retop_ft and refc_dbz >= DBZ_HEAVY


# --- polygon extraction (connected components + convex hull) ----------------

def _connected_components(mask: np.ndarray) -> list[list[tuple[int, int]]]:
    """4-connectivity labeling of a boolean grid. Returns lists of (i, j) cells."""
    visited = np.zeros_like(mask, dtype=bool)
    comps: list[list[tuple[int, int]]] = []
    rows, cols = mask.shape
    cells = np.argwhere(mask)
    for r0, c0 in cells:
        if visited[r0, c0]:
            continue
        stack = [(int(r0), int(c0))]
        visited[r0, c0] = True
        comp: list[tuple[int, int]] = []
        while stack:
            i, j = stack.pop()
            comp.append((i, j))
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ni, nj = i + di, j + dj
                if 0 <= ni < rows and 0 <= nj < cols and mask[ni, nj] and not visited[ni, nj]:
                    visited[ni, nj] = True
                    stack.append((ni, nj))
        comps.append(comp)
    return comps


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew's monotone chain. points are (x, y); returns hull CCW (no repeat)."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _cell_corners(comp: list[tuple[int, int]]) -> list[tuple[float, float]]:
    """All 4 grid corners of every cell in a component, in (col, row) space."""
    out: set[tuple[float, float]] = set()
    for i, j in comp:
        out.add((float(j), float(i)))
        out.add((float(j + 1), float(i)))
        out.add((float(j), float(i + 1)))
        out.add((float(j + 1), float(i + 1)))
    return list(out)


def extract_cells(refc: np.ndarray, retop: np.ndarray, dbz: float = DBZ_HEAVY,
                  min_cells: int = 1) -> list[dict]:
    """Contour refc>=dbz into convex storm-cell polygons.

    Returns dicts: {polygon: [(lat, lon), ...], max_dbz, mean_dbz, retop_ft,
    n_cells}. `retop_ft` is the component's max echo top — the band top, the
    depth axis the problem cares about. Coarse by design (convex hull per
    connected component); conservative for a keep-out zone.
    """
    mask = np.nan_to_num(refc, nan=-999.0) >= dbz
    out: list[dict] = []
    for comp in _connected_components(mask):
        if len(comp) < min_cells:
            continue
        # hull in (col=x, row=y) corner space, then map corners -> (lat, lon)
        hull_xy = _convex_hull(_cell_corners(comp))
        if len(hull_xy) < 3:
            continue
        polygon = [corner_latlon(ci=y, cj=x) for (x, y) in hull_xy]

        vals = np.array([refc[i, j] for (i, j) in comp], dtype=np.float64)
        tops = np.array([retop[i, j] for (i, j) in comp], dtype=np.float64)
        tops = tops[~np.isnan(tops)]
        retop_ft = float(np.max(tops)) if tops.size else 0.0
        out.append({
            "polygon": polygon,
            "max_dbz": float(np.nanmax(vals)),
            "mean_dbz": float(np.nanmean(vals)),
            "retop_ft": retop_ft,
            "n_cells": len(comp),
        })
    # strongest first
    out.sort(key=lambda c: -c["max_dbz"])
    return out


# --- color / severity mapping (shared by czml emitter) ----------------------

def dbz_color(max_dbz: float, alpha: int = 110) -> list[int]:
    """RGBA for a cell, hotter = redder. Mirrors the convective palette."""
    if max_dbz >= 55:
        rgb = [200, 30, 30]
    elif max_dbz >= 50:
        rgb = [255, 50, 40]
    elif max_dbz >= 45:
        rgb = [255, 120, 40]
    else:
        rgb = [255, 190, 60]
    return rgb + [alpha]


def _severity(max_dbz: float) -> int:
    # 40-50 dBZ heavy -> significant (3); 50+ severe/hail -> urgent (4).
    return 4 if max_dbz >= 50 else 3


def _draw_polygon_action(polygon: list[tuple[float, float]], poly_id: str,
                         label: str, color: list[int], extruded_m: float) -> dict:
    """Same draw_polygon contract as turbulence_area / coordinator.correlate."""
    return {
        "action": "draw_polygon",
        "id": poly_id,
        "points": [[lon, lat] for (lat, lon) in polygon],  # frontend wants [lon, lat]
        "color": color,
        "label": label,
        "height_m": 0,
        "extruded_m": float(extruded_m),
    }


# --- the interface contract: storm cells -> Findings ------------------------

def hazard_polygons(asked_at: str | None = None, t=None, dbz: float = DBZ_HEAVY) -> list[Finding]:
    """Convective hazard areas for the strip covering `t`, as Findings whose
    `metadata["polygon"]` is (lat, lon) — ready for Coordinator.correlate().

    Each Finding carries a representative echo top (`retop_ft`) as the vertical
    band top and a `draw_polygon` map action the frontend already renders.
    """
    asked_at = asked_at or default_asked_at()
    if t is None:
        # default to the first strip's window if no time given
        t = list_strips(asked_at, "refc")[0].valid_from
    refc = load_strip(asked_at, "refc", t)
    retop = load_strip(asked_at, "retop", t)
    vfrom, vto = strip_window(asked_at, t)

    findings: list[Finding] = []
    for n, cell in enumerate(extract_cells(refc, retop, dbz=dbz)):
        top_ft = cell["retop_ft"]
        sev = _severity(cell["max_dbz"])
        fl = int(round(top_ft / 100.0))
        label = f"CONVECTION {cell['max_dbz']:.0f} dBZ tops FL{fl:03d}"
        findings.append(Finding(
            specialist="convection",
            severity=sev,
            summary=(
                f"Convective cell: {cell['max_dbz']:.0f} dBZ (>= {dbz:.0f}) over "
                f"{cell['n_cells']} cell(s), echo tops ~FL{fl:03d}."
            ),
            detail=(
                "HRRR composite reflectivity contour. Any flight at/below the echo "
                "top transiting this polygon is in heavy precip; project traffic "
                "forward to see who enters it."
            ),
            recommended_action=(
                f"Advise transit traffic below FL{fl:03d} of heavy convection; "
                "consider lateral deviation around the cell."
            ),
            map_actions=[_draw_polygon_action(
                cell["polygon"], f"wx-cell-{n}", label,
                dbz_color(cell["max_dbz"]), top_ft * FT_TO_M,
            )],
            sources=["wx_refc", "wx_retop"],
            metadata={
                "polygon": cell["polygon"],          # (lat, lon) -> feeds correlate()
                "kind": "convective",
                "max_dbz": cell["max_dbz"],
                "mean_dbz": cell["mean_dbz"],
                "retop_ft": top_ft,                  # band top (depth axis)
                "band_top_ft": top_ft,
                "n_cells": cell["n_cells"],
                "valid_from": vfrom.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "valid_to": vto.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "asked_at": asked_at,
            },
        ))
    return findings


# --- runnable acceptance demo -----------------------------------------------

def _demo() -> None:
    asked = default_asked_at()
    strips = list_strips(asked, "refc")
    print(f"bundle: {BUNDLE_DIR}")
    print(f"asked_at: {asked}  ({len(strips)} refc strips)")

    # Pick a mid-forecast strip and report on it.
    strip = strips[len(strips) // 3]
    t = strip.valid_from
    refc = load_strip(asked, "refc", t)
    retop = load_strip(asked, "retop", t)
    print(f"\nstrip window: {strip.iso_from()} -> {strip.iso_to()}")
    print(f"  max dBZ:        {np.nanmax(refc):.1f}")
    print(f"  cells >= 40 dBZ:{int(np.nansum(refc >= 40))}")
    print(f"  max echo top:   {np.nanmax(retop):.0f} ft")

    cells = extract_cells(refc, retop, dbz=40)
    print(f"  storm-cell polygons: {len(cells)}")
    if cells:
        c = cells[0]
        print(f"    strongest: {c['max_dbz']:.0f} dBZ, tops {c['retop_ft']:.0f} ft, "
              f"{c['n_cells']} cells, {len(c['polygon'])}-pt polygon")

    # Demonstrate an affects() == True case: take the strongest cell's centroid,
    # fly something well below its echo top through it.
    if cells:
        poly = cells[0]["polygon"]
        clat = sum(p[0] for p in poly) / len(poly)
        clon = sum(p[1] for p in poly) / len(poly)
        top = cells[0]["retop_ft"]
        test_alt = max(top - 5000.0, 1000.0)
        rdbz, rtop = sample(clat, clon, t, asked_at=asked)
        hit = affects(clat, clon, test_alt, t, asked_at=asked)
        print(f"\naffects() check @ ({clat:.2f}N, {clon:.2f}W) alt {test_alt:.0f} ft, t={strip.iso_from()}")
        print(f"  sample -> refc {rdbz:.1f} dBZ, retop {rtop:.0f} ft")
        print(f"  affects = {hit}")

    hf = hazard_polygons(asked, t, dbz=40)
    print(f"\nhazard_polygons(): {len(hf)} Findings (sev>=3: "
          f"{sum(1 for f in hf if f.severity >= 3)})")
    if hf:
        f0 = hf[0]
        poly = f0.metadata["polygon"]
        print(f"  [{f0.specialist} sev{f0.severity}] {f0.summary}")
        print(f"  metadata['polygon']: {len(poly)} (lat,lon) pts; "
              f"map_actions={[a['action'] for a in f0.map_actions]}")


if __name__ == "__main__":
    _demo()
