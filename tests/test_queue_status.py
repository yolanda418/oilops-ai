"""Tests for services.queue_status (Batch 2).

Pure unit tests over the deterministic derive_queue_status function.
No I/O. No DB. No network.
"""
from __future__ import annotations

import pytest

from services.invoice_checker import (
    InvoiceValidationResult,
    ValidationIssue,
    CODE_AMOUNT_MISMATCH,
    CODE_MISSING_DUE_DATE,
    CODE_MISSING_INVOICE_NUMBER,
    CODE_MISSING_PO,
    CODE_POSSIBLE_DUPLICATE,
    CODE_OVERDUE,
    CODE_DUE_SOON,
    CODE_ZERO_AMOUNT,
    CODE_NEGATIVE_AMOUNT,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    SEVERITY_INFO,
)
from services.queue_status import (
    ALL_QUEUE_STATUSES,
    QUEUE_STATUS_READY,
    QUEUE_STATUS_NEEDS_REVIEW,
    QUEUE_STATUS_POSSIBLE_DUPLICATE,
    QUEUE_STATUS_FAILED,
    derive_queue_status,
    queue_status_priority,
)


def _issue(code, severity=SEVERITY_WARNING):
    return ValidationIssue(
        code=code, severity=severity, message="m", field=None,
    )


def test_all_queue_statuses_constant_is_complete():
    assert set(ALL_QUEUE_STATUSES) == {
        QUEUE_STATUS_READY,
        QUEUE_STATUS_NEEDS_REVIEW,
        QUEUE_STATUS_POSSIBLE_DUPLICATE,
        QUEUE_STATUS_FAILED,
    }


def test_derive_failed_when_parse_status_is_not_ok():
    v = InvoiceValidationResult()
    for ps in (None, "empty", "unsupported", "failed", "anything"):
        assert (
            derive_queue_status(parse_status=ps, validation=v)
            == QUEUE_STATUS_FAILED
        )


def test_derive_ready_when_parse_ok_and_no_validation_issues():
    v = InvoiceValidationResult()
    assert (
        derive_queue_status(parse_status="ok", validation=v)
        == QUEUE_STATUS_READY
    )


def test_derive_ready_when_validation_is_none():
    assert (
        derive_queue_status(parse_status="ok", validation=None)
        == QUEUE_STATUS_READY
    )


def test_derive_possible_duplicate_takes_priority():
    """Duplicate detection must beat generic Needs Review."""
    v = InvoiceValidationResult(issues=[
        _issue(CODE_POSSIBLE_DUPLICATE, severity=SEVERITY_INFO),
        _issue(CODE_MISSING_PO, severity=SEVERITY_WARNING),
        _issue(CODE_AMOUNT_MISMATCH, severity=SEVERITY_WARNING),
    ])
    assert (
        derive_queue_status(parse_status="ok", validation=v)
        == QUEUE_STATUS_POSSIBLE_DUPLICATE
    )


def test_derive_needs_review_for_missing_po():
    v = InvoiceValidationResult(issues=[
        _issue(CODE_MISSING_PO, severity=SEVERITY_WARNING),
    ])
    assert (
        derive_queue_status(parse_status="ok", validation=v)
        == QUEUE_STATUS_NEEDS_REVIEW
    )


def test_derive_needs_review_for_amount_mismatch():
    v = InvoiceValidationResult(issues=[
        _issue(CODE_AMOUNT_MISMATCH, severity=SEVERITY_WARNING),
    ])
    assert (
        derive_queue_status(parse_status="ok", validation=v)
        == QUEUE_STATUS_NEEDS_REVIEW
    )


def test_derive_needs_review_for_missing_due_date():
    v = InvoiceValidationResult(issues=[
        _issue(CODE_MISSING_DUE_DATE, severity=SEVERITY_WARNING),
    ])
    assert (
        derive_queue_status(parse_status="ok", validation=v)
        == QUEUE_STATUS_NEEDS_REVIEW
    )


def test_derive_needs_review_for_overdue():
    v = InvoiceValidationResult(issues=[
        _issue(CODE_OVERDUE, severity=SEVERITY_WARNING),
    ])
    assert (
        derive_queue_status(parse_status="ok", validation=v)
        == QUEUE_STATUS_NEEDS_REVIEW
    )


def test_derive_needs_review_for_due_soon():
    v = InvoiceValidationResult(issues=[
        _issue(CODE_DUE_SOON, severity=SEVERITY_WARNING),
    ])
    assert (
        derive_queue_status(parse_status="ok", validation=v)
        == QUEUE_STATUS_NEEDS_REVIEW
    )


def test_derive_needs_review_for_zero_amount():
    v = InvoiceValidationResult(issues=[
        _issue(CODE_ZERO_AMOUNT, severity=SEVERITY_WARNING),
    ])
    assert (
        derive_queue_status(parse_status="ok", validation=v)
        == QUEUE_STATUS_NEEDS_REVIEW
    )


def test_derive_needs_review_for_negative_amount():
    v = InvoiceValidationResult(issues=[
        _issue(CODE_NEGATIVE_AMOUNT, severity=SEVERITY_WARNING),
    ])
    assert (
        derive_queue_status(parse_status="ok", validation=v)
        == QUEUE_STATUS_NEEDS_REVIEW
    )


def test_derive_needs_review_for_missing_invoice_number_error():
    v = InvoiceValidationResult(issues=[
        _issue(CODE_MISSING_INVOICE_NUMBER, severity=SEVERITY_ERROR),
    ])
    assert (
        derive_queue_status(parse_status="ok", validation=v)
        == QUEUE_STATUS_NEEDS_REVIEW
    )


def test_derive_failed_beats_duplicate():
    """A failed parse must short-circuit even if duplicate could be set."""
    v = InvoiceValidationResult(issues=[
        _issue(CODE_POSSIBLE_DUPLICATE, severity=SEVERITY_INFO),
    ])
    assert (
        derive_queue_status(parse_status="failed", validation=v)
        == QUEUE_STATUS_FAILED
    )


def test_queue_status_priority_orders_failed_first():
    p_failed = queue_status_priority(QUEUE_STATUS_FAILED)
    p_dup = queue_status_priority(QUEUE_STATUS_POSSIBLE_DUPLICATE)
    p_review = queue_status_priority(QUEUE_STATUS_NEEDS_REVIEW)
    p_ready = queue_status_priority(QUEUE_STATUS_READY)
    assert p_failed < p_dup < p_review < p_ready


def test_queue_status_priority_unknown_returns_high_number():
    assert queue_status_priority("Bogus") > queue_status_priority(QUEUE_STATUS_READY)
