"""Sensitive-data detection and redaction for OilOps AI (STEP 4).

PRIVACY GUARANTEES
------------------
* This module makes NO external network requests.
* It does NOT call any LLM or third-party API.
* It is fully deterministic and runs in-memory only.
* It does not persist anything to disk.

STEP 4 SCOPE
------------
Two responsibilities:

    1. Detect and redact common sensitive fields that might be embedded in
       the bank footer / remit-to section of an invoice (bank account
       numbers, routing / SWIFT / IBAN, email, phone). Detection is
       *context-based*: an explicit label such as "Account Number:" or
       "Routing:" is required before any numeric run is treated as
       sensitive. This deliberately trades raw recall for low
       false-positive rate on business-critical fields like "Invoice #" /
       "PO #" / dates / amounts.

    2. Build a *minimum-necessary* AI-safe payload from a structured
       `InvoiceExtraction`. The payload contains only business fields
       that a future expense classifier might reasonably consume
       (description, subtotal, gst, total_amount, currency, vendor_name,
       invoice_number, ...). Sensitive financial identifiers (bank
       account, routing, SWIFT, IBAN, email, phone) are NEVER included
       in this payload even when present in the underlying text.

DESIGN PRINCIPLES
-----------------
* Regex with strict terminators to avoid false positives on long digits
  such as "Invoice #: 202609221234" or "PO #: PO-1042".
* Original input text is NOT mutated in place.
* Typed placeholders ("[REDACTED:BANK_ACCOUNT]") keep the *category* of
  what was removed so a future AI pipeline can still reason about field
  presence, without ever seeing the value.
* No toggle, no flag, no UI option disables redaction for the AI-safe
  payload. The redacted/minimized path is the ONLY path future AI code
  may consume.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional, Sequence

from .invoice_extractor import InvoiceExtraction


CATEGORY_BANK_ACCOUNT = "BANK_ACCOUNT"
CATEGORY_ROUTING_NUMBER = "ROUTING_NUMBER"
CATEGORY_SWIFT = "SWIFT"
CATEGORY_IBAN = "IBAN"
CATEGORY_EMAIL = "EMAIL"
CATEGORY_PHONE = "PHONE"

ALL_CATEGORIES: tuple = (
    CATEGORY_BANK_ACCOUNT,
    CATEGORY_ROUTING_NUMBER,
    CATEGORY_SWIFT,
    CATEGORY_IBAN,
    CATEGORY_EMAIL,
    CATEGORY_PHONE,
)


@dataclass
class Detection:
    """A single redaction occurrence."""

    category: str
    placeholder: str
    start: int
    end: int
    original: str

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class RedactionResult:
    """Outcome of running the privacy filter over a piece of text."""

    redacted_text: str
    detections: List[Detection] = field(default_factory=list)

    @property
    def redaction_count(self) -> int:
        return len(self.detections)

    @property
    def categories(self) -> List[str]:
        seen = []
        for d in self.detections:
            if d.category not in seen:
                seen.append(d.category)
        return seen

    def category_counts(self) -> dict:
        out = {}
        for d in self.detections:
            out[d.category] = out.get(d.category, 0) + 1
        return out


def _placeholder(category: str) -> str:
    return "[REDACTED:" + category + "]"


# ---------------------------------------------------------------------------
# Label-driven detectors (bank account, routing, SWIFT)
# ---------------------------------------------------------------------------
#
# These detectors look for an explicit label on the same line, then
# capture the value to the right of that label. This is what makes us
# context-aware: a bare 12-digit number with no label is NOT flagged,
# but "Account Number: 12345..." IS.

_LABEL_BANK_ACCOUNT_LOOSE_RE = re.compile(
    r"(?im)^[ \t]*"
    r"(?P<lbl>bank\s*account|account(?:\s*(?:#|no|number))?|acct\.?\s*(?:#|no|number)?)"
    r"\s*[:\-]?\s*"
    r"(?P<val>\d[\d \-]{4,30}\d)"
)

_LABEL_ROUTING_RE = re.compile(
    r"(?im)^[ \t]*"
    r"(?P<lbl>"
    r"routing(?:\s*(?:/\s*(?:transit|transit\s*(?:#|no|number)))|\s*(?:#|no|number))?"
    r"|transit(?:\s*(?:/\s*routing)|\s*(?:#|no|number))?"
    r"|aba(?:\s*(?:#|no|number|routing))?"
    r"|sort\s*code"
    r")"
    r"\s*[:\-]?\s*"
    r"(?P<val>\d[\d \-]{4,18}\d)"
)

_LABEL_SWIFT_RE = re.compile(
    r"(?im)^[ \t]*"
    r"(?P<lbl>"
    r"swift(?:\s*(?:#|code|bic))?"
    r"|bic(?:\s*(?:#|code))?"
    r"|swift/bic"
    r")"
    r"\s*[:\-]?\s*"
    r"(?P<val>[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)"
)


# Pattern-driven detectors (IBAN, email, phone)

_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}[A-Z0-9]{0,30}\b")

_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

_PHONE_RE = re.compile(
    r"(?x)"
    r"(?<![A-Za-z0-9])"
    r"(?:\+?1[-.\s]?)?"
    r"(?:\(\d{3}\)\s?|\d{3}[-.\s])"
    r"\d{3}[-.\s]\d{4}"
    r"(?![A-Za-z0-9])"
)


def _filter_bank_account_value(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return 6 <= len(digits) <= 24



def _find_label_detections(text: str) -> List[Detection]:
    out: List[Detection] = []
    seen_spans: set = set()

    for m in _LABEL_BANK_ACCOUNT_LOOSE_RE.finditer(text):
        val = m.group("val")
        if not _filter_bank_account_value(val):
            continue
        start, end = m.span("val")
        if (start, end) in seen_spans:
            continue
        seen_spans.add((start, end))
        out.append(
            Detection(
                category=CATEGORY_BANK_ACCOUNT,
                placeholder=_placeholder(CATEGORY_BANK_ACCOUNT),
                start=start,
                end=end,
                original=text[start:end],
            )
        )

    for m in _LABEL_ROUTING_RE.finditer(text):
        val = m.group("val")
        digits = re.sub(r"\D", "", val)
        if len(digits) < 6 or len(digits) > 16:
            continue
        start, end = m.span("val")
        if (start, end) in seen_spans:
            continue
        seen_spans.add((start, end))
        out.append(
            Detection(
                category=CATEGORY_ROUTING_NUMBER,
                placeholder=_placeholder(CATEGORY_ROUTING_NUMBER),
                start=start,
                end=end,
                original=text[start:end],
            )
        )

    for m in _LABEL_SWIFT_RE.finditer(text):
        val = m.group("val").upper()
        if not (8 <= len(val) <= 11):
            continue
        start, end = m.span("val")
        if (start, end) in seen_spans:
            continue
        seen_spans.add((start, end))
        out.append(
            Detection(
                category=CATEGORY_SWIFT,
                placeholder=_placeholder(CATEGORY_SWIFT),
                start=start,
                end=end,
                original=text[start:end],
            )
        )

    return out



def _find_pattern_detections(text: str) -> List[Detection]:
    out: List[Detection] = []

    for m in _IBAN_RE.finditer(text):
        cand = m.group(0)
        if len(re.sub(r"\s", "", cand)) < 15:
            continue
        start, end = m.span(0)
        out.append(
            Detection(
                category=CATEGORY_IBAN,
                placeholder=_placeholder(CATEGORY_IBAN),
                start=start,
                end=end,
                original=text[start:end],
            )
        )

    for m in _EMAIL_RE.finditer(text):
        start, end = m.span(0)
        out.append(
            Detection(
                category=CATEGORY_EMAIL,
                placeholder=_placeholder(CATEGORY_EMAIL),
                start=start,
                end=end,
                original=text[start:end],
            )
        )

    for m in _PHONE_RE.finditer(text):
        val = m.group(0)
        if len(re.sub(r"\D", "", val)) < 10:
            continue
        start, end = m.span(0)
        out.append(
            Detection(
                category=CATEGORY_PHONE,
                placeholder=_placeholder(CATEGORY_PHONE),
                start=start,
                end=end,
                original=text[start:end],
            )
        )

    return out


def _merge_and_sort(detections: List[Detection]) -> List[Detection]:
    """Sort by start, drop any that overlap (keep the first one seen)."""
    detections = sorted(detections, key=lambda d: (d.start, -d.end))
    merged: List[Detection] = []
    last_end = -1
    for d in detections:
        if d.start < last_end:
            continue
        merged.append(d)
        last_end = d.end
    return merged


def redact_text(raw_text: Optional[str]) -> RedactionResult:
    """Run the full detection pipeline on `raw_text` and return a fresh
    RedactionResult.

    The original input is never mutated. The redacted text is a brand-new
    string. Empty / None input returns an empty result.
    """
    if raw_text is None or raw_text == "":
        return RedactionResult(redacted_text="")

    label_dets = _find_label_detections(raw_text)
    pattern_dets = _find_pattern_detections(raw_text)
    detections = _merge_and_sort(label_dets + pattern_dets)

    parts: List[str] = []
    cursor = 0
    for d in detections:
        parts.append(raw_text[cursor:d.start])
        parts.append(d.placeholder)
        cursor = d.end
    parts.append(raw_text[cursor:])
    redacted = "".join(parts)

    return RedactionResult(redacted_text=redacted, detections=detections)



# ---------------------------------------------------------------------------
# AI-safe payload
# ---------------------------------------------------------------------------
#
# This is the ONLY structured representation that future AI code is
# allowed to consume. It deliberately omits:
#   - bank account / routing / SWIFT / IBAN
#   - email, phone
#   - the full raw invoice text
#
# Keep this list minimal. Add fields ONLY when a future step genuinely
# needs them, and re-audit the redaction boundary each time.


_AI_SAFE_ALLOWED_FIELDS: tuple = (
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "due_date",
    "po_number",
    "description",
    "subtotal",
    "gst",
    "total_amount",
    "currency",
    "warnings",
)


_FORBIDDEN_FIELDS: tuple = (
    "bank_account",
    "routing_number",
    "swift",
    "iban",
    "email",
    "phone",
    "raw_text",
)


def build_ai_safe_payload(
    extraction: InvoiceExtraction,
    *,
    extra_fields: Optional[Sequence[str]] = None,
) -> dict:
    """Construct the AI-safe payload from an `InvoiceExtraction`.

    The returned dict contains ONLY fields on the allow-list. Adding
    anything sensitive (bank account, contact info, raw text) is a bug.
    """
    allowed = set(_AI_SAFE_ALLOWED_FIELDS)
    if extra_fields:
        allowed.update(extra_fields)

    raw = extraction.as_dict()
    payload = {}
    for k in sorted(allowed):
        if k in raw:
            v = raw[k]
            if isinstance(v, Decimal):
                payload[k] = str(v)
            else:
                payload[k] = v

    # Hard guarantee: sensitive fields can NEVER appear in the payload
    # even if a caller smuggles them via extra_fields.
    for forbidden in _FORBIDDEN_FIELDS:
        payload.pop(forbidden, None)

    return payload


def is_ai_safe_payload(payload: dict) -> bool:
    """Defensive helper: True iff `payload` contains no field that we
    have decided is sensitive. Use this to gate any future AI call."""
    return not any(field in payload for field in _FORBIDDEN_FIELDS)
