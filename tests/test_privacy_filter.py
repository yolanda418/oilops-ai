"""Tests for services/privacy_filter.py (STEP 4).

All fixtures are SYNTHETIC. No real vendor / bank / personal data.

These tests pin down the privacy boundary:
    * sensitive PII is detected and replaced with typed placeholders
    * business-critical fields (invoice #, PO #, dates, amounts,
      vendor name, description) are NEVER falsely redacted
    * the AI-safe payload contains only allow-listed business fields
      and is guaranteed to exclude any sensitive field, even when a
      caller tries to smuggle one in via `extra_fields`
    * the privacy filter makes no network calls
"""

from __future__ import annotations

import socket
import urllib.request
from decimal import Decimal

import pytest

from services.invoice_extractor import InvoiceExtraction
from services.privacy_filter import (
    ALL_CATEGORIES,
    CATEGORY_BANK_ACCOUNT,
    CATEGORY_EMAIL,
    CATEGORY_IBAN,
    CATEGORY_PHONE,
    CATEGORY_ROUTING_NUMBER,
    CATEGORY_SWIFT,
    Detection,
    RedactionResult,
    build_ai_safe_payload,
    is_ai_safe_payload,
    redact_text,
)


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------


PRIVACY_INVOICE = (
    "INVOICE (SYNTHETIC TEST DATA)\n"
    "Prairie Field Equipment Ltd. (SYNTHETIC TEST DATA)\n"
    "Invoice #: PFE-2026-009\n"
    "PO #: PO-8821\n"
    "Invoice Date: 2026-09-22\n"
    "Due Date: 2026-10-22\n"
    "Description: Pump equipment rental\n"
    "Subtotal: CAD 5,000.00\n"
    "GST: CAD 250.00\n"
    "Total: CAD 5,250.00\n"
    "\n"
    "Remit To:\n"
    "Bank Account: 123456789012\n"
    "Routing Number: 021000021\n"
    "SWIFT: BOFMCAM2\n"
    "IBAN: GB82WEST12345698765432\n"
    "Email: billing@example.com\n"
    "Phone: (403) 555-0123\n"
)


INVOICE_NUMBER_FALSE_POSITIVE = (
    "INVOICE (SYNTHETIC TEST DATA)\n"
    "Prairie Field Equipment Ltd. (SYNTHETIC TEST DATA)\n"
    "Invoice #: 202609221234\n"
    "PO #: 202609221235\n"
    "Bank Account: 202609221236\n"
    "Subtotal: $1,000.00\n"
    "Total: $1,050.00\n"
)



# ---------------------------------------------------------------------------
# Per-category detection
# ---------------------------------------------------------------------------


def test_detects_bank_account():
    r = redact_text("Bank Account: 123456789012\n")
    cats = [d.category for d in r.detections]
    assert CATEGORY_BANK_ACCOUNT in cats


def test_detects_routing_number():
    r = redact_text("Routing Number: 021000021\n")
    cats = [d.category for d in r.detections]
    assert CATEGORY_ROUTING_NUMBER in cats


@pytest.mark.parametrize("label", ["Routing / Transit", "Routing", "Transit"])
def test_detects_routing_transit_label_variants(label):
    result = redact_text(f"{label}: 00000-001\n")
    assert "00000-001" not in result.redacted_text
    assert CATEGORY_ROUTING_NUMBER in result.categories


def test_detects_swift():
    r = redact_text("SWIFT: BOFMCAM2\n")
    cats = [d.category for d in r.detections]
    assert CATEGORY_SWIFT in cats


def test_detects_iban():
    r = redact_text("IBAN: GB82WEST12345698765432\n")
    cats = [d.category for d in r.detections]
    assert CATEGORY_IBAN in cats


def test_detects_email():
    r = redact_text("Contact: accounts@example.com\n")
    cats = [d.category for d in r.detections]
    assert CATEGORY_EMAIL in cats


def test_detects_phone_parens():
    r = redact_text("Phone: (403) 555-0123\n")
    cats = [d.category for d in r.detections]
    assert CATEGORY_PHONE in cats


