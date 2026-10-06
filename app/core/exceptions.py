"""
Backend exception hierarchy.

Every domain/backend error derives from AppError so callers can catch the
broadest application error without swallowing genuine bugs (TypeError,
KeyError, etc.). HTTP mapping happens in one place (main.py handlers), not at
each raise site.
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for all expected application errors."""

    #: Default HTTP status used when an error is surfaced to a client.
    status_code = 500

    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.detail = detail or message

    @property
    def code(self) -> str:
        """Stable machine-readable identifier (the class name)."""
        return type(self).__name__

    @property
    def message(self) -> str:
        """The human-readable message (args[0])."""
        return str(self)


class ValidationError(AppError):
    """Client input failed domain validation beyond schema-level checks."""

    status_code = 422


class NotFoundError(AppError):
    """Requested entity does not exist (or is not visible to the caller)."""

    status_code = 404


class PermissionDeniedError(AppError):
    """Authenticated caller is not allowed to perform this action."""

    status_code = 403


class ExternalServiceError(AppError):
    """An upstream dependency (AI provider, ad platform, SMS…) failed."""

    status_code = 502

    def __init__(
        self,
        service: str,
        message: str,
        *,
        status_code: int = 502,
        detail: str | None = None,
    ):
        super().__init__(f"{service}: {message}", detail=detail)
        self.service = service
        self.status_code = status_code


class ConfigurationError(AppError):
    """Required configuration is missing or invalid."""

    status_code = 500


class RateLimitError(ExternalServiceError):
    """Upstream dependency rejected the call due to rate limiting."""

    def __init__(self, service: str, message: str = "rate limited", *, detail: str | None = None):
        super().__init__(service, message, status_code=429, detail=detail)
