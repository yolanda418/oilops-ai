"""Local PDF invoice parser for OilOps AI.

PRIVACY GUARANTEES
-----------------
* This module makes NO external network requests.
* It does NOT call any LLM or third-party API.
* It does NOT upload invoice contents anywhere.
* All processing happens in-memory; the caller decides what to persist.

STEP 2 SCOPE
------------
This parser only extracts raw text and basic metadata. Structured
extraction of vendor / invoice_number / amounts is STEP 3 territory"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class InvoiceParseResult:
    """Structured result of a local PDF parse.

    Attributes
    ----------
    filename: original filename as supplied by the user.
    raw_text: full extracted text (joined across pages with newlines).
    page_count: number of pages successfully read.
    parsing_status: 'ok' on success; 'empty', 'failed', or 'unsupported' otherwise.
    error: human-readable error message when parsing failed; None otherwise.
    """
    filename: str = ""
    raw_text: str = ""
    page_count: int = 0
    parsing_status: str = "ok"
    error: Optional[str] = None



# Maximum bytes we will load into memory from a single upload.
MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MB; invoices are small documents.

# Maximum characters of raw_text to show in the UI preview.
# The full text is still stored in session_state; the preview is just
# for the human eye. Larger previews defeat the privacy principle of
# 'show a reasonable preview'.
MAX_RAW_TEXT_PREVIEW = 2000


class InvoiceParseError(Exception):
    """Raised when a PDF cannot be parsed for structural reasons."""


def _looks_like_pdf(data: bytes) -> bool:
    """Cheap magic-bytes check. We never read beyond the first 5 bytes."""
    return isinstance(data, bytes) and len(data) >= 5 and data[:5] == b"%PDF-"


def parse_pdf(data: bytes, filename: str = "") -> InvoiceParseResult:
    """Parse a PDF entirely in-memory.

    Returns an InvoiceParseResult regardless of outcome; failures are
    signalled via parsing_status + error rather than exceptions so the
    Streamlit UI can render them gracefully.

    This function is sync and pure. It must remain offline-only.
    """
    result = InvoiceParseResult(filename=filename)

    if not isinstance(data, (bytes, bytearray)):
        result.parsing_status = "failed"
        result.error = "Uploaded payload is not bytes."
        return result

    data = bytes(data)

    if len(data) == 0:
        result.parsing_status = "empty"
        result.error = "Uploaded file is empty."
        return result

    if len(data) > MAX_PDF_BYTES:
        result.parsing_status = "failed"
        result.error = "PDF exceeds 25 MB cap."
        return result

    if not _looks_like_pdf(data):
        result.parsing_status = "unsupported"
        result.error = "File does not look like a PDF (missing %PDF- header)."
        return result


    # Lazy import so unit tests that mock PyPDF do not require it installed.
    try:
        from pypdf import PdfReader
    except Exception as exc:
        result.parsing_status = "failed"
        result.error = "PDF parser library not available: " + str(exc)
        return result

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        result.parsing_status = "failed"
        result.error = "Failed to open PDF: " + str(exc)
        return result

    page_texts = []
    try:
        for page in reader.pages:
            try:
                extracted = page.extract_text() or ""
            except Exception as exc:
                # One bad page must not abort the whole parse.
                extracted = ""
            page_texts.append(extracted)
    except Exception as exc:
        result.parsing_status = "failed"
        result.error = "Failed to iterate pages: " + str(exc)
        return result

    result.page_count = len(page_texts)
    result.raw_text = chr(10).join(page_texts).strip()

    if result.page_count == 0:
        result.parsing_status = "empty"
        result.error = "PDF contained no pages."
    elif not result.raw_text:
        # Pages exist but no extractable text; likely a scanned document.
        result.parsing_status = "empty"
        result.error = "No extractable text (PDF may be image-only / scanned)."
    return result


def preview_text(raw_text: str, max_chars: int = MAX_RAW_TEXT_PREVIEW) -> str:
    """Return a privacy-respecting preview of the parsed text."""
    if not raw_text:
        return ""
    if len(raw_text) <= max_chars:
        return raw_text
    return raw_text[:max_chars] + chr(10) + "... (truncated for preview; full text retained in session_state)"

