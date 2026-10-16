"""
Lightweight in-memory rate limiter for public hot paths (no external deps).

Fixed-window counters keyed by (client IP, bucket). Buckets match a path
prefix plus an optional path suffix, so the expensive *anonymous* endpoints
(login, public survey/booking forms, chat, public voice-call endpoints) can
be throttled independently of authenticated API traffic that shares the same
prefix (e.g. /api/advertisements/{id}/... serves both).

State is per-process — acceptable for the current single-container ECS
service. If the backend ever scales horizontally this should move to Redis.
"""

from __future__ import annotations

import logging
import time

from fastapi import Request, Response
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# bucket name → (path prefixes, matching suffixes | None, max requests, window seconds).
# A bucket matches when the path starts with one of the prefixes AND (ends with
# one of the suffixes when suffixes is not None). Suffix scoping keeps buckets
# off authenticated dashboard traffic sharing the same prefix.
_BUCKETS: dict[str, tuple[tuple[str, ...], tuple[str, ...] | None, int, int]] = {
    "auth": (
        ("/api/auth/login", "/api/auth/register", "/api/auth/reset-password"),
        None,
        20,
        60,
    ),
    # Anonymous survey submission + booking (slots listing / booking / campaign info)
    "public-forms": (
        ("/api/advertisements/",),
        (
            "/survey-responses",
            "/appointments/slots",
            "/appointments",
            "/public",
        ),
        60,
        60,
    ),
    "chat": (
        ("/api/chat",),
        None,
        60,
        60,
    ),
    # Public voice-call endpoints on campaigns (request an outbound call, session token)
    "voice-public": (
        ("/api/advertisements/",),
        ("/voice-call/request", "/voice-session/token"),
        30,
        60,
    ),
    # Anonymous brand-PDF extraction (onboarding runs it pre-signup; each hit
    # can trigger one LLM call, so keep the budget tight).
    "brand-extract": (
        ("/api/brand-kit/extract-pdf",),
        None,
        10,
        60,
    ),
}

# (ip, bucket) → [count, window_start]
_counters: dict[tuple[str, str], list] = {}

_CLEAN_EVERY = 512  # requests between stale-entry sweeps
_request_tick = 0


def _client_ip(request: Request) -> str:
    """
    Best-effort client IP for rate-limit keying.

    Behind CloudFront → ALB the FIRST XFF hop is whatever the client sent and
    is trivially spoofable — rotating it would bypass every limit. Prefer the
    header CloudFront injects itself, then the LAST XFF hop (the nearest
    trusted proxy's view), then the socket peer.
    """
    viewer = request.headers.get("cloudfront-viewer-address")
    if viewer:
        # Format "1.2.3.4:56789" — strip the port.
        return viewer.split(":")[0]
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _match_bucket(path: str) -> tuple[str, int, int] | None:
    for name, (prefixes, suffixes, limit, window) in _BUCKETS.items():
        for prefix in prefixes:
            if not path.startswith(prefix):
                continue
            if suffixes is None or path.endswith(tuple(suffixes)):
                return name, limit, window
    return None


async def rate_limit_middleware(request: Request, call_next) -> Response:
    global _request_tick

    match = _match_bucket(request.url.path)
    if match is None:
        return await call_next(request)

    name, limit, window = match
    key = (_client_ip(request), name)
    now = time.monotonic()

    _request_tick += 1
    if _request_tick % _CLEAN_EVERY == 0:
        stale = [k for k, v in _counters.items() if now - v[1] > 120]
        for k in stale:
            _counters.pop(k, None)

    entry = _counters.get(key)
    if entry is None or now - entry[1] >= window:
        entry = [0, now]
        _counters[key] = entry

    entry[0] += 1
    if entry[0] > limit:
        retry_after = max(1, int(window - (now - entry[1])))
        logger.warning(
            "Rate limit exceeded: ip=%s bucket=%s count=%d limit=%d",
            key[0],
            name,
            entry[0],
            limit,
        )
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Please slow down and try again shortly."},
            headers={"Retry-After": str(retry_after)},
        )

    return await call_next(request)
