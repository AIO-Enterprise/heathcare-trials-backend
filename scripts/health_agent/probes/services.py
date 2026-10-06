"""Layer 4: service-module probes.

Two flavors:
  1. import-test for every service module under backend/app/services
     (catches syntax errors, broken module-level imports)
  2. instantiate + one real call for the standalone API-client wrappers
     (the DB-coupled services are exercised by routes + e2e layers instead)
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import List

from ..config import BACKEND_DIR
from .base import ProbeResult, SkipProbe, run_probe

SERVICE_MODULES = [
    "app.services.ai.curator",
    "app.services.ai.reviewer",
    "app.services.ai.creative",
    "app.services.ai.voicebot_agent",
    "app.services.ai.website_agent",
    "app.services.ai.fusion_tts",
    "app.services.ai.video_compositor",
    "app.services.ai.compositor",
    "app.services.ai.conversation_analyzer",
    "app.services.voice.campaign_worker",
    "app.services.ads.scheduling.meta_scheduler",
    "app.services.ads.scheduling.google_scheduler",
    "app.services.messaging.email_service",
    "app.services.messaging.sms_service",
    "app.services.storage.brand_kit_extractor",
    "app.services.ads.google_ads_service",
    "app.services.ads.meta_ads_service",
    "app.services.storage.storage",
    "app.services.storage.extractor",
    "app.services.optimization.optimizer",
    "app.services.optimization.reinforcement",
    "app.services.training.trainer",
]


def _ensure_backend_path():
    p = str(BACKEND_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)


def _import_probe(modname: str):
    def _probe():
        _ensure_backend_path()
        # Reload-safe: drop cached entry so syntax errors aren't masked by an old import.
        sys.modules.pop(modname, None)
        importlib.import_module(modname)
        return "imported"

    return _probe


def _bedrock_client_call():
    _ensure_backend_path()
    from app.core.bedrock import get_async_client, get_model, is_configured

    if not is_configured():
        raise SkipProbe("USE_BEDROCK=false or AWS creds missing")
    import asyncio

    async def _go():
        client = get_async_client()
        resp = await client.messages.create(
            model=get_model(),
            max_tokens=16,
            messages=[{"role": "user", "content": "Reply: OK"}],
        )
        return resp.content[0].text.strip()

    text = asyncio.run(_go())
    return f"reply: {text[:30]}"


def run() -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for mod in SERVICE_MODULES:
        label = mod.replace("app.services.", "")
        results.append(run_probe("services", label, _import_probe(mod)))
    results.append(run_probe("services", "bedrock_client_call", _bedrock_client_call))
    return results
