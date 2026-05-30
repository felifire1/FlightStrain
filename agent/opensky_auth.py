"""OpenSky OAuth2 client-credentials helper.

OpenSky migrated from HTTP basic auth to OAuth2 client credentials in 2024.
Tokens are ~30min JWTs; we fetch lazily, cache in-process, and re-fetch on
expiry or 401. Single source of truth for traffic-side auth.

Usage:
    from agent.opensky_auth import authed_get

    payload = authed_get(
        "https://opensky-network.org/api/states/all",
        params={"lamin": 41, "lamax": 43, "lomin": -72, "lomax": -70},
    )

If creds are missing, falls back to anonymous (400 credits/day, no historical).
"""
from __future__ import annotations
import os
import time
from typing import Any

import httpx

TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network"
    "/protocol/openid-connect/token"
)

_TOKEN: dict[str, Any] = {"access_token": None, "expires_at": 0.0}


def _have_creds() -> bool:
    return bool(os.environ.get("OPENSKY_CLIENT_ID") and os.environ.get("OPENSKY_CLIENT_SECRET"))


def _fetch_token() -> str | None:
    cid = os.environ.get("OPENSKY_CLIENT_ID")
    csec = os.environ.get("OPENSKY_CLIENT_SECRET")
    if not (cid and csec):
        return None
    r = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": cid,
            "client_secret": csec,
        },
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    tok = body["access_token"]
    # Refresh 60s before stated expiry to avoid races
    ttl = int(body.get("expires_in", 1800))
    _TOKEN["access_token"] = tok
    _TOKEN["expires_at"] = time.time() + max(30, ttl - 60)
    return tok


def get_token(force: bool = False) -> str | None:
    """Return a valid access token, fetching/refreshing as needed.
    Returns None if no creds configured (caller should fall back to anonymous)."""
    if not _have_creds():
        return None
    if force or not _TOKEN["access_token"] or time.time() >= _TOKEN["expires_at"]:
        return _fetch_token()
    return _TOKEN["access_token"]


def auth_headers() -> dict[str, str]:
    tok = get_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def authed_get(url: str, params: dict | None = None, timeout: float = 20.0) -> httpx.Response:
    """GET with auto-attached bearer. Retries once on 401 with a forced fresh token."""
    r = httpx.get(url, params=params, headers=auth_headers(), timeout=timeout)
    if r.status_code == 401 and _have_creds():
        get_token(force=True)
        r = httpx.get(url, params=params, headers=auth_headers(), timeout=timeout)
    return r
