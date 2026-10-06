"""
Fusion TTS — dual-model ElevenLabs synthesis.

Architecture:
  1. Response Router   — splits response text into (opener, continuation)
  2. Flash Opener      — synthesised with eleven_flash_v2 (~75 ms TTFB)
  3. Expressive Cont.  — synthesised with eleven_v3 in parallel (~600–1200 ms)
  4. TTS Stream Mgr    — RMS-normalises both, stitches, yields PCM chunks

Audio format throughout: PCM 16 000 Hz, 16-bit signed little-endian, mono.
No external numeric libraries required — normalisation uses struct + math.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import struct
from collections.abc import AsyncGenerator

import httpx

from app.core.config import settings
from app.core.exceptions import ExternalServiceError

logger = logging.getLogger(__name__)

_ELEVENLABS_TTS = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"

# Audio tags supported by eleven_v3 but spoken literally / rejected by eleven_flash_v2.
# Matches both XML-style (<break time="0.5s">) and square-bracket ([laugh], [chuckle]).
_AUDIO_TAG_RE = re.compile(r"<[^>]+>|\[[^\]]+\]")

# Sentence-ending punctuation — preferred split points
_SENTENCE_END_RE = re.compile(r"[.!?;](?:\s|$)")

# Target RMS ratio for loudness normalisation (fraction of max int16 amplitude)
_TARGET_RMS_RATIO = 0.25
# Never amplify by more than this factor (prevents clipping on quiet segments)
_MAX_GAIN = 3.0

# ── Voice settings applied uniformly to both models ──────────────────────────
_VOICE_SETTINGS = {
    "stability": 0.55,
    "similarity_boost": 0.82,
    "style": 0.35,
    "use_speaker_boost": False,
}


# ─────────────────────────────────────────────────────────────────────────────
# Text splitting
# ─────────────────────────────────────────────────────────────────────────────


def split_for_fusion(text: str) -> tuple[str, str]:
    """
    Split *text* into (opener, continuation).

    - opener      → stripped of audio tags → eleven_flash_v2_5
    - continuation → audio tags preserved  → eleven_v3

    Returns (full_text_stripped, "") when the response is too short to split
    so the caller always gets valid strings (never empty opener).
    """
    text = text.strip()
    if not text:
        return "", ""

    words = text.split()
    min_w = settings.FUSION_OPENER_MIN_WORDS
    max_w = settings.FUSION_OPENER_MAX_WORDS

    if len(words) <= min_w:
        # Short response — flash only, strip tags for compatibility
        return _AUDIO_TAG_RE.sub("", text).strip(), ""

    # Prefer a natural sentence boundary within the word-count window
    for i in range(min_w, min(max_w + 1, len(words))):
        partial = " ".join(words[: i + 1])
        if _SENTENCE_END_RE.search(partial):
            opener = _AUDIO_TAG_RE.sub("", partial).strip()
            continuation = " ".join(words[i + 1 :]).strip()
            return opener, continuation

    # No sentence boundary found — hard split at max words
    opener = _AUDIO_TAG_RE.sub("", " ".join(words[:max_w])).strip()
    continuation = " ".join(words[max_w:]).strip()
    return opener, continuation


# ─────────────────────────────────────────────────────────────────────────────
# PCM utilities
# ─────────────────────────────────────────────────────────────────────────────


def normalize_pcm(pcm_bytes: bytes) -> bytes:
    """
    RMS-normalise raw 16-bit signed little-endian PCM to _TARGET_RMS_RATIO.
    Returns the input unchanged when the segment is silence or empty.
    """
    if len(pcm_bytes) < 2:
        return pcm_bytes

    n = len(pcm_bytes) // 2
    samples = struct.unpack(f"<{n}h", pcm_bytes[: n * 2])

    rms = math.sqrt(sum(s * s for s in samples) / n)
    if rms < 1.0:
        return pcm_bytes  # silence — don't amplify noise

    target_rms = 32767.0 * _TARGET_RMS_RATIO
    scale = min(target_rms / rms, _MAX_GAIN)

    normalised = [max(-32768, min(32767, int(s * scale))) for s in samples]
    return struct.pack(f"<{n}h", *normalised)


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap raw PCM in a minimal RIFF/WAV header (16-bit mono)."""
    channels = 1
    bits = 16
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = len(pcm_bytes)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b"data",
        data_size,
    )
    return header + pcm_bytes


# ─────────────────────────────────────────────────────────────────────────────
# ElevenLabs TTS call
# ─────────────────────────────────────────────────────────────────────────────


