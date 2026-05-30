"""Convert PIREPs (pilot reports) into a CZML overlay of 3D point markers.

Each PIREP becomes a sphere at the reporting altitude (FL * 100ft), colored
by reported turbulence intensity. Hover/click pops the raw report.

Why this matters: G-AIRMETs are *forecasts*. PIREPs are *what pilots actually
flew through 5 minutes ago* — the ground truth ATCs and dispatchers trust
most. Showing them as 3D dots in airspace makes the data legible at a glance.

Usage:
    .venv/bin/python scripts/pireps_to_czml.py <pireps.json> <output.czml>
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

FT_TO_M = 0.3048

# PIREP turbulence intensity scale → RGBA. Each PIREP gets the worst of its
# turbulence and icing intensities.
INTENSITY_COLOR = {
    # smooth-to-light variants
    "NEG":      [110, 231, 160, 200],
    "SMTH":     [110, 231, 160, 200],
    "SMTH-LGT": [180, 220, 140, 220],
    "LGT":      [240, 200,  80, 230],
    "LGT-MOD":  [255, 170,  60, 235],
    "MOD":      [255, 130,  40, 240],
    "MOD-SEV":  [255,  80,  60, 245],
    "SEV":      [240,  50,  50, 250],
    "EXTM":     [255,  20, 180, 250],
}
# Severity rank for picking the "worst" between turb1, turb2, icing1, icing2.
RANK = {k: i for i, k in enumerate([
    "", "NEG", "SMTH", "SMTH-LGT", "LGT", "LGT-MOD", "MOD", "MOD-SEV", "SEV", "EXTM",
])}


def iso(ts) -> str:
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return str(ts)


def worst_intensity(p: dict) -> str:
    candidates = []
    for k in ("tbInt1", "tbInt2", "icgInt1", "icgInt2"):
        v = (p.get(k) or "").strip().upper()
        if v:
            candidates.append(v)
    if not candidates:
        return ""
    return max(candidates, key=lambda c: RANK.get(c, 0))


def fmt_pirep_html(p: dict) -> str:
    raw = (p.get("rawOb") or "").strip()
    obs_iso = iso(p.get("obsTime")) if p.get("obsTime") else "—"
    fields = []
    for label, k in [
        ("Aircraft", "acType"), ("Flight Level", "fltLvl"),
        ("Station", "icaoId"), ("Observed", None),
    ]:
        if label == "Observed":
            fields.append(("Observed", obs_iso))
        else:
            v = p.get(k)
            if v not in (None, ""):
                fields.append((label, v if label != "Flight Level" else f"FL{v}"))

    # Pull out the structured turbulence/icing fields if present
    turb_rows = []
    for n in (1, 2):
        intens = (p.get(f"tbInt{n}") or "").strip()
        if intens:
            tbase = p.get(f"tbBas{n}"); ttop = p.get(f"tbTop{n}")
            t_type = (p.get(f"tbType{n}") or "").strip()
            band = ""
            if tbase or ttop:
                band = f" · FL{tbase or '?'}–FL{ttop or '?'}"
            turb_rows.append(f"<li>{intens} {t_type}{band}</li>")
    icing_rows = []
    for n in (1, 2):
        intens = (p.get(f"icgInt{n}") or "").strip()
        if intens:
            ibase = p.get(f"icgBas{n}"); itop = p.get(f"icgTop{n}")
            i_type = (p.get(f"icgType{n}") or "").strip()
            band = ""
            if ibase or itop:
                band = f" · FL{ibase or '?'}–FL{itop or '?'}"
            icing_rows.append(f"<li>{intens} {i_type}{band}</li>")

    rows_html = "".join(
        f"<tr><td style='color:#8a929b;padding:2px 8px 2px 0'>{k}</td><td>{v}</td></tr>"
        for k, v in fields
    )
    turb_html = f"<h3 style='margin:8px 0 4px;font:600 11px Inter,sans-serif;letter-spacing:0.1em;color:#ff8a3c'>TURBULENCE</h3><ul style='margin:0;padding-left:18px'>{''.join(turb_rows)}</ul>" if turb_rows else ""
    icg_html = f"<h3 style='margin:8px 0 4px;font:600 11px Inter,sans-serif;letter-spacing:0.1em;color:#78c8ff'>ICING</h3><ul style='margin:0;padding-left:18px'>{''.join(icing_rows)}</ul>" if icing_rows else ""

    return f"""
    <div style="font:12px ui-monospace,Menlo,monospace;color:#e6ebef;padding:4px">
      <div style="font-size:11px;color:#8a929b;letter-spacing:0.15em;text-transform:uppercase">› pirep</div>
      <h2 style="margin:4px 0 10px;font:600 16px 'Inter',sans-serif;color:#fff">Pilot Report</h2>
      <table style="width:100%;border-collapse:collapse">{rows_html}</table>
      {turb_html}{icg_html}
      <div style="margin-top:10px;padding-top:8px;border-top:1px dotted #2a3138;color:#8a929b;font-size:11px">
        <pre style="margin:0;white-space:pre-wrap;font:11px ui-monospace,monospace">{raw}</pre>
      </div>
    </div>
    """


def main(in_path: Path, out_path: Path) -> None:
    raw_data = json.loads(in_path.read_text())
    if not isinstance(raw_data, list) or not raw_data:
        print("empty / invalid PIREP file", file=sys.stderr)
        sys.exit(1)

    czml = [{
        "id": "document",
        "name": "pireps",
        "version": "1.0",
    }]

    used = 0
    for i, p in enumerate(raw_data):
        lat, lon = p.get("lat"), p.get("lon")
        if lat is None or lon is None:
            continue
        fl = p.get("fltLvl")
        # If no flight level, place at FL050 just so the marker is visible above ground
        alt_m = (fl * 100 if fl else 5000) * FT_TO_M

        intens = worst_intensity(p) or "NEG"
        color = INTENSITY_COLOR.get(intens, [180, 180, 180, 220])

        # Availability — show for ~2hr around the observation time so it doesn't
        # smear across the entire timeline when scrubbing.
        obs = p.get("obsTime")
        availability = None
        if obs:
            t0 = datetime.fromtimestamp(obs, tz=timezone.utc) - timedelta(minutes=30)
            t1 = datetime.fromtimestamp(obs, tz=timezone.utc) + timedelta(hours=2)
            availability = f"{iso(t0.timestamp())}/{iso(t1.timestamp())}"

        # Size grows with severity for legibility
        size = 8 + RANK.get(intens, 0) * 2

        entity = {
            "id": f"pirep-{i}",
            "name": f"PIREP {intens or 'NEG'} · {p.get('acType') or '?'} · FL{fl or '?'}",
            "description": fmt_pirep_html(p),
            "position": {"cartographicDegrees": [float(lon), float(lat), alt_m]},
            "point": {
                "pixelSize": size,
                "color": {"rgba": color},
                "outlineColor": {"rgba": [10, 13, 16, 220]},
                "outlineWidth": 1.5,
                "heightReference": "NONE",
                "scaleByDistance": {"nearFarScalar": [10_000, 1.6, 4_000_000, 0.7]},
            },
        }
        if availability:
            entity["availability"] = availability
        czml.append(entity)
        used += 1

    out_path.write_text(json.dumps(czml))
    print(f"wrote {out_path}  pireps={used}/{len(raw_data)}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: pireps_to_czml.py <pireps.json> <output.czml>", file=sys.stderr)
        sys.exit(2)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
