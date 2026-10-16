"""
Creative Agent - Ad Copy + Image Generation
Owner: AI Dev

Pipeline:
  1. Generic LLM   → structured JSON (layout specs + copy + photo prompt)
  2. Image model   → clean background photo (scene only, no text)
  3. Compositor     → Pillow overlays headline, divider, subtext onto photo
  4. Save PNG       → served via /outputs/
"""

import asyncio
import base64
import json
import logging
import os
from typing import Any

from openai import OpenAI

from app.core.config import settings
from app.core.llm import extract_json, get_ai_client
from app.services.storage import file_storage
from app.models.models import Advertisement

logger = logging.getLogger(__name__)


def _foundry_base_url(url: str) -> str:
    """Root for the Foundry OpenAI v1 data plane (".../openai/v1")."""
    from urllib.parse import urlparse

    url = (url or "").strip()
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else url.rstrip("/")
    path = parsed.path or ""
    if "/openai/v1" in path:
        return root + path[: path.index("/openai/v1") + len("/openai/v1")]
    return root + "/openai/v1"


def _get_image_client():
    """Returns an OpenAI-compatible image client, or None when AZURE_IMAGE_GEN_* is unset."""
    url = (settings.AZURE_IMAGE_GEN_API_URL or "").strip()
    key = (settings.AZURE_IMAGE_GEN_API_KEY or "").strip()
    if not (url and key):
        return None
    return OpenAI(base_url=_foundry_base_url(url), api_key=key)


def _image_generation_enabled() -> bool:
    enabled = bool(
        (settings.AZURE_IMAGE_GEN_API_URL or "").strip()
        and (settings.AZURE_IMAGE_GEN_API_KEY or "").strip()
        and (settings.AZURE_IMAGE_GEN_MODEL or "").strip()
    )
    import logging

    _log = logging.getLogger(__name__)
    # Partial endpoint — safe to log (no credentials)
    endpoint_hint = (settings.AZURE_IMAGE_GEN_API_URL or "")[:60] if settings.AZURE_IMAGE_GEN_API_URL else "not set"
    _log.info(
        "Image generation enabled=%s | model=%s | azure_endpoint=%s | has_azure_key=%s",
        enabled,
        (settings.AZURE_IMAGE_GEN_MODEL or "").strip() or "not set",
        endpoint_hint,
        bool((settings.AZURE_IMAGE_GEN_API_KEY or "").strip()),
    )
    return enabled


# ── Claude system prompt ──────────────────────────────────────────────────────
# Rules derived from analysis of 13 high-performing clinical trial recruitment
# ads running on Meta/Instagram Reels (Nucleus Network, Clinical Trial Seeker,
# George Institute, Woolcock, Clinibase, Sydney Clinic, Nightingale Research).

