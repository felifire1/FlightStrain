"""Offline lookup of icao24 → aircraft metadata.

Reads OpenSky's aircraft database CSV (~50MB) at data/cache/aircraft_db.csv.
Lazy-loaded on first call, cached in-process as a dict keyed by lower-case
icao24 hex. Lookup is O(1) after first call.

The CSV header varies slightly across OpenSky dumps; we read it dynamically
and surface only the fields a dispatcher would actually quote in a sentence:
registration, operator, model.

Usage:
    from agent.aircraft_db import lookup
    info = lookup("a73dfe")  # -> {"registration": "N920PD", "operator": "Endeavor Air", "model": "CRJ-700", ...}
"""
from __future__ import annotations
import csv
import sys
from pathlib import Path

# OpenSky's dump has occasional outsized fields (long notes/owner strings).
csv.field_size_limit(sys.maxsize)
from typing import Any

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "cache" / "aircraft_db.csv"

# Columns we care about; map CSV header -> internal key. We try each candidate
# in order so we tolerate header drift across OpenSky dump versions.
_FIELD_MAP = {
    "registration":   ["registration"],
    "operator":       ["operator", "operatorcallsign"],
    "operator_icao":  ["operatoricao"],
    "model":          ["model"],
    "manufacturer":   ["manufacturername", "manufacturericao"],
    "typecode":       ["typecode", "icaoaircrafttype"],
    "owner":          ["owner"],
    "category":       ["categoryDescription"],
}

_DB: dict[str, dict[str, str]] | None = None


def _resolve_columns(header: list[str]) -> dict[str, str | None]:
    lower = [h.strip().lower() for h in header]
    out: dict[str, str | None] = {}
    for key, candidates in _FIELD_MAP.items():
        out[key] = None
        for c in candidates:
            if c.lower() in lower:
                out[key] = header[lower.index(c.lower())]
                break
    return out


def _strip(s: str | None) -> str:
    """OpenSky's CSV uses single-quoted strings; csv module preserves them."""
    if not s:
        return ""
    s = s.strip()
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        s = s[1:-1]
    return s.strip()


def _load() -> dict[str, dict[str, str]]:
    global _DB
    if _DB is not None:
        return _DB
    if not CSV_PATH.exists():
        _DB = {}
        return _DB
    db: dict[str, dict[str, str]] = {}
    with CSV_PATH.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        headers = [_strip(h) for h in (reader.fieldnames or [])]
        # Locate the icao24 column (typically first; sometimes single-quoted)
        try:
            icao_idx = [h.lower() for h in headers].index("icao24")
        except ValueError:
            _DB = {}
            return _DB
        icao_col = (reader.fieldnames or [])[icao_idx]
        cols = _resolve_columns(headers)
        # cols maps internal key -> stripped header name; we need to translate
        # back to the raw header (with quotes) since DictReader keys by raw.
        raw_by_stripped = {h: r for h, r in zip(headers, (reader.fieldnames or []))}
        for row in reader:
            icao = _strip(row.get(icao_col)).lower()
            if not icao:
                continue
            entry = {}
            for key, stripped_name in cols.items():
                if stripped_name and (raw := raw_by_stripped.get(stripped_name)):
                    val = _strip(row.get(raw))
                    if val and val.lower() not in ("unknow", "unknown", "none"):
                        entry[key] = val
            if entry:
                db[icao] = entry
    _DB = db
    return _DB


def lookup(icao24: str) -> dict[str, Any]:
    """Return {registration, operator, model, ...} for an icao24, or {} if unknown."""
    if not icao24:
        return {}
    return _load().get(icao24.lower(), {})


def describe(icao24: str, fallback_callsign: str | None = None) -> str:
    """One-line human description of an aircraft. Used in label text and auditor output.
    Examples: 'N920PD · Endeavor Air · CRJ-700' or 'AAL1767' if nothing found."""
    info = lookup(icao24)
    if not info:
        return (fallback_callsign or icao24).strip()
    bits = []
    if "registration" in info:
        bits.append(info["registration"])
    if "operator" in info:
        bits.append(info["operator"])
    if "model" in info:
        bits.append(info["model"])
    return " · ".join(bits) if bits else (fallback_callsign or icao24).strip()


def db_size() -> int:
    return len(_load())