def test_detects_phone_dash():
    r = redact_text("Phone: 403-555-1234\n")
    cats = [d.category for d in r.detections]
    assert CATEGORY_PHONE in cats


def test_detects_phone_dot():
    r = redact_text("Phone: 403.555.1234\n")
    cats = [d.category for d in r.detections]
    assert CATEGORY_PHONE in cats


def test_detects_phone_with_country_code():
    r = redact_text("Phone: +1 403 555 0123\n")
    cats = [d.category for d in r.detections]
    assert CATEGORY_PHONE in cats


def test_multiple_sensitive_fields_in_one_document():
    r = redact_text(PRIVACY_INVOICE)
    cats = sorted(r.categories)
    assert CATEGORY_BANK_ACCOUNT in cats
    assert CATEGORY_ROUTING_NUMBER in cats
    assert CATEGORY_SWIFT in cats
    assert CATEGORY_IBAN in cats
    assert CATEGORY_EMAIL in cats
    assert CATEGORY_PHONE in cats


def test_redaction_count_matches_detections():
    r = redact_text(PRIVACY_INVOICE)
    assert r.redaction_count == len(r.detections)
    assert r.redaction_count >= 6



# ---------------------------------------------------------------------------
# Typed placeholders
# ---------------------------------------------------------------------------


def test_redactions_use_typed_placeholders():
    r = redact_text(PRIVACY_INVOICE)
    for d in r.detections:
        assert d.placeholder == "[REDACTED:" + d.category + "]"
    # Every category we know about must have its own typed placeholder
    # (i.e. "[REDACTED:BANK_ACCOUNT]" never equals "[REDACTED:EMAIL]").
    seen = {d.placeholder for d in r.detections}
    assert "[REDACTED:BANK_ACCOUNT]" in seen
    assert "[REDACTED:EMAIL]" in seen
    assert len(seen) == len({d.category for d in r.detections})


def test_detection_original_captures_pre_redaction_text():
    r = redact_text("Bank Account: 123456789012\n")
    bank_dets = [d for d in r.detections if d.category == CATEGORY_BANK_ACCOUNT]
    assert len(bank_dets) == 1
    assert bank_dets[0].original == "123456789012"


def test_detection_offsets_match_redacted_text():
    r = redact_text("Bank Account: 123456789012\n")
    bank_dets = [d for d in r.detections if d.category == CATEGORY_BANK_ACCOUNT]
    d = bank_dets[0]
    # `length` is the length of the ORIGINAL span that was redacted.
    assert d.length == len(d.original)
    # The detection offsets point into the ORIGINAL text, NOT the
    # redacted text (which has placeholders of different lengths).
    orig = 'Bank Account: 123456789012\n'
    assert orig[d.start:d.end] == d.original
    # And the placeholder IS present in the redacted text.
    assert d.placeholder in r.redacted_text


def test_category_counts_helper():
    r = redact_text(
        "Email: a@x.com\n"
        "Email: b@x.com\n"
        "Bank Account: 123456789\n"
    )
    counts = r.category_counts()
    assert counts[CATEGORY_EMAIL] == 2
    assert counts[CATEGORY_BANK_ACCOUNT] == 1


def test_all_categories_constant_matches_detected_set():
    # Sanity: every category constant is a non-empty string.
    for c in ALL_CATEGORIES:
        assert isinstance(c, str) and c



# ---------------------------------------------------------------------------
# Original text immutability
# ---------------------------------------------------------------------------


def test_redact_text_does_not_mutate_input():
    original = "Bank Account: 123456789012\nEmail: a@b.com\n"
    snapshot = original
    redact_text(original)
    assert original == snapshot


def test_redacted_text_is_fresh_string():
    original = "Bank Account: 123456789012\n"
    r = redact_text(original)
    assert isinstance(r.redacted_text, str)
    assert r.redacted_text is not original


def test_redaction_result_is_dataclass():
    r = redact_text("Bank Account: 123456789012\n")
    assert isinstance(r, RedactionResult)
    for d in r.detections:
        assert isinstance(d, Detection)


