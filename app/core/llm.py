"""
Provider-agnostic LLM access layer.

Slots mirror 1.env: "generic" (reviewer/optimizer/creative brief/...),
"curator" (strategy), "chat" (widget). Strict per-slot fallback:
AZURE_{SLOT}_* triplet set → Azure Foundry (GPT); else AWS_{SLOT}_MODEL
(Bedrock Claude). No cross-slot inheritance — leave a slot's Azure triplet
empty and that slot runs on Bedrock.

Services depend on the AIClient interface (constructor-injected in tests,
module default in production) instead of building SDK clients inline:

Usage:
    from app.core.llm import get_ai_client

    client = get_ai_client("curator")
    text   = await client.complete(system=system_prompt, messages=[...], max_tokens=2048)
    data   = await client.complete_json(system=..., messages=[...])   # parsed dict

The canonical `extract_json` used by complete_json (and importable directly)
replaces the eight ad-hoc fence-stripping variants that existed previously.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol
from urllib.parse import urlparse

from app.core.config import settings
from app.core.exceptions import ConfigurationError, ExternalServiceError

logger = logging.getLogger(__name__)

_FENCE_OPEN_RE = re.compile(r"^```[a-zA-Z]*\s*")
_FENCE_CLOSE_RE = re.compile(r"\s*```\s*$")

_VALID_SLOTS = ("generic", "curator", "chat")


def _check_slot(slot: str) -> str:
    slot = (slot or "generic").lower()
    if slot not in _VALID_SLOTS:
        raise ValueError(f"unknown LLM slot {slot!r} (expected one of {_VALID_SLOTS})")
    return slot


def model_family(model: str) -> str:
    """
    "openai" for GPT-style models, "anthropic" for Claude-style.
    Reverting providers = changing env model values only; call sites branch
    on this (payload shape, tool schema) instead of on provider settings.
    """
    m = (model or "").strip().lower()
    if m.startswith(("gpt-", "gpt_", "o1", "o3", "o4", "openai.", "openai-")) or "/gpt-" in m:
        return "openai"
    return "anthropic"


def extract_json(text: str) -> dict[str, Any]:
    """
    Canonical multi-stage JSON extractor for LLM output.

      Stage 1: direct parse.
      Stage 2: strip markdown fences then parse.
      Stage 3: slice the outermost { … } block then parse.

    Raises json.JSONDecodeError if every stage fails.
    """
    # Stage 1: raw
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Stage 2: strip markdown fences
    try:
        clean = _FENCE_OPEN_RE.sub("", text.strip())
        clean = _FENCE_CLOSE_RE.sub("", clean).strip()
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    # Stage 3: extract outermost JSON object
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        return json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        pass

    raise json.JSONDecodeError("no JSON object found in LLM output", text, 0)


class AIClient(Protocol):
    """Interface every AI provider adapter must satisfy."""

    async def complete(
        self,
        *,
        system: str | None = None,
        messages: list[dict],
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> str:
        """Return the assistant's plain-text reply."""
        ...

    def is_configured(self) -> bool:
        """True when credentials for this slot exist."""
        ...


def _foundry_base_url(url: str) -> str:
    """
    Base URL for the Foundry OpenAI v1 data plane (responses/images).

    Accepts a bare host ("https://<res>.services.ai.azure.com") or a full
    portal path ("https://<res>.services.ai.azure.com/openai/v1/responses")
    and returns the ".../openai/v1" root the SDK appends paths to.
    """
    url = (url or "").strip()
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else url.rstrip("/")
    path = parsed.path or ""
    if "/openai/v1" in path:
        return root + path[: path.index("/openai/v1") + len("/openai/v1")]
    return root + "/openai/v1"