_CREATIVE_SYSTEM = """You are a clinical trial recruitment advertising expert who has studied hundreds of high-performing healthcare ads on Meta/Facebook/Instagram Reels.

Given a marketing strategy and ad specifications, output structured creative briefs — one per format.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANDATORY DIVERSITY RULE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ALL creatives MUST use layout_style "full_bleed" — photo fills the entire canvas with a dark gradient scrim, white text overlaid. This is non-negotiable.

Within that fixed style, EVERY creative MUST be distinctly different across these axes:

AXIS 1 — HOOK ARCHETYPE: each creative must use a different archetype from the list below. No two creatives may share the same hook type.

AXIS 2 — IMAGE CONCEPT: each creative must depict a completely different scene, subject, and visual mood. If creative 1 shows a person indoors at night, creative 2 must be outdoors or in a clinical setting, creative 3 must be a different demographic angle or emotional tone. No two image_prompts may describe the same scenario.

Additionally, vary: CTA wording, subtext tone, body copy angle (empathy vs eligibility vs reward), headline length, and scrim darkness (top_bg_color between #0d0d0d and #1a1a1a).

Each creative has two parts:
1. LAYOUT SPECS  — everything the compositor needs to render the text design overlay
2. IMAGE PROMPT  — a prompt for GPT-image-1 to generate the PHOTO ONLY (bottom section, no text)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOOK ARCHETYPES — pick the best fit for the condition and audience
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. SPECIFIC SCENARIO — name the exact moment, not the condition label
   BAD: "Struggling with insomnia?"
   GOOD: "Still staring at the ceiling at 3am?" / "Mind racing the second your head hits the pillow?"

2. STATISTIC SHOCK — prevalence/scale with specificity to stop the scroll
   e.g. "1 IN 3 AUSTRALIANS can't get the sleep they need" / "Over 2,000 Australians helped so far"

3. FRUSTRATION BRIDGE — mirror the daily burden in their own words
   BAD: "Do you have Type 2 Diabetes?"
   GOOD: "Tired of planning every meal around your blood sugar?" / "Sick of reading every label just to eat lunch?"

4. REWARD LEAD — compensation + benefit stacked together
   e.g. "Get paid $80 AND finally understand your sleep" / "8 weeks, fully remote, $350 gift card on completion"

5. CREDIBILITY/MISSION — institution-backed altruistic framing
   e.g. "UNIVERSITY OF SYDNEY researchers need your help" / "HELP ADVANCE SLEEP RESEARCH backed by UNSW"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HIGH-CONVERSION COPY RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HEADLINE (headline_text rendered on the image):
  - Be SPECIFIC: name the exact moment, symptom, or frustration — never the condition label alone
  - Sleep: "Still awake at 3am?" beats "Having trouble sleeping?"
  - Pain: "Knees aching every time you walk downstairs?" beats "Experiencing knee pain?"
  - ALL CAPS on the most emotionally charged or data-driven words only

SUBTEXT (one line, high information density):
  Include TWO of these credibility signals — do NOT just list the cash reward:
  • Institution: "Backed by [University/Hospital]"
  • Scale: "[N]+ participants enrolled"
  • Method: "Fully remote · No medication · App-based"
  • Outcome: "Clinically proven approach"
  • Ease: "Takes 60 seconds to check eligibility"
  Example: "Backed by UNSW Sleep Research · 500+ enrolled · Fully remote"

BODY (2-3 sentences shown in the platform feed, not on the image):
  - Open with the exact daily frustration: "If you're [specific scenario], you're not alone."
  - Sentence 2: eligibility hook with specific details (age, location, condition).
  - Sentence 3: low-friction nudge — "It takes under 60 seconds to check if you qualify."
  - NEVER use: "cutting-edge", "revolutionary", "world-class", "unique opportunity"

proof_line (SHORT credibility stamp rendered between headline and subtext):
  - This is a single compact line shown on the image to add social proof.
  - Examples: "Rated 4.8★ · 500+ participants" | "UNSW-backed research" | "2,400 enrolled so far"
  - Max 50 characters. Leave empty string "" if no credibility data is available.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HIGH-CONVERSION CTA OPTIONS — pick the most action-driven fit
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Choose the CTA that matches the hook's tone and lowers friction:
  Urgency:      "Check My Eligibility" | "See If I Qualify" | "Start Tonight"
  Low-friction: "Take the 60-Second Quiz" | "Check in 60 Seconds"
  Reward:       "Claim My Spot" | "Get Paid to Participate"
  Mission:      "Learn About the Study" | "Contact the Study Team" | "Help Advance Research"
  Default:      "Book Now"

CRITICAL SAFETY RULES FOR COPY:
  - Do NOT imply the advertiser knows the viewer's medical condition or diagnosis.
  - Avoid stereotype-heavy condition shortcuts like "Still reading EVERY LABEL before lunch?" unless the strategy explicitly supports that phrasing and it can be framed neutrally.
  - Do NOT use scarcity or pressure language such as "limited spots", "act now", or similar urgency unless the campaign context explicitly requires it and it is factual, material, and non-misleading.
  - Any statistic used in headline_text, body, or proof_line must be source-safe, geographically sensible, and easy for the sponsor to substantiate. If unsure, do not use a statistic.
  - If the campaign's next step is inquiry, information request, or eligibility screening, the CTA must reflect that. Do NOT use enrollment-forward CTAs like "Join the Study" unless the next step is truly enrollment.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LAYOUT RULES (full_bleed only)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
layout_style: ALWAYS "full_bleed"
top_bg_color: dark scrim colour — vary between #0d0d0d, #111111, #0a0a1a, #0d1a0d (subtle tint variation)
top_height_pct: ignored — field still required, set to 50
text_color: always #FFFFFF
subtext_color: always #CCCCCC
divider_color: always #FFFFFF

headline_text (the HOOK rendered large over the photo):
  - Be SPECIFIC: name the exact moment or frustration, not just the condition label
  - Write the most emotionally charged or data-driven words in ALL CAPS
  - For STATISTIC SHOCK: lead with the number ("1 IN 70 AUSTRALIANS HAVE COELIAC DISEASE")
  - For REWARD LEAD: put the benefit in ALL CAPS ("Join This PAID RESEARCH STUDY")

subtext (compact one-liner below proof_line):
  - Eligibility nudge: "Paid study. Limited spots available."
  - Or: "You may qualify — check eligibility now."
  - Or: "Participants reimbursed for time and travel."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMAGE PROMPT RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL: Show a CONDITION-SPECIFIC lifestyle scene. Never use a generic "smiling couple outdoors."

Match the scene to the condition — be SPECIFIC and modern:
  Insomnia/sleep   → person staring at glowing phone screen in pitch-dark bedroom at 3am |
                     OR overhead shot of person lying rigid awake, clock showing 2am, harsh shadows |
                     OR split-toned contrast: left side dark/restless, right side calm/sleeping
  Diabetes/weight  → person checking glucose reader, frustrated expression, kitchen counter |
                     OR person carefully reading nutrition label in supermarket aisle
  Obesity/BMI      → person mid-walk on suburban street, slightly breathless, authentic body type
  Coeliac/dietary  → hands carefully inspecting bread packaging; restaurant scene checking menu
  Joint/ortho      → person wincing as they climb stairs; hands gripping a railing
  Mental health    → person sitting alone at window at dusk, soft light, introspective |
                     OR close portrait with direct eye contact, dignified, not sad
  Respiratory      → person pausing mid-run to catch breath; inhaler on bedside table
  Autoimmune       → person looking tired but determined; close portrait, natural light
  Oncology         → person in calm garden or park, peaceful and reflective

Photography style:
  - Cinematic, editorial, slightly moody — NOT bright stock-photo cheerfulness
  - Real people, authentic body language, imperfect lighting
  - Subject age/gender must match the trial's target demographic from the strategy
  - Modern, mobile-native framing: vertical composition, subject in upper or centre frame
  - NO text, NO words, NO letters, NO UI overlays anywhere in the image
  - Max 400 characters

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Respond ONLY with valid JSON (no markdown fences, no extra text):
{
  "creatives": [
    {
      "index": 0,
      "format": "<format name>",
      "headline": "<platform card headline, max 8 words>",
      "body": "<2-3 sentences — include eligibility/compensation/location>",
      "cta": "<one action-driven CTA from the HIGH-CONVERSION CTA OPTIONS list above>",
      "layout": {
        "layout_style": "full_bleed",
        "top_bg_color": "<dark scrim hex — vary subtly e.g. #111111, #0d0d0d, #0a0a1a>",
        "top_height_pct": 50,
        "headline_text": "<HOOK with ALL CAPS on key condition/action words>",
        "divider_color": "#FFFFFF",
        "subtext": "<one punchy eligibility or compensation nudge>",
        "text_color": "#FFFFFF",
        "subtext_color": "#CCCCCC",
        "proof_line": "<short credibility stamp max 50 chars e.g. 'Rated 4.8★ · 500+ participants' — empty string if none>"
      },
      "image_prompt": "<condition-specific lifestyle scene, photorealistic editorial, no text, max 400 chars>"
    }
  ]
}"""


