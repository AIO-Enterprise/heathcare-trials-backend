"""
M2: Core Configuration
Owner: Backend Dev 1

Central config loaded from environment variables.
"""

import os
from typing import Literal

from pydantic_settings import BaseSettings

# Resolve .env regardless of which directory the server is started from.
# Checks: backend/.env → project-root/.env → one further up.
_HERE = os.path.dirname(os.path.abspath(__file__))  # .../backend/app/core
_ENV_CANDIDATES = (
    os.path.join(_HERE, "..", "..", ".env"),  # backend/.env
    os.path.join(_HERE, "..", "..", "..", ".env"),  # project-root/.env
    os.path.join(_HERE, "..", "..", "..", "..", ".env"),  # one above root
)
_ENV_FILE = next((p for p in _ENV_CANDIDATES if os.path.exists(p)), ".env")


class Settings(BaseSettings):
    # App
    APP_NAME: str = "Marketing Analytics Platform"
    APP_ENV: Literal["dev", "prod"] = "dev"  # dev → INFO logs, prod → WARNING logs
    DEBUG: bool = False
    SECRET_KEY: str = "CHANGE-ME-in-production-use-openssl-rand-hex-32"

    # Default IANA timezone for campaign-local wall times (booking slots,
    # pause schedules, reminders) when an ad does not override bot_config.timezone
    DEFAULT_CAMPAIGN_TIMEZONE: str = "Australia/Sydney"

    # JWT
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440  # 24 hours — proactively refreshed by frontend

    # Auth cookie transport. Dev default (lax + auto-secure) requires
    # same-origin API calls (Vite proxy) — cross-site fetch silently drops
    # Lax cookies, which looks like "login works then kicks me out".
    # Split-origin prod (e.g. Static Web App → Container App) needs
    # COOKIE_SAMESITE=none (forces Secure).
    COOKIE_SAMESITE: str = "lax"
    # None → Secure only in prod; True/False override explicitly.
    COOKIE_SECURE: bool | None = None

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./marketing_platform.db"

    # Azure Foundry (GPT side) — per-activity triplets.
    # Strict per-slot fallback: AZURE_{SLOT}_* set → Azure, else AWS_{SLOT}_MODEL.
    # No cross-slot inheritance. AZURE_IMAGE_GEN_* drives image generation.
    AZURE_CURATOR_API_URL: str = ""
    AZURE_CURATOR_API_KEY: str = ""
    AZURE_CURATOR_API_VERSION: str = ""
    AZURE_CURATOR_MODEL: str = ""
    AZURE_GENERIC_API_URL: str = ""
    AZURE_GENERIC_API_KEY: str = ""
    AZURE_GENERIC_API_VERSION: str = ""
    AZURE_GENERIC_MODEL: str = ""
    AZURE_CHAT_API_URL: str = ""
    AZURE_CHAT_API_KEY: str = ""
    AZURE_CHAT_API_VERSION: str = ""
    AZURE_CHAT_MODEL: str = ""
    AZURE_IMAGE_GEN_API_URL: str = ""
    AZURE_IMAGE_GEN_API_KEY: str = ""
    AZURE_IMAGE_GEN_VERSION: str = ""
    AZURE_IMAGE_GEN_MODEL: str = ""

    # AWS Bedrock (Claude side) — shared creds, per-activity models.
    # Credentials optional: boto3 falls back to its default chain
    # (IAM role, ~/.aws/credentials, ECS task role, instance metadata).
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None
    AWS_REGION: str = "us-east-1"
    AWS_CURATOR_MODEL: str = ""
    AWS_GENERIC_MODEL: str = ""
    AWS_CHAT_MODEL: str = ""
    # Reserved — Bedrock serves no images; image gen reads AZURE_IMAGE_GEN_* only.
    AWS_IMAGE_GEN_MODEL: str = ""

    # Default Azure API version when a slot's VERSION field is blank.
    AZURE_API_VERSION_DEFAULT: str = "2025-05-01-preview"

    def _clean(self, value: str | None) -> str:
        return (value or "").strip()

    def azure_slot(self, slot: str) -> tuple[str, str, str, str]:
        """Return (url, key, version, model) for a slot: curator|generic|chat."""
        prefix = f"AZURE_{slot.upper()}"
        url = self._clean(getattr(self, f"{prefix}_API_URL"))
        key = self._clean(getattr(self, f"{prefix}_API_KEY"))
        version = self._clean(getattr(self, f"{prefix}_API_VERSION")) or self.AZURE_API_VERSION_DEFAULT
        model = self._clean(getattr(self, f"{prefix}_MODEL"))
        return url, key, version, model

    def is_azure_configured(self, slot: str) -> bool:
        url, key, _, model = self.azure_slot(slot)
        return bool(url and key and model)

    def bedrock_model(self, slot: str) -> str:
        """Return the Bedrock model for a slot: curator|generic|chat."""
        return self._clean(getattr(self, f"AWS_{slot.upper()}_MODEL", ""))

    @property
    def backend_base(self) -> str:
        """Canonical backend base URL (no trailing slash) — "" when unset."""
        return self._clean(self.BACKEND_HOSTING_URL).rstrip("/")

    def absolute_url(self, path_or_url: str) -> str:
        """Join BACKEND_HOSTING_URL with a relative path; pass absolute URLs through."""
        value = self._clean(path_or_url)
        if value.startswith("http://") or value.startswith("https://"):
            return value
        base = self.backend_base
        if not base:
            return value
        return f"{base}/{value.lstrip('/')}"

    @property
    def meta_redirect_uri(self) -> str:
        return self.absolute_url(self.META_OAUTH_REDIRECT_URI)

    @property
    def google_redirect_uri(self) -> str:
        return self.absolute_url(self.GOOGLE_ADS_OAUTH_REDIRECT_URI)

    # Meta for Developers – OAuth credentials for Facebook/Instagram ad publishing
    # Register your app at https://developers.facebook.com and add the Marketing API product.
    META_APP_ID: str | None = None
    META_APP_SECRET: str | None = None
    META_OAUTH_REDIRECT_URI: str = "/api/platform-connections/meta/callback"

    # Google Ads – OAuth credentials for Google Ads publishing
    # Create credentials at https://console.cloud.google.com → APIs & Services → Credentials
    # Enable the Google Ads API and obtain a developer token from your Google Ads manager account.
    GOOGLE_ADS_CLIENT_ID: str | None = None
    GOOGLE_ADS_CLIENT_SECRET: str | None = None
    GOOGLE_ADS_DEVELOPER_TOKEN: str | None = None
    GOOGLE_ADS_OAUTH_REDIRECT_URI: str = "/api/platform-connections/google/callback"

    # ElevenLabs
    ELEVENLABS_API_KEY: str | None = None
    ELEVENLABS_VOICE_ID: str = "EXAVITQu4vr4xnSDxMaL"  # Default: Rachel
    ELEVENLABS_PHONE_NUMBER_ID: str | None = None  # Phone number ID for outbound calls
    # TTS model for ElevenLabs ConvAI agents — flash for <100ms latency + natural disfluencies
    # eleven_flash_v2_5 is rejected by ConvAI for English agents; eleven_flash_v2 is required
    ELEVENLABS_TTS_MODEL: str = "eleven_flash_v2"
    ELEVENLABS_WEBHOOK_SECRET: str | None = None  # HMAC secret set on the ElevenLabs agent

    # ── Fusion TTS models (dual-model hybrid voice pipeline) ─────────────────
    # Flash model: ultra-low latency (~75ms TTFB) — used for the opener phrase.
    # No expressive audio tag support; audio tags are stripped before synthesis.
    ELEVENLABS_FLASH_MODEL: str = "eleven_flash_v2"
    # Expressive model: richer emotional delivery — used for the continuation.
    # Supports <laugh>, <sigh>, <break> and other audio tags.
    ELEVENLABS_EXPRESSIVE_MODEL: str = "eleven_v3_conversational"
    # Word count range for the flash opener before handing off to expressive model
    FUSION_OPENER_MIN_WORDS: int = 8
    FUSION_OPENER_MAX_WORDS: int = 18

    # Fusion WebSocket voice pipeline settings
    ELEVENLABS_STREAMING_LATENCY: int = 5
    ELEVENLABS_VAD_THRESHOLD: float = 0.35
    ELEVENLABS_SILENCE_MS: int = 550
    ELEVENLABS_PREFIX_PADDING_MS: int = 150
    ELEVENLABS_INTERRUPTION_THRESHOLD: float = 0.3
    # Server-side VAD for hybrid pipeline: RMS energy threshold (0–32767 scale)
    FUSION_VAD_RMS_THRESHOLD: int = 400
    # Silence duration (ms) after which end-of-speech is triggered
    FUSION_VAD_SILENCE_MS: int = 700

    # Backend base URL — joined with relative paths (OAuth callbacks, media URLs,
    # WS URLs, generated-page API_BASE) to build absolute URLs in split-origin
    # deployments (e.g. Azure Static Web App → Container App).
    # Leave blank for single-origin dev/AWS where relative URLs suffice.
    # Example: https://backend.example.com  (no trailing slash)
    BACKEND_HOSTING_URL: str = ""

    # Frontend origins allowed to call the API with cookies (comma-separated).
    # Required for cookie auth — browsers reject allow_credentials with "*".
    # Example: https://app.example.com,http://localhost:5173
    ALLOWED_ORIGINS: str = ""

    # Email (SMTP) — leave blank to log OTP to console instead of sending
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""  # defaults to SMTP_USER if blank

    # SMS (Twilio) — leave TWILIO_ACCOUNT_SID blank to log messages to console
    # instead of sending (dev mode, mirrors the SMTP fallback above).
    # Uses Twilio's REST API directly via httpx — no twilio SDK required.
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""  # E.164, e.g. +18885551234
    # Set to a WhatsApp-enabled sender ("whatsapp:+14155238886") to send via
    # WhatsApp instead of SMS. Leave blank for plain SMS.
    TWILIO_WHATSAPP_FROM: str = ""
    # Appointment reminders: send an SMS this many hours before the slot.
    SMS_REMINDER_HOURS_BEFORE: int = 24
    # How often the reminder sweeper wakes up, in seconds.
    SMS_REMINDER_SWEEP_SECONDS: int = 300

    # File storage
    UPLOAD_DIR: str = "./uploads"
    OUTPUT_DIR: str = "./outputs"
    STATIC_DIR: str = "./static"
    STATIC_URL: str = "/static"

    # Storage backend for user assets (images, html, videos, docs, logos).
    # "local" (disk under UPLOAD_DIR/OUTPUT_DIR/STATIC_DIR), "s3" (AWS S3),
    # or "azure" (Azure Blob Storage). Stored path values ("/uploads/...",
    # "/outputs/...") are identical for every backend — only bytes move.
    STORAGE_BACKEND: str = "local"
    # Bucket (S3) or container (Azure). Falls back to S3_UPLOAD_BUCKET when blank.
    STORAGE_BUCKET: str = ""
    # Optional key prefix inside the bucket/container, e.g. "prod".
    STORAGE_PREFIX: str = ""
    # Azure Blob — connection string; S3 reuses AWS_REGION + AWS_* keys above.
    AZURE_STORAGE_CONNECTION_STRING: str = ""

    # S3 — optional. When set, protocol document uploads use S3 pre-signed URLs
    # so files bypass CloudFront WAF body-size limits entirely.
    # Leave blank for local/dev — falls back to direct multipart upload.
    S3_UPLOAD_BUCKET: str | None = None
    S3_UPLOAD_PREFIX: str = "ad-documents"  # key prefix inside the bucket

    class Config:
        env_file = _ENV_FILE
        extra = "ignore"  # ignore VITE_* and other frontend-only vars in .env


settings = Settings()
