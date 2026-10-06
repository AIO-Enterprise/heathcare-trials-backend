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
    from app.services.storage.extractor import extract_text
    text = extract_text("/abs/path/to/file.pdf")
"""

import logging
import os

logger = logging.getLogger(__name__)

# Max characters stored per document — keeps DB rows sensible
_MAX_CHARS = 50_000


def extract_text(abs_path: str) -> str | None:
    """
    Extract plain text from a file on disk.

    Args:
        abs_path: absolute filesystem path to the file

    Returns:
        Extracted text string, or None if extraction fails / unsupported format.
    """
    if not abs_path or not os.path.exists(abs_path):
        return None

    ext = os.path.splitext(abs_path)[1].lower()

    try:
        if ext in (".txt", ".md"):
            return _read_text(abs_path)
        elif ext == ".pdf":
            return _read_pdf(abs_path)
        elif ext == ".docx":
            return _read_docx(abs_path)
        else:
            logger.warning("Unsupported file type for extraction: %s", ext)
            return None
    except Exception as exc:
        logger.error("Content extraction failed for %s: %s", abs_path, exc)
        return None


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()[:_MAX_CHARS]


def _read_pdf(path: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf not installed — cannot extract PDF content")
        return None

    reader = PdfReader(path)
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text.strip())
    return "\n\n".join(pages)[:_MAX_CHARS]


def _read_docx(path: str) -> str:
    try:
        from docx import Document
    except ImportError:
        logger.warning("python-docx not installed — cannot extract DOCX content")
        return None

    doc = Document(path)
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n\n".join(paragraphs)[:_MAX_CHARS]


def url_to_disk_path(file_url: str, backend_root: str) -> str | None:
    """
    Convert a stored URL like '/uploads/docs/company/file.pdf'
    to an absolute disk path.

    Args:
        file_url:     the value stored in DB (e.g. '/uploads/docs/.../file.pdf')
        backend_root: absolute path to the backend root directory

    Returns:
        Absolute path string, or None if the URL cannot be resolved.
    """
    if not file_url:
        return None
    relative = file_url.lstrip("/").removeprefix("uploads/")
    return os.path.join(backend_root, "uploads", relative)


# Resolve backend root from this file's location
# extractor.py lives at backend/app/services/storage/extractor.py
# → 4 levels up = backend/
BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
