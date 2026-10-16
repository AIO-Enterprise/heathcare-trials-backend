"""Layer 2: external provider probes — Bedrock, OpenAI, ElevenLabs, Google Ads, Meta."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List

import requests

from ..config import BACKEND_DIR, PROBE_TIMEOUT, REPO_ROOT
from .base import ProbeResult, SkipProbe, run_probe


def _bedrock():
    probe = BACKEND_DIR / "test_bedrock.py"
    if not probe.exists():
        probe = REPO_ROOT / "test_bedrock.py"  # backend IS the repo root here
    if not probe.exists():
        raise SkipProbe("test_bedrock.py not present")
    out = subprocess.run(
        [sys.executable, str(probe)],
        cwd=str(probe.parent),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if out.returncode != 0:
        tail = (out.stdout + out.stderr).strip().splitlines()[-5:]
        raise RuntimeError("\n".join(tail))
    return "BEDROCK_OK reply received"


def _openai():
    """Minimal live Foundry check — this resource exposes no models-list endpoint."""
    url = (os.getenv("AZURE_GENERIC_API_URL") or "").strip()
    key = (os.getenv("AZURE_GENERIC_API_KEY") or "").strip()
    model = (os.getenv("AZURE_GENERIC_MODEL") or "").strip()
    if not (url and key and model):
        raise SkipProbe("AZURE_GENERIC_API_URL / AZURE_GENERIC_API_KEY / AZURE_GENERIC_MODEL not set")
    from openai import OpenAI

    base = url[: url.index("/openai/v1") + len("/openai/v1")] if "/openai/v1" in url else url.rstrip("/") + "/openai/v1"
    r = OpenAI(base_url=base, api_key=key).responses.create(
        model=model,
        input="Reply with exactly: probe-ok",
        max_output_tokens=32,  # this deployment rejects values < 16
    )
    text = (r.output_text or "").strip()
    if "probe-ok" not in text:
        raise RuntimeError(f"unexpected reply: {text[:80]}")
    return f"reply: {text[:30]}"


def _elevenlabs():
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        raise SkipProbe("ELEVENLABS_API_KEY not set")
    r = requests.get(
        "https://api.elevenlabs.io/v1/voices",
        headers={"xi-api-key": key},
        timeout=PROBE_TIMEOUT,
    )
    r.raise_for_status()
    n = len(r.json().get("voices", []))
    return f"{n} voices available"


def _google_ads_oauth():
    cid = os.getenv("GOOGLE_CLIENT_ID") or os.getenv("GOOGLE_ADS_CLIENT_ID")
    csec = os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv("GOOGLE_ADS_CLIENT_SECRET")
    if not cid or not csec:
        raise SkipProbe("GOOGLE_ADS_CLIENT_ID / GOOGLE_ADS_CLIENT_SECRET not set")
    r = requests.post(
        "https://oauth2.googleapis.com/tokeninfo",
        params={"client_id": cid},
        timeout=PROBE_TIMEOUT,
    )
    if r.status_code in (200, 400):
        return "OAuth endpoint reachable"
    raise RuntimeError(f"unexpected status {r.status_code}")


def _meta_oauth():
    app_id = os.getenv("META_APP_ID") or os.getenv("FACEBOOK_APP_ID")
    if not app_id:
        raise SkipProbe("META_APP_ID not set")
    r = requests.get(
        f"https://graph.facebook.com/v18.0/{app_id}",
        timeout=PROBE_TIMEOUT,
    )
    if r.status_code in (200, 400):
        return "Graph API reachable"
    raise RuntimeError(f"unexpected status {r.status_code}")


def run() -> list[ProbeResult]:
    return [
        run_probe("providers", "bedrock", _bedrock),
        run_probe("providers", "openai", _openai),
        run_probe("providers", "elevenlabs", _elevenlabs),
        run_probe("providers", "google_ads", _google_ads_oauth),
        run_probe("providers", "meta_ads", _meta_oauth),
    ]
