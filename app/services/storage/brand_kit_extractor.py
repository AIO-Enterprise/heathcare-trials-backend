"""
Brand Kit PDF Extractor
Extracts dominant colors and font families from a brand guidelines PDF,
then uses Claude to interpret raw signals into usable hex values and
Google Font names.

Pipeline:
  1. pymupdf (fitz) — scrape every text span's color + font name, and
     sample fill/stroke colors from vector paths on each page.
  2. Aggregate → top-3 colors by frequency, top-2 fonts by usage count.
  3. Claude — given the raw signals, return a structured JSON with
     primaryColor, accentColor, secondaryColor, primaryFont, secondaryFont,
     adjectives, dos, donts.  Claude normalises approximate colors to clean
     hex values and maps internal font names to Google Fonts equivalents.

Falls back gracefully: if pymupdf is absent the function raises ImportError;
if Claude is unavailable it returns the raw signal data with best-effort hex.
"""

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from app.core.exceptions import ValidationError

logger = logging.getLogger(__name__)

# ── Minimum frequency for a color to be considered dominant ───────────────────
_MIN_COLOR_HITS = 3


@dataclass
class _RawSignals:
    """Intermediate extraction result before Claude post-processing."""

    color_counts: Counter = field(default_factory=Counter)  # hex → count
    font_counts: Counter = field(default_factory=Counter)  # font name → count
    text_sample: str = ""  # first ~800 chars of body text


# ── Step 1: pymupdf extraction ────────────────────────────────────────────────


def _rgb_int_to_hex(rgb_int: int) -> str:
    """Convert pymupdf integer RGB (0xRRGGBB) to #rrggbb."""
    r = (rgb_int >> 16) & 0xFF
    g = (rgb_int >> 8) & 0xFF
    b = (rgb_int) & 0xFF
    return f"#{r:02x}{g:02x}{b:02x}"


def _float_triple_to_hex(r: float, g: float, b: float) -> str:
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


_NEAR_WHITE = {"#ffffff", "#fefefe", "#fdfdfd", "#fafafa", "#f9f9f9", "#f5f5f5"}
_NEAR_BLACK = {"#000000", "#010101", "#0d0d0d", "#111111", "#1a1a1a"}

# Regex to find explicit hex color codes in text (e.g. "HEX #F37037" or "#f37037")
_HEX_IN_TEXT_RE = re.compile(r"#([0-9A-Fa-f]{6})\b")


def _is_trivial(hex_color: str) -> bool:
    """Skip near-white and near-black — they are almost never brand colors."""
    c = hex_color.lower()
    return c in _NEAR_WHITE or c in _NEAR_BLACK


def _extract_signals_from_bytes(pdf_bytes: bytes) -> _RawSignals:
    try:
        import fitz  # pymupdf
    except ImportError:
        raise ImportError("pymupdf is required for PDF brand extraction. Install it with: pip install pymupdf")

    signals = _RawSignals()
    text_parts: list[str] = []

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_limit = min(len(doc), 25)  # cap at 25 pages — covers color/typography sections

    for page_idx in range(page_limit):
        page = doc[page_idx]

        # ── Text spans: color + font ──────────────────────────────────────────
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
        for block in blocks:
            if block.get("type") != 0:  # 0 = text block
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if not text:
                        continue

                    # Collect body text for Claude context
                    if len(text) > 3:
                        text_parts.append(text)

                    # Explicit hex codes written in the PDF text (e.g. "HEX #F37037")
                    # These are the most reliable signal — brand guidelines commonly list them.
                    for match in _HEX_IN_TEXT_RE.findall(text):
                        hex_c = f"#{match.lower()}"
                        if not _is_trivial(hex_c):
                            # High-weight boost: explicitly stated colors dominate the ranking
                            signals.color_counts[hex_c] += 500

                    # Font name
                    font_raw = span.get("font", "")
                    if font_raw:
                        # Strip weight/style suffixes like "-Bold", ",Italic"
                        base_font = re.split(r"[-,]", font_raw)[0].strip()
                        if base_font:
                            signals.font_counts[base_font] += len(text)

                    # Text color (integer 0xRRGGBB)
                    color_int = span.get("color", None)
                    if color_int is not None:
                        hex_c = _rgb_int_to_hex(color_int)
                        if not _is_trivial(hex_c):
                            signals.color_counts[hex_c] += len(text)

        # ── Vector paths: fill / stroke colors ───────────────────────────────
        try:
            paths = page.get_drawings()
        except Exception:
            paths = []

        for path in paths:
            for attr in ("fill", "color"):
                val = path.get(attr)
                if not val:
                    continue
                try:
                    if isinstance(val, (list, tuple)) and len(val) == 3:
                        hex_c = _float_triple_to_hex(*val)
                    elif isinstance(val, int):
                        hex_c = _rgb_int_to_hex(val)
                    else:
                        continue
                    if not _is_trivial(hex_c):
                        signals.color_counts[hex_c] += 1
                except Exception:
                    continue

    doc.close()

    signals.text_sample = " ".join(text_parts)[:800]
    return signals


