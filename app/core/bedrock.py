"""
Bedrock client factory.
Returns the right sync or async Anthropic client based on USE_BEDROCK config.

Usage (async services — curator, reviewer, optimizer, reinforcement):
    from app.core.bedrock import get_async_client, get_model
    client   = get_async_client()
    response = await client.messages.create(model=get_model(), ...)

Usage (sync services — trainer):
    from app.core.bedrock import get_client, get_model
    client   = get_client()
    response = client.messages.create(model=get_model(), ...)
"""

from app.core.config import settings


def is_configured() -> bool:
    """True if any AI backend is configured."""
    return settings.USE_BEDROCK or bool(settings.ANTHROPIC_API_KEY)


def _bedrock_kwargs() -> dict:
    """
    Build keyword args for AnthropicBedrock clients.
    Only pass explicit credentials when set — otherwise let boto3 resolve
    them via its default chain (IAM role, ~/.aws/credentials, Secrets Manager
    sidecar, ECS task role, instance metadata, etc.).
    """
    kwargs = {"aws_region": settings.AWS_REGION}
    if settings.AWS_ACCESS_KEY_ID:
        kwargs["aws_access_key"] = settings.AWS_ACCESS_KEY_ID
    if settings.AWS_SECRET_ACCESS_KEY:
        kwargs["aws_secret_key"] = settings.AWS_SECRET_ACCESS_KEY
    return kwargs


def get_client():
    """Synchronous client — use in blocking/trainer contexts."""
    if settings.USE_BEDROCK:
        from anthropic import AnthropicBedrock

        return AnthropicBedrock(**_bedrock_kwargs())
    from anthropic import Anthropic

    return Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def get_async_client():
    """Async client — use in async service methods."""
    if settings.USE_BEDROCK:
        from anthropic import AsyncAnthropicBedrock

        return AsyncAnthropicBedrock(**_bedrock_kwargs())
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)


def get_model() -> str:
    """Return the correct model ID for the active backend."""
    return settings.BEDROCK_MODEL if settings.USE_BEDROCK else settings.ANTHROPIC_MODEL


def get_curator_model() -> str:
    """Return Opus 4.6 model ID — used exclusively for ad strategy generation."""
    return settings.BEDROCK_CURATOR_MODEL if settings.USE_BEDROCK else settings.ANTHROPIC_CURATOR_MODEL
