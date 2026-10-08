"""
File Content Extractor
Extracts plain text from uploaded documents so the AI has actual content
instead of seeing '[See attached file]'.

Supported formats:
  .txt / .md   — read directly
  .pdf         — pypdf (page by page)
  .docx        — python-docx paragraphs
  .doc         — not supported (binary format); returns None

Usage:
    from app.services.storage.extractor import extract_bytes
    text = extract_bytes(b"%PDF...", "file.pdf")
"""

import io
import logging
import os

logger = logging.getLogger(__name__)

# Max characters stored per document — keeps DB rows sensible
_MAX_CHARS = 50_000


def extract_bytes(data: bytes, filename: str) -> str | None:
    """Same as extract_text but from in-memory bytes (provider-agnostic)."""
    if not data:
        return None
    ext = os.path.splitext(filename)[1].lower()
    try:
        if ext in (".txt", ".md"):
            return data.decode("utf-8", errors="replace")[:_MAX_CHARS]
        elif ext == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                logger.warning("pypdf not installed — cannot extract PDF content")
                return None
            pages = [p.extract_text().strip() for p in PdfReader(io.BytesIO(data)).pages if p.extract_text()]
            return "\n\n".join(pages)[:_MAX_CHARS]
        elif ext == ".docx":
            try:
                from docx import Document
            except ImportError:
                logger.warning("python-docx not installed — cannot extract DOCX content")
                return None
            paragraphs = [p.text for p in Document(io.BytesIO(data)).paragraphs if p.text.strip()]
            return "\n\n".join(paragraphs)[:_MAX_CHARS]
        else:
            logger.warning("Unsupported file type for extraction: %s", ext)
            return None
    except Exception as exc:
        logger.error("Content extraction failed for %s: %s", filename, exc)
        return None