class AnthropicAIClient:
    """
    Bedrock adapter for one slot.

    The SDK client is created once and reused for the process lifetime —
    building a fresh client per call discarded connection pooling.
    An explicit `client`/`model_resolver` can be injected for tests.
    """

    def __init__(self, slot: str = "generic", client: Any | None = None, model_resolver=None):
        self._slot = _check_slot(slot)
        self._client = client
        self._model_resolver = model_resolver

    def _sdk_client(self):
        if self._client is None:
            from app.core.bedrock import get_async_client

            self._client = get_async_client()
        return self._client

    def _resolve_model(self, override: str | None) -> str:
        if override:
            return override
        if self._model_resolver is not None:
            return self._model_resolver()
        from app.core.bedrock import get_model

        return get_model(self._slot)

    def is_configured(self) -> bool:
        from app.core.bedrock import is_configured as _configured

        return _configured(self._slot)

    async def complete(
        self,
        *,
        system: str | None = None,
        messages: list[dict],
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> str:
        if not self.is_configured():
            raise ConfigurationError(f"No AI provider configured for slot {self._slot!r} (AZURE_{self._slot.upper()}_* / AWS_{self._slot.upper()}_MODEL)")
        kwargs: dict[str, Any] = {
            "model": self._resolve_model(model),
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system is not None:
            kwargs["system"] = system
        try:
            response = await self._sdk_client().messages.create(**kwargs)
            return response.content[0].text
        except ExternalServiceError:
            raise
        except Exception as exc:  # narrow in Phase 4 retry layer
            logger.error("AI completion failed (%s): %s", type(exc).__name__, exc)
            raise ExternalServiceError(
                self._resolve_model(model),
                f"completion failed: {exc}",
            ) from exc


class AzureAIClient:
    """
    Azure Foundry adapter for one slot (OpenAI v1 data plane).

    The resource exposes `{base}/openai/v1/*` (responses, images) with the
    model in the body — not the classic `{base}/openai/deployments/*` path —
    so this uses the OpenAI-compatible client, not AsyncAzureOpenAI.
    """

    def __init__(self, slot: str = "generic", client: Any | None = None):
        self._slot = _check_slot(slot)
        self._client = client

    def _config(self) -> tuple[str, str, str, str]:
        return settings.azure_slot(self._slot)

    def is_configured(self) -> bool:
        return settings.is_azure_configured(self._slot)

    def _sdk_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            url, key, _, _ = self._config()
            self._client = AsyncOpenAI(
                base_url=_foundry_base_url(url),
                api_key=key,
            )
        return self._client

    async def complete(
        self,
        *,
        system: str | None = None,
        messages: list[dict],
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> str:
        _, _, _, deployment = self._config()
        if not self.is_configured():
            raise ConfigurationError(f"No AI provider configured for slot {self._slot!r} (AZURE_{self._slot.upper()}_* / AWS_{self._slot.upper()}_MODEL)")
        kwargs: dict[str, Any] = {
            "model": model or deployment,
            "input": [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages],
            "max_output_tokens": max_tokens,
        }
        if system is not None:
            kwargs["instructions"] = system
        try:
            response = await self._sdk_client().responses.create(**kwargs)
            return response.output_text or ""
        except ExternalServiceError:
            raise
        except Exception as exc:
            logger.error("AI completion failed (%s): %s", type(exc).__name__, exc)
            raise ExternalServiceError(
                model or deployment,
                f"completion failed: {exc}",
            ) from exc


_default_clients: dict[str, AIClient] = {}


def get_ai_client(slot: str = "generic") -> AIClient:
    """
    Process-wide default AIClient per slot (lazy).
    Azure triplet set → Azure; else Bedrock (unconfigured when both empty,
    preserving the mock fallbacks at call sites).
    """
    slot = _check_slot(slot)
    client = _default_clients.get(slot)
    if client is None:
        client = AzureAIClient(slot) if settings.is_azure_configured(slot) else AnthropicAIClient(slot)
        _default_clients[slot] = client
    return client


def set_ai_client(client: AIClient | None, slot: str = "generic") -> None:
    """Override the default client for a slot (tests / alternative providers)."""
    slot = _check_slot(slot)
    if client is None:
        _default_clients.pop(slot, None)
    else:
        _default_clients[slot] = client


async def close_azure_clients() -> None:
    """Close cached Azure clients on shutdown (no-op if never created)."""
    for slot, client in list(_default_clients.items()):
        sdk = getattr(client, "_client", None)
        if isinstance(client, AzureAIClient) and sdk is not None:
            try:
                await sdk.close()
            finally:
                _default_clients.pop(slot, None)
