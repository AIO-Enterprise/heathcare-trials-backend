"""
Hybrid Voice Session — custom WebSocket conversation pipeline.

TTS strategy (single-model per utterance — no ensemble):
  • Fillers  → eleven_flash_v2          (~75 ms TTFB, buffered, short phrases)
  • Greeting → eleven_flash_v2          (fast start when patient connects)
  • Responses→ eleven_v3_conversational (EL streaming WS, first chunk ~200 ms
                                         after first LLM token — snappy gap)

Architecture (this rewrite):
  • A background `mic_task` ALWAYS reads PCM from the browser and runs VAD,
    even while the agent is speaking.  That makes true barge-in possible.
  • Completed user-speech segments are pushed onto an asyncio.Queue.
  • The main loop pops segments and launches a cancellable `_agent_turn` task.
  • If the mic_task detects sustained user speech *while the agent is speaking*,
    it sets `interrupt_event`.  The current agent_turn checks this between
    chunks and aborts; a `{"type":"interruption"}` is sent to the browser so it
    clears its audio queue.
  • The browser sends `{"type":"playback_done"}` whenever its scheduled audio
    has drained.  The server uses this (with a fallback timeout) to release the
    `agent_speaking` flag — eliminating the dead-air guess of a fixed timer.

Wire protocol
─────────────
Browser → Backend  (binary):      Raw PCM 16 000 Hz 16-bit LE mono
Browser → Backend  (text/JSON):
    {"type": "interrupt"}                — user explicitly stopped agent
    {"type": "playback_done"}            — browser audio queue drained

Backend → Browser  (binary):      Raw PCM 16 000 Hz 16-bit LE mono
Backend → Browser  (text/JSON):
    {"type": "session_ready", "first_message": "...", "voice_id": "...", "voice_name": "..."}
    {"type": "transcript",    "role": "user",  "text": "..."}
    {"type": "agent_start"}
    {"type": "agent_end",     "text": "..."}
    {"type": "interruption"}             — browser must stop playback now
    {"type": "error",         "detail": "..."}

Authentication: pass JWT as ?token=<jwt> query param.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import random
import re
import struct

import httpx
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.llm import get_ai_client
from app.core.security import decode_token, get_current_user
from app.db.database import async_session_factory, get_db
from app.models.models import (
    Advertisement,
    CallTranscript,
    Company,
    User,
    UserRole,
    VoiceSession,
)
from app.services.ai.fusion_tts import _fetch_tts_pcm, normalize_pcm, pcm_to_wav
from app.services.ai.voicebot_agent import AUSTRALIAN_VOICES, _voice_for_style

logger = logging.getLogger(__name__)

router = APIRouter()

ELEVENLABS_ASR_URL = "https://api.elevenlabs.io/v1/speech-to-text"
_ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"

FLASH_MODEL = settings.ELEVENLABS_FLASH_MODEL  # eleven_flash_v2
EXPRESSIVE_MODEL = settings.ELEVENLABS_EXPRESSIVE_MODEL  # eleven_v3_conversational
SAMPLE_RATE = 16_000

# Keep at most this many turns of history sent to Claude (plus initial seed).
# Long calls would otherwise inflate latency and let the model drift.
_HISTORY_MAX_TURNS = 14

# Minimum continuous voiced audio required to count as a barge-in attempt.
# Tuned conservative so echo bleed / background noise can't truncate the
# agent mid-sentence.  Real user speech easily lasts >800 ms.
_BARGE_IN_MIN_MS = 900

# Maximum time to wait for the browser to ack playback completion before
# falling back to a timer (in case the browser never sends the ack).
_PLAYBACK_DONE_TIMEOUT_S = 8.0

_VOICE_SETTINGS = {
    "stability": 0.55,
    "similarity_boost": 0.82,
    "style": 0.35,
    "use_speaker_boost": False,
}

# 2-word fillers (~900 ms each) — long enough to cover ASR + LLM + v3 TTFB.
# Flash synthesises them in ~75 ms so the browser hears something immediately.
_THINKING_FILLERS = [
    "Hmm, sure.",
    "Ah, right.",
    "Mmm, okay.",
    "Right, sure.",
    "Of course.",
    "Absolutely.",
]

# Strip XML-style and square-bracket audio tags so Flash v2 doesn't speak them literally.
_XML_TAG_RE = re.compile(r"<[^>]{1,40}>|\[[^\]]{1,30}\]")
# Sentence-boundary regex for opener extraction (period, question mark, exclamation).
_SENTENCE_END_RE = re.compile(r"[.!?]+\s*$")


# ─────────────────────────────────────────────────────────────────────────────
# VAD
# ─────────────────────────────────────────────────────────────────────────────


def _rms(pcm_bytes: bytes) -> float:
    n = len(pcm_bytes) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm_bytes[: n * 2])
    return math.sqrt(sum(s * s for s in samples) / n)


# ─────────────────────────────────────────────────────────────────────────────
# ASR
# ─────────────────────────────────────────────────────────────────────────────


async def _transcribe(pcm_bytes: bytes) -> str:
    """Send buffered PCM to ElevenLabs STT. Returns '' on failure."""
    if not pcm_bytes:
        return ""
    wav = pcm_to_wav(pcm_bytes, sample_rate=SAMPLE_RATE)
    headers = {"xi-api-key": settings.ELEVENLABS_API_KEY or ""}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                ELEVENLABS_ASR_URL,
                headers=headers,
                files={"file": ("audio.wav", io.BytesIO(wav), "audio/wav")},
                data={"model_id": "scribe_v1"},
            )
            if not resp.is_success:
                logger.warning("ASR %s: %s", resp.status_code, resp.text[:200])
                return ""
            return resp.json().get("text", "").strip()
    except Exception as exc:
        logger.warning("ASR exception: %s", exc)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# TTS helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _synth_flash(text: str, voice_id: str) -> bytes:
    """Synthesise a short phrase with Flash and return normalised PCM (buffered)."""
    pcm = await _fetch_tts_pcm(text, voice_id, FLASH_MODEL)
    return normalize_pcm(pcm)


async def _stream_tts_to_ws(
    text: str,
    voice_id: str,
    model_id: str,
    websocket: WebSocket,
    chunk_size: int = 4096,
    interrupt_event: asyncio.Event | None = None,
) -> None:
    """
    Stream ElevenLabs TTS PCM chunks directly to the browser as they arrive.

    If *interrupt_event* is provided, stops forwarding chunks the moment it is
    set — used by the barge-in path.
    """
    if not text.strip():
        return
    url = _ELEVENLABS_TTS_URL.format(voice_id=voice_id)
    payload = {"text": text, "model_id": model_id, "voice_settings": _VOICE_SETTINGS}
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
                raise ValueError(f"ElevenLabs TTS [{model_id}] {resp.status_code}: {body.decode(errors='replace')}")
            async for chunk in resp.aiter_bytes(chunk_size):
                if interrupt_event is not None and interrupt_event.is_set():
                    return
                if chunk:
                    await websocket.send_bytes(chunk)


# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# History windowing
# ─────────────────────────────────────────────────────────────────────────────


def _trim_history(history: list[dict]) -> list[dict]:
    """Keep the most recent turns; preserve user/assistant alternation."""
    if len(history) <= _HISTORY_MAX_TURNS:
        return history
    trimmed = history[-_HISTORY_MAX_TURNS:]
    # Anthropic requires the first message to be "user" — drop a leading
    # assistant if windowing landed on one.
    while trimmed and trimmed[0]["role"] != "user":
        trimmed = trimmed[1:]
    return trimmed


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _create_voice_session(db: AsyncSession, advertisement_id: str) -> VoiceSession:
    session = VoiceSession(
        advertisement_id=advertisement_id,
        status="active",
        caller_metadata={"type": "inbound", "source": "browser_hybrid"},
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def _finalise_session(db: AsyncSession, session: VoiceSession, history: list[dict]) -> None:
    try:
        for i, turn in enumerate(history):
            db.add(
                CallTranscript(
                    session_id=session.id,
                    speaker=turn["role"],
                    text=turn["content"],
                    turn_index=i,
                    timestamp_ms=0,
                )
            )
        session.status = "ended"
        await db.commit()
    except Exception as exc:
        logger.warning("Could not finalise session %s: %s", session.id, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Debug endpoint — returns the exact system prompt the voice bot will use,
# plus the campaign-data presence summary.  Use this to see in one place what
# the agent does (and doesn't) know about the campaign.
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/advertisements/{advertisement_id}/voice/debug")
async def voice_debug(
    advertisement_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.document import AdvertisementDocument
    from app.services.ai.voicebot_agent import VoicebotAgentService

    ad_res = await db.execute(select(Advertisement).where(Advertisement.id == advertisement_id))
    ad = ad_res.scalar_one_or_none()
    if not ad:
        return {"error": "Advertisement not found"}
    if user.role != UserRole.SUPER_USER and ad.company_id != user.company_id:
        return {"error": "Forbidden"}

    docs_res = await db.execute(
        select(AdvertisementDocument).where(AdvertisementDocument.advertisement_id == advertisement_id)
    )
    docs = docs_res.scalars().all()

    svc = VoicebotAgentService(db)
    prompt = await svc._build_system_prompt(ad, allow_audio_tags=True)
    section_headers = [ln for ln in prompt.split("\n") if ln.startswith("## ")]

    return {
        "advertisement_id": ad.id,
        "title": ad.title,
        "campaign_category": ad.campaign_category,
        "duration": ad.duration,
        "trial_location": ad.trial_location,
        "patients_required": ad.patients_required,
        "data_presence": {
            "bot_config_keys": list((ad.bot_config or {}).keys()),
            "strategy_json": bool(ad.strategy_json),
            "website_reqs": bool(ad.website_reqs),
            "questionnaire": bool(ad.questionnaire),
            "target_audience": bool(ad.target_audience),
            "protocol_docs_total": len(docs),
            "protocol_docs_with_content": sum(1 for d in docs if (d.content or "").strip()),
            "protocol_docs_titles": [d.title for d in docs],
        },
        "prompt_length": len(prompt),
        "prompt_sections": section_headers,
        "system_prompt": prompt,
    }


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket endpoint
# ─────────────────────────────────────────────────────────────────────────────


@router.websocket("/advertisements/{advertisement_id}/voice/ws")
async def hybrid_voice_ws(
    websocket: WebSocket,
    advertisement_id: str,
    token: str | None = Query(default=None),
):
    """
    Full-duplex voice session with proper barge-in.

    A background mic_task continuously runs VAD on incoming PCM, even during
    agent speech.  Sustained energy during agent speech triggers an interrupt
    that cancels the in-progress LLM/TTS stream and tells the browser to flush
    its audio queue.  The browser ack's actual playback completion so the
    server knows precisely when the human can speak again.

    DB sessions are short-lived: one for setup (load campaign + create the
    session row) and one for finalisation. A WebSocket can stay open for many
    minutes — holding a pooled connection for its lifetime would starve the
    pool under concurrent calls.
    """
    # ── Auth ──────────────────────────────────────────────────────────────────
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        payload = decode_token(token)
        if not payload.get("sub"):
            raise ValueError("no sub")
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()

    # ── Setup session (closed before the call starts) ─────────────────────────
    from app.models.document import AdvertisementDocument
    from app.services.ai.voicebot_agent import VoicebotAgentService

    async with async_session_factory() as db:
        ad_result = await db.execute(select(Advertisement).where(Advertisement.id == advertisement_id))
        ad = ad_result.scalar_one_or_none()
        if not ad:
            await websocket.send_text(json.dumps({"type": "error", "detail": "Campaign not found"}))
            await websocket.close()
            return

        # Tenant scope — token company must own the campaign (super_user bypasses).
        if payload.get("role") != "super_user" and payload.get("company_id") != ad.company_id:
            await websocket.send_text(json.dumps({"type": "error", "detail": "Forbidden"}))
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        bot_config: dict = ad.bot_config or {}

        publisher_voice_id = bot_config.get("voice_id")
        if publisher_voice_id:
            matched_profile = next((v for v in AUSTRALIAN_VOICES if v["id"] == publisher_voice_id), None)
            voice_id = publisher_voice_id
            voice_name = matched_profile["name"] if matched_profile else bot_config.get("bot_name", "Assistant")
        else:
            profile = _voice_for_style(bot_config.get("conversation_style", "warm"))
            voice_id = profile["id"]
            voice_name = profile["name"]

        company_res = await db.execute(select(Company).where(Company.id == ad.company_id))
        company = company_res.scalar_one_or_none()
        company_name = company.name if company else "our organization"

        default_first_message = (
            f"Hi. This is {voice_name} from {company_name}. "
            f"Thanks a lot for expressing interest in our study. "
            f"How are you doing today?"
        )
        first_message = bot_config.get("first_message") or default_first_message

        svc = VoicebotAgentService(db)
        system_prompt = await svc._build_system_prompt(ad, allow_audio_tags=True)

        # Diagnostic: show exactly what campaign data is/isn't reaching the agent.
        docs_count_res = await db.execute(
            select(AdvertisementDocument).where(AdvertisementDocument.advertisement_id == advertisement_id)
        )
        docs_loaded = docs_count_res.scalars().all()
        docs_with_content = sum(1 for d in docs_loaded if (d.content or "").strip())
        strategy_present = bool(ad.strategy_json)
        website_reqs_present = bool(
            ad.website_reqs
            and isinstance(ad.website_reqs, dict)
            and (ad.website_reqs.get("must_have") or ad.website_reqs.get("faqs"))
        )
        questions_present = bool(
            ad.questionnaire and isinstance(ad.questionnaire, dict) and ad.questionnaire.get("questions")
        )
        logger.info(
            "Voice session campaign data — title=%r category=%r duration=%r "
            "strategy=%s website_reqs=%s questionnaire=%s protocol_docs=%d/%d-with-content "
            "→ system_prompt=%d chars",
            ad.title,
            ad.campaign_category,
            ad.duration,
            strategy_present,
            website_reqs_present,
            questions_present,
            docs_with_content,
            len(docs_loaded),
            len(system_prompt),
        )
        # Emit the section headers we built so you can see what's in the prompt.
        section_headers = [line for line in system_prompt.split("\n") if line.startswith("## ")]
        logger.info("Voice session prompt sections: %s", section_headers)

        voice_session = await _create_voice_session(db, advertisement_id)
        voice_session_id = voice_session.id

    # ── Shared state ──────────────────────────────────────────────────────────
    rms_threshold = float(settings.FUSION_VAD_RMS_THRESHOLD)
    silence_samples_threshold = (settings.FUSION_VAD_SILENCE_MS * SAMPLE_RATE) // 1000
    barge_in_samples_required = (_BARGE_IN_MIN_MS * SAMPLE_RATE) // 1000

    user_speech_queue: asyncio.Queue[bytes] = asyncio.Queue()
    interrupt_event = asyncio.Event()
    playback_done_event = asyncio.Event()
    agent_speaking = asyncio.Event()
    closing = asyncio.Event()

    history: list[dict] = [
        {"role": "user", "content": "[call started]"},
        {"role": "assistant", "content": first_message},
    ]

    # ── Mic reader / VAD task ─────────────────────────────────────────────────
    async def mic_task() -> None:
        speech_buffer = bytearray()
        silence_samples = 0
        speech_detected = False
        voiced_samples_during_agent = 0  # for barge-in debounce

        while not closing.is_set():
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                logger.info("mic_task: client disconnected, closing session")
                closing.set()
                return
            except RuntimeError as exc:
                # Starlette raises RuntimeError once the socket is in a closed
                # state — treat the same as disconnect.
                logger.info("mic_task: socket closed (%s), closing session", exc)
                closing.set()
                return
            except Exception as exc:
                logger.exception("mic_task: unexpected error reading from websocket: %s", exc)
                # Keep the task alive on unknown read errors — the main loop
                # will see no new queue entries but the conversation isn't
                # silently dead.
                await asyncio.sleep(0.1)
                continue

            if message.get("type") == "websocket.disconnect":
                logger.info("mic_task: websocket.disconnect message received")
                closing.set()
                return

            # ── Control messages ──────────────────────────────────────────────
            if message.get("type") == "websocket.receive" and "text" in message:
                try:
                    ctrl = json.loads(message["text"])
                except json.JSONDecodeError:
                    continue
                ctype = ctrl.get("type")
                if ctype == "interrupt":
                    interrupt_event.set()
                    speech_buffer.clear()
                    silence_samples = 0
                    speech_detected = False
                    voiced_samples_during_agent = 0
                elif ctype == "playback_done":
                    playback_done_event.set()
                continue

            if "bytes" not in message:
                continue
            raw: bytes = message["bytes"]
            if not raw:
                continue

            chunk_rms = _rms(raw)
            voiced = chunk_rms >= rms_threshold

            # ── Barge-in detection while agent is speaking ───────────────────
            if agent_speaking.is_set():
                if voiced:
                    voiced_samples_during_agent += len(raw) // 2
                    if voiced_samples_during_agent >= barge_in_samples_required:
                        if not interrupt_event.is_set():
                            logger.info(
                                "Barge-in detected after %d ms voiced (rms=%.0f, threshold=%.0f) — interrupting agent",
                                (voiced_samples_during_agent * 1000) // SAMPLE_RATE,
                                chunk_rms,
                                rms_threshold,
                            )
                            interrupt_event.set()
                        # Start buffering this speech so the next turn includes it.
                        speech_detected = True
                        silence_samples = 0
                        speech_buffer.extend(raw)
                else:
                    # Reset barge-in counter on silence
                    voiced_samples_during_agent = max(0, voiced_samples_during_agent - len(raw) // 2)
                continue

            # ── Normal VAD when agent is not speaking ────────────────────────
            if voiced:
                speech_detected = True
                silence_samples = 0
                speech_buffer.extend(raw)
            else:
                if not speech_detected:
                    continue
                silence_samples += len(raw) // 2
                speech_buffer.extend(raw)
                if silence_samples < silence_samples_threshold:
                    continue

                # End of speech
                pcm_snapshot = bytes(speech_buffer)
                speech_buffer.clear()
                silence_samples = 0
                speech_detected = False
                voiced_samples_during_agent = 0

                if len(pcm_snapshot) < SAMPLE_RATE // 4:
                    continue  # < 250 ms noise burst — drop
                await user_speech_queue.put(pcm_snapshot)

    # ── Agent turn: filler + ASR + LLM/TTS — cancellable ──────────────────────
    async def agent_turn(user_pcm: bytes) -> None:
        agent_speaking.set()
        interrupt_event.clear()
        playback_done_event.clear()
        sent_response_audio = False  # tracks whether response PCM reached the browser

        try:
            # Filler + ASR in parallel
            filler_text = random.choice(_THINKING_FILLERS)
            filler_task = asyncio.create_task(_synth_flash(filler_text, voice_id))
            asr_task = asyncio.create_task(_transcribe(user_pcm))

            try:
                filler_audio = await asyncio.wait_for(filler_task, timeout=3.0)
                for i in range(0, len(filler_audio), 4096):
                    if interrupt_event.is_set():
                        break
                    await websocket.send_bytes(filler_audio[i : i + 4096])
            except Exception as exc:
                logger.warning("Filler TTS failed: %s", exc)

            transcript = await asr_task
            if not transcript:
                logger.warning("ASR returned empty transcript — skipping turn")
                return

            if interrupt_event.is_set():
                return

            logger.info("User: %s", transcript)
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "transcript",
                        "role": "user",
                        "text": transcript,
                    }
                )
            )
            history.append({"role": "user", "content": transcript})

            await websocket.send_text(json.dumps({"type": "agent_start"}))
            # Clear the playback_done_event so the finally block waits for the
            # *response* audio, not the filler ack that arrived during LLM wait.
            playback_done_event.clear()

            agent_text = ""
            try:
                ai = get_ai_client()
                raw = await ai.complete(
                    system=system_prompt,
                    messages=_trim_history(history),
                    max_tokens=1024,
                )
                agent_text = _XML_TAG_RE.sub("", raw).strip()
            except Exception as exc:
                logger.error("LLM error: %s", exc)
                await websocket.send_text(json.dumps({"type": "error", "detail": "Response generation failed"}))

            if agent_text and not interrupt_event.is_set():
                try:
                    await _stream_tts_to_ws(
                        agent_text,
                        voice_id,
                        FLASH_MODEL,
                        websocket,
                        interrupt_event=interrupt_event,
                    )
                    sent_response_audio = True
                except Exception as exc:
                    logger.error("Flash TTS failed: %s", exc)

            history.append(
                {
                    "role": "assistant",
                    "content": agent_text or "I'm sorry, I had a little trouble there.",
                }
            )
            if agent_text:
                logger.info(
                    "Agent (%d words, interrupted=%s): %s",
                    len(agent_text.split()),
                    interrupt_event.is_set(),
                    agent_text[:200],
                )

            await websocket.send_text(
                json.dumps(
                    {
                        "type": "agent_end",
                        "text": agent_text or "",
                        "interrupted": interrupt_event.is_set(),
                    }
                )
            )
        finally:
            # Notify browser to flush queued audio if we aborted mid-stream.
            if interrupt_event.is_set():
                try:
                    await websocket.send_text(json.dumps({"type": "interruption"}))
                except Exception:
                    logger.debug("interruption flush send failed", exc_info=True)
            elif sent_response_audio:
                # Wait for the browser to confirm the response audio queue has
                # drained before releasing the mic gate. On interrupt, don't
                # wait — the browser just flushed everything.
                try:
                    await asyncio.wait_for(playback_done_event.wait(), timeout=_PLAYBACK_DONE_TIMEOUT_S)
                except TimeoutError:
                    logger.debug("playback_done ack timed out — releasing gate")
            agent_speaking.clear()

    # ── Greeting (Flash for fast first impression) ────────────────────────────
    # Start mic_task FIRST so it can process `playback_done` ack and barge-in
    # during the greeting itself.
    agent_speaking.set()
    interrupt_event.clear()
    playback_done_event.clear()
    mic_runner = asyncio.create_task(mic_task(), name="mic_task")

    await websocket.send_text(
        json.dumps(
            {
                "type": "session_ready",
                "first_message": first_message,
                "voice_id": voice_id,
                "voice_name": voice_name,
            }
        )
    )
    await websocket.send_text(json.dumps({"type": "agent_start"}))
    try:
        await _stream_tts_to_ws(
            first_message,
            voice_id,
            FLASH_MODEL,
            websocket,
            interrupt_event=interrupt_event,
        )
    except Exception as exc:
        logger.warning("Greeting TTS failed: %s", exc)
    await websocket.send_text(
        json.dumps(
            {
                "type": "agent_end",
                "text": first_message,
                "interrupted": interrupt_event.is_set(),
            }
        )
    )

    if interrupt_event.is_set():
        # Greeting was barged-in — flush browser queue and skip ack wait.
        try:
            await websocket.send_text(json.dumps({"type": "interruption"}))
        except Exception:
            pass
        agent_speaking.clear()
    else:
        # Wait for browser to ack greeting playback — or timeout.
        try:
            await asyncio.wait_for(playback_done_event.wait(), timeout=_PLAYBACK_DONE_TIMEOUT_S)
        except TimeoutError:
            pass
        agent_speaking.clear()

    # ── Main turn loop ────────────────────────────────────────────────────────
    try:
        while not closing.is_set():
            # Wait for either a queued user turn or a disconnect
            try:
                pcm = await asyncio.wait_for(user_speech_queue.get(), timeout=1.0)
            except TimeoutError:
                if closing.is_set():
                    break
                continue

            await agent_turn(pcm)

    except WebSocketDisconnect:
        logger.info("Voice session disconnected (ad=%s)", advertisement_id)
    except Exception as exc:
        logger.error("Voice session error (ad=%s): %s", advertisement_id, exc)
    finally:
        closing.set()
        mic_runner.cancel()
        try:
            await mic_runner
        except (asyncio.CancelledError, Exception):
            pass
        # Fresh short-lived session — the setup session closed long ago.
        try:
            async with async_session_factory() as db2:
                fresh = await db2.get(VoiceSession, voice_session_id)
                if fresh is not None:
                    await _finalise_session(db2, fresh, history)
                else:
                    logger.warning("Voice session %s vanished before finalise", voice_session_id)
        except Exception:
            logger.exception("Finalising voice session %s failed — transcript not saved", voice_session_id)