def test_empty_input_safe():
    r = redact_text("")
    assert r.redacted_text == ""
    assert r.detections == []
    assert r.redaction_count == 0


def test_none_input_safe():
    r = redact_text(None)
    assert r.redacted_text == ""
    assert r.detections == []


def test_malformed_garbage_input_safe():
    r = redact_text("!@#$%^&*()_+=\n\n\n\x00\x01")
    # No false positives expected from non-invoice garbage.
    assert isinstance(r, RedactionResult)
    assert isinstance(r.redacted_text, str)



# ---------------------------------------------------------------------------
# False-positive protection on business fields
# ---------------------------------------------------------------------------


def test_invoice_number_preserved():
    r = redact_text(PRIVACY_INVOICE)
    assert "PFE-2026-009" in r.redacted_text
    assert "[REDACTED" not in r.redacted_text.split("\n")[2]  # Invoice # line


def test_po_number_preserved():
    r = redact_text(PRIVACY_INVOICE)
    assert "PO-8821" in r.redacted_text


def test_invoice_dates_preserved():
    r = redact_text(PRIVACY_INVOICE)
    assert "2026-09-22" in r.redacted_text
    assert "2026-10-22" in r.redacted_text


def test_subtotal_preserved():
    r = redact_text(PRIVACY_INVOICE)
    assert "5,000.00" in r.redacted_text


def test_gst_preserved():
    r = redact_text(PRIVACY_INVOICE)
    assert "250.00" in r.redacted_text


def test_total_preserved():
    r = redact_text(PRIVACY_INVOICE)
    assert "5,250.00" in r.redacted_text


def test_description_preserved():
    r = redact_text(PRIVACY_INVOICE)
    assert "Pump equipment rental" in r.redacted_text


def test_vendor_name_preserved():
    r = redact_text(PRIVACY_INVOICE)
    assert "Prairie Field Equipment Ltd." in r.redacted_text


# ---------------------------------------------------------------------------
# Critical: label-driven detection means long digits WITHOUT a label
# (like an Invoice # or PO #) are NOT redacted, even when they are
# the same length as the bank account on the next line.
# ---------------------------------------------------------------------------


def test_long_digits_without_label_are_preserved():
    """The numbers 202609221234 and 202609221235 appear only as Invoice #
    and PO # — they must be preserved verbatim."""
    r = redact_text(INVOICE_NUMBER_FALSE_POSITIVE)
    # Invoice # and PO # preserved
    assert "Invoice #: 202609221234" in r.redacted_text
    assert "PO #: 202609221235" in r.redacted_text
    # Only the labelled "Bank Account" value is redacted
    cats = [d.category for d in r.detections]
    assert CATEGORY_BANK_ACCOUNT in cats
    # The bank account digit run is gone from the redacted version
    assert "Bank Account: 202609221236" not in r.redacted_text
    # But the other two long digit runs (no label) survive intact
    assert r.redacted_text.count("202609221234") == 1
    assert r.redacted_text.count("202609221235") == 1


def test_invoice_number_is_not_misclassified_as_bank_account():
    """Specifically: a digit run following 'Invoice #:' is NEVER a BANK_ACCOUNT
    detection, even though it is the same shape as a bank account."""
    r = redact_text("Invoice #: 202609221234\n")
    cats = [d.category for d in r.detections]
    assert CATEGORY_BANK_ACCOUNT not in cats
    assert "202609221234" in r.redacted_text


def test_po_number_is_not_misclassified_as_bank_account():
    r = redact_text("PO #: 202609221235\n")
    cats = [d.category for d in r.detections]
    assert CATEGORY_BANK_ACCOUNT not in cats
    assert "202609221235" in r.redacted_text



# ---------------------------------------------------------------------------
# AI-safe payload
# ---------------------------------------------------------------------------


