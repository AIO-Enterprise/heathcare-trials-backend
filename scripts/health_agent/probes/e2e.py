"""Layer 5: end-to-end probe — runs tests/seed.py against the live backend."""

from __future__ import annotations

import subprocess
import sys
from typing import List

from ..config import API, REPO_ROOT
from .base import ProbeResult, SkipProbe, run_probe


def _seed_pipeline():
    seed = REPO_ROOT / "tests" / "seed.py"
    if not seed.exists():
        raise SkipProbe("tests/seed.py not present")
    out = subprocess.run(
        [sys.executable, str(seed), "--url", API],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if out.returncode != 0:
        tail = (out.stdout + out.stderr).strip().splitlines()[-8:]
        raise RuntimeError("\n".join(tail))
    return "onboarding -> upload -> train passed"


def run() -> list[ProbeResult]:
    return [run_probe("e2e", "onboarding_pipeline", _seed_pipeline)]
