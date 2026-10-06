"""Probe result type + helper to safely run a probe callable."""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ProbeResult:
    layer: str
    module: str
    status: str
    duration_ms: int = 0
    error: str | None = None
    note: str | None = None


def run_probe(layer: str, module: str, fn: Callable[[], str | None]) -> ProbeResult:
    started = time.perf_counter()
    try:
        note = fn()
        return ProbeResult(
            layer=layer,
            module=module,
            status="ok",
            duration_ms=int((time.perf_counter() - started) * 1000),
            note=note,
        )
    except SkipProbe as s:
        return ProbeResult(
            layer=layer,
            module=module,
            status="skip",
            duration_ms=int((time.perf_counter() - started) * 1000),
            note=str(s),
        )
    except Exception as e:
        return ProbeResult(
            layer=layer,
            module=module,
            status="fail",
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}",
        )


class SkipProbe(Exception):
    """Raise inside a probe to mark it as skipped (e.g. missing credentials)."""