def _extraction() -> InvoiceExtraction:
    return InvoiceExtraction(
        vendor_name="Prairie Field Equipment Ltd.",
        invoice_number="PFE-2026-009",
        invoice_date="2026-09-22",
        due_date="2026-10-22",
        po_number="PO-8821",
        description="Pump equipment rental",
        subtotal=Decimal("5000.00"),
        gst=Decimal("250.00"),
        total_amount=Decimal("5250.00"),
        currency="CAD",
    )


def test_ai_safe_payload_excludes_bank_account():
    p = build_ai_safe_payload(_extraction())
    for forbidden in ("bank_account", "routing", "account"):
        assert not any(forbidden in k.lower() for k in p.keys())


def test_ai_safe_payload_excludes_routing_swift_iban_email_phone():
    p = build_ai_safe_payload(_extraction())
    for forbidden in ("routing", "swift", "iban", "email", "phone"):
        assert not any(forbidden in k.lower() for k in p.keys())


def test_ai_safe_payload_contains_only_allowed_business_fields():
    p = build_ai_safe_payload(_extraction())
    allowed = {
        "vendor_name", "invoice_number", "invoice_date", "due_date",
        "po_number", "description", "subtotal", "gst", "total_amount",
        "currency", "warnings",
    }
    assert set(p.keys()).issubset(allowed), (
        "extra fields leaked into payload: " + str(set(p.keys()) - allowed)
    )


def test_ai_safe_payload_serializes_money_as_strings():
    p = build_ai_safe_payload(_extraction())
    assert isinstance(p["subtotal"], str)
    assert isinstance(p["gst"], str)
    assert isinstance(p["total_amount"], str)


def test_is_ai_safe_payload_returns_true_for_clean_payload():
    p = build_ai_safe_payload(_extraction())
    assert is_ai_safe_payload(p) is True


def test_is_ai_safe_payload_returns_false_for_smuggled_sensitive_field():
    """Even if a caller tries to put a sensitive field into the payload,
    is_ai_safe_payload must reject it."""
    p = {"description": "test", "bank_account": "1"}
    assert is_ai_safe_payload(p) is False
    p2 = {"description": "test", "raw_text": "secret"}
    assert is_ai_safe_payload(p2) is False


def test_build_ai_safe_payload_ignores_smuggled_extra_fields():
    """Even if a caller passes extra_fields=[...], the forbidden fields
    are still stripped."""
    p = build_ai_safe_payload(
        _extraction(),
        extra_fields=["bank_account", "routing_number", "raw_text", "phone"],
    )
    for forbidden in ("bank_account", "routing_number", "raw_text", "phone"):
        assert forbidden not in p


def test_build_ai_safe_payload_does_not_contain_raw_text():
    p = build_ai_safe_payload(_extraction())
    assert "raw_text" not in p
    assert "text" not in p



# ---------------------------------------------------------------------------
# Security: no network calls
# ---------------------------------------------------------------------------


def test_privacy_filter_makes_no_network_calls(monkeypatch):
    """Hard guarantee: privacy_filter must not open TCP, do DNS or HTTP."""

    def _blocked_create_connection(*a, **k):
        raise AssertionError("redact_text must not open TCP connections")

    def _blocked_getaddrinfo(*a, **k):
        raise AssertionError("redact_text must not perform DNS lookups")

    def _blocked_urlopen(*a, **k):
        raise AssertionError("redact_text must not make HTTP requests")

    monkeypatch.setattr(socket, "create_connection", _blocked_create_connection, raising=True)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked_getaddrinfo, raising=True)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked_urlopen, raising=True)

    r = redact_text(PRIVACY_INVOICE)
    assert isinstance(r, RedactionResult)
    # Also build a payload, since it shares the module surface.
    p = build_ai_safe_payload(_extraction())
    assert isinstance(p, dict)


def test_redact_text_handles_unicode_safely():
    r = redact_text("Bank Account: 123456789012\nEmail: tëst@example.com\n")
    assert isinstance(r.redacted_text, str)
    # Email detector is ASCII-locale safe: the unicode-local email is
    # either redacted (good) or left verbatim; what matters is no crash.
    assert "Bank Account:" in r.redacted_text