class CreativeService:
    def __init__(self, company_id: str, ai=None):
        self.company_id = company_id
        # Injectable AI client (DIP) — defaults to the shared provider adapter.
        self.ai = ai or get_ai_client()

    async def generate_creatives(self, ad: Advertisement, instructions: str | None = None) -> list[dict[str, Any]]:
        """
        Main entry point.
        Returns list of creative dicts: {format, headline, body, cta, layout, image_prompt, image_url}
        """
        # Step 1: Claude → structured JSON (layout + photo prompt)
        brief = await self._generate_brief(ad, instructions=instructions)
        items = brief.get("creatives", [])
        if not items:
            return []

        # Steps 2 + 3: GPT photo → Pillow composite (each in a thread, concurrent)
        async def process(item):
            image_url = None
            if _image_generation_enabled():
                layout_for_compositor = {
                    **item.get("layout", {}),
                    "cta": item.get("cta", "Book Now"),
                }
                image_url = await asyncio.to_thread(
                    self._generate_and_composite,
                    item.get("image_prompt", ""),
                    layout_for_compositor,
                    item.get("format", "square"),
                    item.get("index", 0),
                    ad.id,
                )
            return {
                "format": item.get("format", ""),
                "headline": item.get("headline", ""),
                "body": item.get("body", ""),
                "cta": item.get("cta", "Book Now"),
                "layout": item.get("layout", {}),
                "image_prompt": item.get("image_prompt", ""),
                "image_url": image_url,
            }

        results = list(await asyncio.gather(*[process(c) for c in items]))

        # Generate a short video from the first PNG creative (runs after PNGs complete)
        if _image_generation_enabled() and results:
            first = results[0]
            if first.get("image_url"):
                video_url = await asyncio.to_thread(
                    self._generate_video_from_creative,
                    first,
                    ad.id,
                )
                if video_url:
                    results.append(
                        {
                            **first,
                            "format": "1080x1920 Short Video",
                            "image_url": video_url,
                        }
                    )

        return results

    # ── Step 1: Claude brief ──────────────────────────────────────────────────

    async def _generate_brief(self, ad: Advertisement, instructions: str | None = None) -> dict[str, Any]:
        if not self.ai.is_configured():
            return self._mock_brief(ad)

        strategy = json.dumps(ad.strategy_json, indent=2) if ad.strategy_json else "{}"
        ad_details = json.dumps(ad.ad_details, indent=2) if ad.ad_details else "{}"

        user_msg = f"""## Campaign: {ad.title}
Budget: {ad.budget or "unspecified"}

## Marketing Strategy
{strategy}

## Ad Specifications (from Reviewer AI)
{ad_details}

Generate one creative brief per format listed in the ad specifications.
If no formats are defined, generate three 1080x1920 Meta Story Ads — each a distinct creative with different hook, mood, and photo concept."""

        if instructions and instructions.strip():
            user_msg += f"\n\n## Reviewer / Ethics Instruction\n{instructions.strip()}\n\nApply this instruction precisely while keeping the creatives compliant and production-ready."

        try:
            text = await self.ai.complete(
                system=_CREATIVE_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
                max_tokens=4096,
            )
            return extract_json(text)
        except json.JSONDecodeError as exc:
            # Fail closed: a garbled brief must surface as a generation error
            # (recorded on the ad by the caller) — never silent mock creatives
            # that could flow into a paid campaign. Mock mode is only for
            # unconfigured AI (see above).
            raise RuntimeError(f"Creative brief was not valid JSON for ad {ad.id} — please retry") from exc
        # Provider failures propagate — a silent mock-brief fallback would
        # publish placeholder creatives into a paid campaign without anyone
        # noticing. Callers record the failure on the advertisement instead.

    # ── Steps 2+3: GPT photo → composite ─────────────────────────────────────

    def _generate_and_composite(
        self,
        image_prompt: str,
        layout: dict,
        format_name: str,
        index: int,
        ad_id: str,
    ) -> str | None:
        """
        Synchronous — run via asyncio.to_thread.
        1. GPT-image-1 generates the scene photo
        2. Compositor overlays text design
        3. Saves final PNG and returns URL path
        """
        import logging

        log = logging.getLogger(__name__)

        import logging

        log = logging.getLogger(__name__)

        # Step 2: GPT-image-1 → raw scene photo
        try:
            client = _get_image_client()
            if client is None:
                log.error("Image generation not configured (AZURE_IMAGE_GEN_*) [format=%s, ad=%s]", format_name, ad_id)
                return None
            size = self._get_openai_size(format_name)

            safe_prompt = (
                image_prompt
                or "Smiling hopeful older couple outdoors, warm sunlight, blue sky, photorealistic, no text"
            )[:400]

            response = client.images.generate(
                model=(settings.AZURE_IMAGE_GEN_MODEL or "").strip(),
                prompt=safe_prompt,
                size=size,
                quality="high",
                n=1,
            )
            photo_bytes = base64.b64decode(response.data[0].b64_json)
            log.info("GPT image generated [format=%s, ad=%s]", format_name, ad_id)
        except Exception as exc:
            log.error("GPT image generation failed [format=%s, ad=%s]: %s", format_name, ad_id, exc)
            return None

        # Step 3: Compositor → overlay text design
        try:
            from app.services.ai.compositor import composite_ad

            canvas_w, canvas_h = self._get_canvas_dimensions(format_name)
            final_png = composite_ad(photo_bytes, layout, canvas_w, canvas_h)
        except ImportError:
            log.error("Pillow not installed — skipping compositor. Run: pip install Pillow>=10.0.0")
            final_png = photo_bytes  # fall back to raw photo
        except Exception as exc:
            log.error("Compositor failed [format=%s, ad=%s]: %s", format_name, ad_id, exc)
            final_png = photo_bytes  # fall back to raw photo

        # Save final PNG — timestamp suffix ensures each regen gets a unique URL
        try:
            import time

            safe_fmt = format_name.replace(" ", "_").replace("/", "-").replace(":", "-").lower()
            ts = int(time.time())
            filename = f"creative_{index}_{safe_fmt}_{ts}.png"
            return file_storage.save_bytes(final_png, f"outputs/{self.company_id}/{ad_id}", filename)
        except Exception as exc:
            log.error("Failed to save creative [format=%s, ad=%s]: %s", format_name, ad_id, exc)
            return None

    async def generate_shorts(self, ad: Advertisement) -> list[dict[str, Any]]:
        """
        Generate a 4-second silent short video from each existing PNG creative.
        Returns list of creative dicts with .mp4 image_url, same structure as output_files.
        """
        source_creatives = ad.output_files or []
        png_sources = [c for c in source_creatives if c.get("image_url") and not c["image_url"].endswith(".mp4")]
        if not png_sources:
            return []

        results = list(
            await asyncio.gather(
                *[
                    asyncio.to_thread(self._generate_short_from_creative, c, ad.id, i)
                    for i, c in enumerate(png_sources)
                ]
            )
        )

        return [r for r in results if r is not None]

    def _generate_short_from_creative(
        self,
        creative: dict,
        ad_id: str,
        index: int = 0,
    ) -> dict | None:
        import logging as _log
        import time

        log = _log.getLogger(__name__)

        image_url = creative.get("image_url", "")
        png_bytes = file_storage.open_bytes(image_url)
        if not png_bytes:
            log.warning("PNG not found for short generation: %s", image_url)
            return None

        fmt = creative.get("format", "1080x1920")
        canvas_w, canvas_h = self._get_canvas_dimensions(fmt)

        try:
            from app.services.ai.video_compositor import composite_short_video

            mp4_bytes = composite_short_video(png_bytes, canvas_w, canvas_h)
            if not mp4_bytes:
                return None
        except Exception as exc:
            log.error("Short video compositor failed for ad %s: %s", ad_id, exc)
            return None

        try:
            ts = int(time.time())
            filename = f"short_{index}_{ts}.mp4"
            stored_url = file_storage.save_bytes(mp4_bytes, f"outputs/{self.company_id}/{ad_id}", filename)
            return {
                **creative,
                "format": f"{fmt} · Short",
                "image_url": stored_url,
            }
        except Exception as exc:
            log.error("Failed to save short for ad %s: %s", ad_id, exc)
            return None

    def _generate_video_from_creative(
        self,
        creative: dict,
        ad_id: str,
    ) -> str | None:
        import logging
        import time

        log = logging.getLogger(__name__)

        image_url = creative.get("image_url", "")
        if not image_url:
            return None

        png_bytes = file_storage.open_bytes(image_url)
        if not png_bytes:
            log.warning("PNG not found for video generation: %s", image_url)
            return None

        fmt = creative.get("format", "1080x1920")
        canvas_w, canvas_h = self._get_canvas_dimensions(fmt)

        try:
            from app.services.ai.video_compositor import composite_video

            mp4_bytes = composite_video(png_bytes, canvas_w, canvas_h)
            if not mp4_bytes:
                return None
        except Exception as exc:
            log.error("Video compositor failed for ad %s: %s", ad_id, exc)
            return None

        try:
            ts = int(time.time())
            filename = f"creative_video_{ts}.mp4"
            return file_storage.save_bytes(mp4_bytes, f"outputs/{self.company_id}/{ad_id}", filename)
        except Exception as exc:
            log.error("Failed to save video creative for ad %s: %s", ad_id, exc)
            return None

    def _get_openai_size(self, format_name: str) -> str:
        fmt = format_name.lower()
        if any(k in fmt for k in ("1080x1920", "story", "portrait", "9x16", "9:16")):
            return "1024x1536"
        if any(k in fmt for k in ("16x9", "16:9", "landscape", "banner")):
            return "1536x1024"
        return "1024x1024"

    def _get_canvas_dimensions(self, format_name: str) -> tuple[int, int]:
        fmt = format_name.lower()
        if any(k in fmt for k in ("1080x1920", "story", "portrait", "9x16", "9:16")):
            return (1080, 1920)
        if any(k in fmt for k in ("16x9", "16:9", "landscape", "banner")):
            return (1920, 1080)
        return (1080, 1080)

    # ── Mock (no API keys configured) ────────────────────────────────────────

    def _mock_brief(self, ad: Advertisement) -> dict[str, Any]:
        return {
            "creatives": [
                {
                    "index": 0,
                    "format": "1080x1920 Meta Ad",
                    "headline": f"Living with {ad.title}?",
                    "body": "No one understands this condition better than you. Join our paid research study and help discover new treatments. Participants reimbursed for time and travel.",
                    "cta": "Book Now",
                    "layout": {
                        "layout_style": "full_bleed",
                        "top_bg_color": "#111111",
                        "top_height_pct": 50,
                        "headline_text": f"Living with {ad.title.upper()}?",
                        "divider_color": "#FFFFFF",
                        "subtext": "You may qualify for a paid clinical research study.",
                        "text_color": "#FFFFFF",
                        "subtext_color": "#CCCCCC",
                        "proof_line": "500+ participants enrolled · UNSW-backed",
                    },
                    "image_prompt": "Middle-aged person sitting thoughtfully in a quiet living room, soft natural window light, authentic documentary style, photorealistic editorial, no text, no graphics",
                    "image_url": None,
                },
                {
                    "index": 1,
                    "format": "1080x1920 Meta Ad",
                    "headline": "Paid Research Study — Limited Spots",
                    "body": "We are looking for adults aged 30–65 managing a chronic health condition. Participants will be reimbursed for time and travel. Check your eligibility today.",
                    "cta": "Book Now",
                    "layout": {
                        "layout_style": "full_bleed",
                        "top_bg_color": "#0d0d0d",
                        "top_height_pct": 50,
                        "headline_text": "PAID RESEARCH STUDY — Limited Spots",
                        "divider_color": "#FFFFFF",
                        "subtext": "Reimbursed for time and travel. Apply in minutes.",
                        "text_color": "#FFFFFF",
                        "subtext_color": "#CCCCCC",
                        "proof_line": "Rated 4.8★ · 2,400 enrolled so far",
                    },
                    "image_prompt": "Person in their 50s walking outdoors in a park, slightly fatigued but determined expression, soft warm morning light, authentic candid feel, photorealistic editorial, no text",
                    "image_url": None,
                },
                {
                    "index": 2,
                    "format": "1080x1920 Meta Ad",
                    "headline": "Help Advance Medical Research",
                    "body": "Your participation could help researchers discover something new. Inviting adults aged 18–65. Less burden, more freedom — check your eligibility now.",
                    "cta": "Book Now",
                    "layout": {
                        "layout_style": "full_bleed",
                        "top_bg_color": "#111111",
                        "top_height_pct": 50,
                        "headline_text": "HELP ADVANCE MEDICAL RESEARCH",
                        "divider_color": "#FFFFFF",
                        "subtext": "Paid study. Limited spots available.",
                        "text_color": "#FFFFFF",
                        "subtext_color": "#CCCCCC",
                        "proof_line": "University of Sydney · Fully remote",
                    },
                    "image_prompt": "Close portrait of a smiling man in his 40s looking directly at camera, soft outdoor natural light, urban background slightly blurred, genuine warm expression, photorealistic editorial, no text",
                    "image_url": None,
                },
            ]
        }
