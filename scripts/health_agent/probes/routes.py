"""Layer 3: API route probes — login as seed admin, hit a cheap GET on each router."""

from __future__ import annotations

from typing import List

import requests

from ..config import ADMIN_EMAIL, ADMIN_PASSWORD, API, PROBE_TIMEOUT
from .base import ProbeResult, SkipProbe, run_probe

# (module_label, method, path, expected_2xx_or_skip_status)
# Only routes that work without an existing ad_id. Ad-scoped routes are covered by e2e.
PROBES = [
    ("auth.check_email", "GET", f"/auth/check-email?email={ADMIN_EMAIL}", False),
    ("users.list", "GET", "/users/", True),
    ("documents.list", "GET", "/documents/", True),
    ("company.profile", "GET", "/company/profile", True),
    ("brand_kit.get", "GET", "/brand-kit/", True),
    ("platform_connections", "GET", "/platform-connections/", True),
    ("ads_crud.list", "GET", "/advertisements/", True),
    ("ads_voice.profiles", "GET", "/advertisements/voice-profiles/australian", True),
    ("meta.oauth_url", "GET", "/platform-connections/meta/oauth-url", True),
    ("google.oauth_url", "GET", "/platform-connections/google/oauth-url", True),
]


def _login() -> str:
    r = requests.post(
        f"{API}/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=PROBE_TIMEOUT,
    )
    if r.status_code == 401:
        raise SkipProbe(f"seed admin {ADMIN_EMAIL} not registered (run tests/seed.py)")
    r.raise_for_status()
    return r.json()["access_token"]


def _make_probe(method: str, path: str, needs_auth: bool, token: str):
    def _probe():
        headers = {"Authorization": f"Bearer {token}"} if needs_auth else {}
        r = requests.request(method, f"{API}{path}", headers=headers, timeout=PROBE_TIMEOUT)
        # Some routes (e.g. brand-kit pre-creation) legitimately return 404 — treat as ok-empty.
        if r.status_code in (200, 201, 204, 404):
            return f"HTTP {r.status_code}"
        raise RuntimeError(f"HTTP {r.status_code} — {r.text[:200]}")

    return _probe


def run() -> list[ProbeResult]:
    results: list[ProbeResult] = []
    try:
        token = _login()
    except SkipProbe as s:
        return [ProbeResult(layer="routes", module="login", status="skip", note=str(s))]
    except Exception as e:
        return [ProbeResult(layer="routes", module="login", status="fail", error=f"{type(e).__name__}: {e}")]

    results.append(ProbeResult(layer="routes", module="login", status="ok", note="seed admin authenticated"))
    for label, method, path, needs_auth in PROBES:
        results.append(run_probe("routes", label, _make_probe(method, path, needs_auth, token)))
    return results
