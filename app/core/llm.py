"""
Provider-agnostic LLM access layer.

Services depend on the AIClient interface (constructor-injected in tests,
module default in production) instead of building Anthropic SDK clients
inline. Swapping providers means writing one new adapter — not editing every
call site.

Usage:
    from app.core.llm import get_ai_client

    client = get_ai_client()
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

from app.core.config import settings
from app.core.exceptions import ConfigurationError, ExternalServiceError

logger = logging.getLogger(__name__)

_FENCE_OPEN_RE = re.compile(r"^```[a-zA-Z]*\s*")
_FENCE_CLOSE_RE = re.compile(r"\s*```\s*$")


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
        """True when credentials for this provider exist."""
        ...


class AnthropicAIClient:
    """
    Default adapter over the Bedrock/Anthropic factory in core.bedrock.

    The SDK client is created once and reused for the process lifetime —
    building a fresh client per call discarded connection pooling.
    An explicit `client`/`model_resolver` can be injected for tests.
    """

    def __init__(self, client: Any | None = None, model_resolver=None):
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

        return get_model()

    def is_configured(self) -> bool:
        from app.core.bedrock import is_configured as _configured

        return _configured()

    async def complete(
        self,
        *,
        system: str | None = None,
        messages: list[dict],
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> str:
        if not self.is_configured():
            raise ConfigurationError("No AI provider configured (USE_BEDROCK / ANTHROPIC_API_KEY)")
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
                settings.BEDROCK_MODEL if settings.USE_BEDROCK else "anthropic",
                f"completion failed: {exc}",
            ) from exc


_default_client: AnthropicAIClient | None = None


def get_ai_client() -> AIClient:
    """Process-wide default AIClient (lazy)."""
    global _default_client
    if _default_client is None:
        _default_client = AnthropicAIClient()
    return _default_client


def set_ai_client(client: AIClient | None) -> None:
    """Override the default client (tests / alternative providers)."""
    global _default_client
    _default_client = client
