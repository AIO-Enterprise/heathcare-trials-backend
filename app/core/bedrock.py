"""
Bedrock client factory — per-slot models from 1.env (AWS_{SLOT}_MODEL).

Strict per-slot fallback lives in app.core.llm: an AZURE_{SLOT}_* triplet
set → Azure; else the AWS_{SLOT}_MODEL here. No cross-slot inheritance.

Usage (async services):
    from app.core.bedrock import get_async_client, get_model
    client   = get_async_client()
    response = await client.messages.create(model=get_model("curator"), ...)

Usage (sync services — trainer):
    from app.core.bedrock import get_client, get_model
    client   = get_client()
    response = client.messages.create(model=get_model("generic"), ...)
"""

from app.core.config import settings

_VALID_SLOTS = ("generic", "curator", "chat")


def _check_slot(slot: str) -> str:
    slot = (slot or "generic").lower()
    if slot not in _VALID_SLOTS:
        raise ValueError(f"unknown LLM slot {slot!r} (expected one of {_VALID_SLOTS})")
    return slot


def is_configured(slot: str = "generic") -> bool:
    """True when this slot has a Bedrock model (creds optional — boto chain/IAM role)."""
    return bool(get_model(slot))


def _bedrock_kwargs() -> dict:
    """
    Build keyword args for AnthropicBedrock clients.
    Only pass explicit credentials when set — otherwise let boto3 resolve
    them via its default chain (IAM role, ~/.aws/credentials, Secrets Manager
    sidecar, ECS task role, instance metadata, etc.).
    """
    kwargs = {"aws_region": settings.AWS_REGION or "us-east-1"}
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_ACCESS_KEY_ID.strip():
        kwargs["aws_access_key"] = settings.AWS_ACCESS_KEY_ID.strip()
    if settings.AWS_SECRET_ACCESS_KEY and settings.AWS_SECRET_ACCESS_KEY.strip():
        kwargs["aws_secret_key"] = settings.AWS_SECRET_ACCESS_KEY.strip()
    return kwargs


# Module-level singletons — one connection pool per process instead of a
# fresh pool on every AI call.
_sync_client = None
_async_client = None


def get_client():
    """Synchronous Bedrock client — use in blocking/trainer contexts."""
    global _sync_client
    if _sync_client is None:
        from anthropic import AnthropicBedrock

        _sync_client = AnthropicBedrock(**_bedrock_kwargs())
    return _sync_client


def get_async_client():
    """Async Bedrock client — use in async service methods."""
    global _async_client
    if _async_client is None:
        from anthropic import AsyncAnthropicBedrock

        _async_client = AsyncAnthropicBedrock(**_bedrock_kwargs())
    return _async_client


async def close_clients() -> None:
    """Close cached Bedrock clients on shutdown (no-op if never created)."""
    global _async_client
    if _async_client is not None:
        try:
            await _async_client.close()
        finally:
            _async_client = None
    from app.core.llm import close_azure_clients

    await close_azure_clients()


def get_model(slot: str = "generic") -> str:
    """Return the Bedrock model ID for a slot ("" when unset)."""
    return settings.bedrock_model(_check_slot(slot))


def get_curator_model() -> str:
    """Return the curator-tier Bedrock model ID."""
    return get_model("curator")