async def _fetch_tts_pcm(text: str, voice_id: str, model_id: str) -> bytes:
    """
    POST to ElevenLabs streaming TTS and return all PCM bytes.
    Raises ValueError on non-2xx responses.
    """
    if not text:
        return b""

    url = _ELEVENLABS_TTS.format(voice_id=voice_id)
    # output_format is a query parameter, not a body field — body-only sends are ignored
    # and ElevenLabs returns MP3 (default), which sounds like static when played as PCM.
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": _VOICE_SETTINGS,
    }
    headers = {
        "xi-api-key": settings.ELEVENLABS_API_KEY or "",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream(
            "POST",
            url,
            json=payload,
            headers=headers,
            params={"output_format": "pcm_16000"},
        ) as resp:
            if not resp.is_success:
                body = await resp.aread()
                logger.error(
                    "ElevenLabs TTS %s [%s]: %s", resp.status_code, model_id, body.decode(errors="replace")[:400]
                )
                raise ExternalServiceError("elevenlabs-tts", f"synthesis failed ({resp.status_code})")
            chunks: list[bytes] = []
            async for chunk in resp.aiter_bytes(4096):
                chunks.append(chunk)
            return b"".join(chunks)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


async def stream_fusion_tts(
    text: str,
    voice_id: str,
    flash_model: str | None = None,
    expressive_model: str | None = None,
    chunk_size: int = 4096,
) -> AsyncGenerator[bytes, None]:
    """
    Yield PCM 16 000 Hz audio chunks using the dual-model fusion strategy.

    Strategy:
      - Opener  → flash_model   (default: eleven_flash_v2_5)
      - Rest    → expressive_model (default: eleven_v3)

    Both synthesis tasks are launched in parallel.  The flash audio (which
    arrives first) is yielded immediately; the expressive continuation is
    queued behind it for seamless playback.

    On any partial failure the function gracefully falls back:
      - Flash fails  → full response sent to expressive model
      - V3 fails     → opener already delivered, turn ends naturally
    """
    _flash = flash_model or settings.ELEVENLABS_FLASH_MODEL
    _expr = expressive_model or settings.ELEVENLABS_EXPRESSIVE_MODEL

    opener, continuation = split_for_fusion(text)

    if not opener:
        return

    # ── Short response: flash only ────────────────────────────────────────────
    if not continuation:
        try:
            audio = await _fetch_tts_pcm(opener, voice_id, _flash)
            audio = normalize_pcm(audio)
        except Exception as exc:
            logger.warning("Flash TTS failed for short response, trying expressive: %s", exc)
            try:
                audio = await _fetch_tts_pcm(opener, voice_id, _expr)
                audio = normalize_pcm(audio)
            except Exception as exc2:
                logger.error("Expressive TTS also failed: %s", exc2)
                return
        for i in range(0, len(audio), chunk_size):
            yield audio[i : i + chunk_size]
        return

    # ── Full fusion: launch both tasks in parallel ────────────────────────────
    flash_task = asyncio.create_task(
        _fetch_tts_pcm(opener, voice_id, _flash),
        name="fusion_flash",
    )
    v3_task = asyncio.create_task(
        _fetch_tts_pcm(continuation, voice_id, _expr),
        name="fusion_v3",
    )

    # Wait for flash first (it always arrives sooner)
    try:
        flash_audio = await flash_task
        flash_audio = normalize_pcm(flash_audio)
        logger.debug("Fusion flash ready: %d bytes", len(flash_audio))
    except Exception as exc:
        logger.warning("Flash TTS failed — falling back to expressive-only: %s", exc)
        v3_task.cancel()
        try:
            full_audio = await _fetch_tts_pcm(text, voice_id, _expr)
            full_audio = normalize_pcm(full_audio)
            for i in range(0, len(full_audio), chunk_size):
                yield full_audio[i : i + chunk_size]
        except Exception as exc2:
            logger.error("Expressive fallback also failed: %s", exc2)
        return

    # Yield flash audio immediately — v3 is being synthesised in the background
    for i in range(0, len(flash_audio), chunk_size):
        yield flash_audio[i : i + chunk_size]

    # Now collect and yield the expressive continuation
    try:
        v3_audio = await v3_task
        v3_audio = normalize_pcm(v3_audio)
        logger.debug("Fusion v3 ready: %d bytes", len(v3_audio))
        for i in range(0, len(v3_audio), chunk_size):
            yield v3_audio[i : i + chunk_size]
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        # Opener was already delivered — log and end the turn gracefully
        logger.warning("Expressive continuation failed (opener already played): %s", exc)