# ── Step 2: Claude interpretation ─────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a brand designer AI. Given raw color frequencies and font names extracted
from a brand guidelines PDF, return a JSON object with the following fields:

{
  "primaryColor":   "#rrggbb",   // darkest / most prominent brand color; good for sidebar
  "accentColor":    "#rrggbb",   // vivid, contrasting color for buttons and highlights
  "secondaryColor": "#rrggbb",   // supporting brand color used in campaigns
  "primaryFont":    "Font Name", // exact Google Fonts name; readable at body size
  "secondaryFont":  "Font Name", // exact Google Fonts name; for headings or contrast
  "adjectives":     "word, word, word",   // 3 brand tone adjectives
  "dos":            "short guidance string",
  "donts":          "short guidance string"
}

Rules:
- Choose colors that would work in a professional healthcare SaaS dashboard.
- primaryColor must be dark (lightness < 25%). Reject light colors for primary.
- accentColor must contrast well on white (avoid colors close to primary).
- Map internal PDF font names to the closest Google Fonts equivalent.
  If there is no clear match, use "Inter" for primary and "Source Serif 4" for secondary.
- Base adjectives/dos/donts on the text sample provided.
- Return ONLY valid JSON, no markdown, no explanation.
"""


async def _interpret_with_claude(signals: _RawSignals) -> dict:
    from app.core.llm import extract_json, get_ai_client

    top_colors = [
        {"hex": hex_c, "occurrences": cnt}
        for hex_c, cnt in signals.color_counts.most_common(10)
        if cnt >= _MIN_COLOR_HITS
    ]
    top_fonts = [{"font": name, "character_count": cnt} for name, cnt in signals.font_counts.most_common(6)]

    user_content = json.dumps(
        {
            "top_colors": top_colors,
            "top_fonts": top_fonts,
            "text_sample": signals.text_sample,
        },
        indent=2,
    )

    ai = get_ai_client()
    raw = await ai.complete(
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        max_tokens=512,
    )
    return extract_json(raw)


def _best_effort_result(signals: _RawSignals) -> dict:
    """Fallback when Claude is unavailable — pick top colors heuristically."""
    colors = [c for c, _ in signals.color_counts.most_common(5)]
    fonts = [f for f, _ in signals.font_counts.most_common(2)]
    return {
        "primaryColor": colors[0] if len(colors) > 0 else None,
        "accentColor": colors[1] if len(colors) > 1 else None,
        "secondaryColor": colors[2] if len(colors) > 2 else None,
        "primaryFont": fonts[0] if len(fonts) > 0 else "Inter",
        "secondaryFont": fonts[1] if len(fonts) > 1 else "Source Serif 4",
        "adjectives": None,
        "dos": None,
        "donts": None,
    }


# ── Public API ────────────────────────────────────────────────────────────────


async def extract_brand_from_pdf(pdf_bytes: bytes) -> dict:
    """
    Extract brand colors and fonts from a PDF's raw bytes.

    Returns a dict with keys:
        primaryColor, accentColor, secondaryColor,
        primaryFont, secondaryFont,
        adjectives, dos, donts

    Raises:
        ImportError  — if pymupdf is not installed
        ValidationError — if the PDF yields no usable signals
    """
    signals = _extract_signals_from_bytes(pdf_bytes)

    if not signals.color_counts and not signals.font_counts:
        raise ValidationError(
            "No color or font data could be extracted from this PDF. It may be a scanned image-only document."
        )

    from app.core.llm import get_ai_client

    ai = get_ai_client()
    if not ai.is_configured():
        logger.warning("AI not configured — returning best-effort brand extraction")
        return _best_effort_result(signals)

    try:
        return await _interpret_with_claude(signals)
    except Exception as exc:
        logger.error("Claude interpretation failed, using best-effort fallback: %s", exc)
        return _best_effort_result(signals)
