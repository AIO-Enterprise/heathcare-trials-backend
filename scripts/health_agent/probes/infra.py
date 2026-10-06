"""Layer 1: infrastructure probes — Docker, DB, backend, frontend reachability."""

from __future__ import annotations

import subprocess
import sys
from typing import List

import requests

from ..config import API, BACKEND_DIR, BACKEND_URL, FRONTEND_URL, PROBE_TIMEOUT, REPO_ROOT
from .base import ProbeResult, SkipProbe, run_probe


def _docker_stack():
    cmd = ["docker", "compose", "ps", "--format", "json"]
    out = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "docker compose ps failed")
    body = out.stdout.strip()
    if not body:
        raise RuntimeError("no docker compose services running")
    lines = [l for l in body.splitlines() if l.strip()]
    running = sum(1 for l in lines if '"State":"running"' in l)
    return f"{running}/{len(lines)} services running"


def _backend_health():
    r = requests.get(f"{API}/health", timeout=PROBE_TIMEOUT)
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "healthy":
        raise RuntimeError(f"unexpected payload: {body}")
    return f"version {body.get('version', '?')}"


def _frontend_health():
    r = requests.get(FRONTEND_URL, timeout=PROBE_TIMEOUT)
    r.raise_for_status()
    if "<!doctype html" not in r.text.lower() and "<html" not in r.text.lower():
        raise RuntimeError("response did not look like HTML")
    return f"{len(r.content)} bytes"


def _db_integrity():
    diagnose = REPO_ROOT / "diagnose.py"
    if not diagnose.exists():
        raise SkipProbe("diagnose.py not present")
    out = subprocess.run(
        [sys.executable, str(diagnose)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or out.stdout.strip())
    bad = [l for l in out.stdout.splitlines() if "by_name=False" in l and "by_value=False" in l]
    if bad:
        raise RuntimeError(f"{len(bad)} rows with unrecognized enum values")
    return "all enum values valid"


def run() -> list[ProbeResult]:
    return [
        run_probe("infra", "docker_stack", _docker_stack),
        run_probe("infra", "backend_health", _backend_health),
        run_probe("infra", "frontend_health", _frontend_health),
        run_probe("infra", "db_integrity", _db_integrity),
    ]
