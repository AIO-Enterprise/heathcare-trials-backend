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

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./marketing_platform.db"

    # Anthropic (direct API)
    ANTHROPIC_API_KEY: str | None = None
    ANTHROPIC_MODEL: str = "claude-sonnet-4-5-20250514"
    # Opus 4.5 — used exclusively for ad strategy generation (curator)
    ANTHROPIC_CURATOR_MODEL: str = "claude-opus-4-5"

    # AWS Bedrock
    USE_BEDROCK: bool = False
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None
    AWS_REGION: str = "us-east-1"
    BEDROCK_MODEL: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    # Opus 4.5 Bedrock ID — used exclusively for ad strategy generation (curator)
    BEDROCK_CURATOR_MODEL: str = "us.anthropic.claude-opus-4-5-20251101-v1:0"

    # Chat widget model (Haiku — lightweight, fast, stays within Bedrock)
    CHAT_MODEL: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    # Azure AI Foundry / Azure OpenAI
    # Set AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY to use Azure Foundry.
    # Leave blank to fall back to standard OpenAI (OPENAI_API_KEY).
    AZURE_OPENAI_ENDPOINT: str | None = None
    AZURE_OPENAI_API_KEY: str | None = None
    AZURE_OPENAI_API_VERSION: str = "2025-05-01-preview"
    # Azure OpenAI Chat (GPT-5) — separate resource from image gen if needed
    # If blank, falls back to AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY
    AZURE_CHAT_ENDPOINT: str | None = None
    AZURE_CHAT_API_KEY: str | None = None
    AZURE_CHAT_DEPLOYMENT: str = "gpt-5"
    # Standard OpenAI fallback
    OPENAI_API_KEY: str | None = None
    OPENAI_IMAGE_MODEL: str = "gpt-image-1.5"

    # Meta for Developers – OAuth credentials for Facebook/Instagram ad publishing
    # Register your app at https://developers.facebook.com and add the Marketing API product.
    META_APP_ID: str | None = None
    META_APP_SECRET: str | None = None
    META_OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/platform-connections/meta/callback"

    # Google Ads – OAuth credentials for Google Ads publishing
    # Create credentials at https://console.cloud.google.com → APIs & Services → Credentials
    # Enable the Google Ads API and obtain a developer token from your Google Ads manager account.
    GOOGLE_ADS_CLIENT_ID: str | None = None
    GOOGLE_ADS_CLIENT_SECRET: str | None = None
    GOOGLE_ADS_DEVELOPER_TOKEN: str | None = None
    GOOGLE_ADS_OAUTH_REDIRECT_URI: str = "http://localhost:8001/api/platform-connections/google/callback"

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

    # Public-facing base URL — used to register post-call webhook with ElevenLabs.
    # Example: https://api.yourdomain.com  (no trailing slash)
    APP_PUBLIC_URL: str | None = None

    # Backend public URL — used to prefix /outputs/ media URLs so they resolve
    # across origins (e.g. Azure Static Web App → Container App).
    # Leave blank on AWS where CloudFront/nginx proxies everything through one origin.
    # Example: https://marketingai-backend.wonderfulpond-9e50895b.eastus2.azurecontainerapps.io
    BACKEND_PUBLIC_URL: str | None = None

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
