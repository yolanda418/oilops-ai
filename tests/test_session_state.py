"""STEP 6 / STEP 7 hardening tests."""
from __future__ import annotations

from decimal import Decimal

from services.invoice_extractor import InvoiceExtraction
from services.session_state import (
    compute_classifier_fingerprint,
    fingerprint_matches,
    reset_review_session,
    session_has_invoice,
)


class _FakeState(dict):
    def __getattr__(self, k):
        if k in self:
            return self[k]
        return None

    def __setattr__(self, k, v):
        self[k] = v

    def __delattr__(self, k):
        if k in self:
            del self[k]


def _ext(**overrides):
    defaults = dict(
        vendor_name="Prairie Pump Rentals",
        description="Equipment rental",
        total_amount=Decimal("5250.00"),
        currency="CAD",
    )
    defaults.update(overrides)
    return InvoiceExtraction(**defaults)


def test_fingerprint_is_stable():
    a = compute_classifier_fingerprint(_ext())
    b = compute_classifier_fingerprint(_ext())
    assert a == b
    assert len(a) == 64


def test_fingerprint_changes_when_vendor_changes():
    a = compute_classifier_fingerprint(_ext(vendor_name="Acme"))
    b = compute_classifier_fingerprint(_ext(vendor_name="Globex"))
    assert a != b


def test_fingerprint_changes_when_description_changes():
    a = compute_classifier_fingerprint(_ext(description="pump rental"))
    b = compute_classifier_fingerprint(_ext(description="freight"))
    assert a != b


def test_fingerprint_changes_when_total_changes():
    a = compute_classifier_fingerprint(_ext(total_amount=Decimal("5250.00")))
    b = compute_classifier_fingerprint(_ext(total_amount=Decimal("5250.01")))
    assert a != b


def test_fingerprint_changes_when_currency_changes():
    a = compute_classifier_fingerprint(_ext(currency="CAD"))
    b = compute_classifier_fingerprint(_ext(currency="USD"))
    assert a != b


def test_fingerprint_independent_of_case():
    a = compute_classifier_fingerprint(_ext(vendor_name="Prairie Pump Rentals"))
    b = compute_classifier_fingerprint(_ext(vendor_name="prairie pump rentals"))
    assert a == b


def test_fingerprint_independent_of_unrelated_fields():
    a = compute_classifier_fingerprint(_ext(invoice_number="A"))
    b = compute_classifier_fingerprint(_ext(invoice_number="B"))
    assert a == b


def test_fingerprint_matches_returns_false_for_none_cached():
    assert fingerprint_matches(None, _ext()) is False
    assert fingerprint_matches("", _ext()) is False


def test_fingerprint_matches_returns_true_for_match():
    fp = compute_classifier_fingerprint(_ext())
    assert fingerprint_matches(fp, _ext()) is True


def test_fingerprint_matches_returns_false_for_mismatch():
    fp = compute_classifier_fingerprint(_ext(vendor_name="Acme"))
    assert fingerprint_matches(fp, _ext(vendor_name="Globex")) is False


def test_reset_review_session_clears_expected_keys():
    keys = (
        "parsed_invoice", "extraction", "reviewed_extraction",
        "classification_result", "classification_fingerprint",
        "classification_source_label",
        "reviewed_category", "reviewed_category_suggested",
        "reviewer_name", "review_note",
        "current_invoice_id", "current_invoice_status",
        "current_invoice_payment_state",
        "ack_duplicate", "duplicate_acknowledged_fingerprint",
        "approve_clicked_at", "reject_clicked_at",
        "mark_paid_clicked_at", "last_save_message",
    )
    s = _FakeState()
    for k in keys:
        s[k] = "present"
    s["keep_me"] = "this key must survive"
    reset_review_session(s)
    for k in keys:
        assert k not in s
    assert s["keep_me"] == "this key must survive"


def test_session_has_invoice_returns_false_when_empty():
    s = _FakeState()
    assert session_has_invoice(s) is False


def test_session_has_invoice_true_when_reviewed_extraction_present():
    s = _FakeState()
    s["reviewed_extraction"] = _ext()
    assert session_has_invoice(s) is True


def test_session_has_invoice_true_when_current_invoice_id_present():
    s = _FakeState()
    s["current_invoice_id"] = 42
    assert session_has_invoice(s) is True


def test_reset_is_safe_on_missing_keys():
    s = _FakeState()
    reset_review_session(s)
