"""Batch Invoice Intake - Queue status rules (Batch 2).

SCOPE
-----
Defines the four queue statuses the Batch Intake Queue UI displays,
and the deterministic function that derives a status from:

  * the local PDF parse result (ok / empty / unsupported / failed)
  * the deterministic InvoiceValidationResult from services.invoice_checker

PRIVACY GUARANTEE
-----------------
This module is pure. It performs NO I/O, NO database access, NO
network calls. It only inspects already-computed, in-memory results.

DUPLICATE BEHAVIOUR
-------------------
"Possible Duplicate" is the HIGHEST priority non-failed status.
When duplicate detection flags an existing record, the file is still
saved (because the human reviewer may decide it is, in fact, a
different invoice that just shares the business triplet). The
"Possible Duplicate" status surfaces the warning; the human must
explicitly acknowledge before approval in Batch 1's Process Invoice
flow. Batch 2 follows the same rule: the file is saved, but its
queue status advertises the duplicate risk so the reviewer sees it
first.

FAILED STATUS
-------------
"Failed" is reserved for cases where the local pipeline could not
produce a usable InvoiceExtraction. This includes:
  * parse_pdf returning status != "ok" (empty / unsupported / failed)
  * uncaught exceptions inside the per-file pipeline
The Batch Intake engine wraps every step in a try/except and marks
the item "Failed" with a stored error string. Other items in the
batch are unaffected.
"""
from __future__ import annotations

from typing import Optional

from .invoice_checker import (
    CODE_AMOUNT_MISMATCH,
    CODE_MISSING_DUE_DATE,
    CODE_MISSING_INVOICE_NUMBER,
    CODE_MISSING_PO,
    CODE_NEGATIVE_AMOUNT,
    CODE_OVERDUE,
    CODE_DUE_SOON,
    CODE_POSSIBLE_DUPLICATE,
    CODE_ZERO_AMOUNT,
    InvoiceValidationResult,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
)


QUEUE_STATUS_READY = "Ready"
QUEUE_STATUS_NEEDS_REVIEW = "Needs Review"
QUEUE_STATUS_POSSIBLE_DUPLICATE = "Possible Duplicate"
QUEUE_STATUS_FAILED = "Failed"

ALL_QUEUE_STATUSES = (
    QUEUE_STATUS_READY,
    QUEUE_STATUS_NEEDS_REVIEW,
    QUEUE_STATUS_POSSIBLE_DUPLICATE,
    QUEUE_STATUS_FAILED,
)


_NEEDS_REVIEW_CODES = (
    CODE_MISSING_INVOICE_NUMBER,
    CODE_MISSING_PO,
    CODE_MISSING_DUE_DATE,
    CODE_AMOUNT_MISMATCH,
    CODE_ZERO_AMOUNT,
    CODE_NEGATIVE_AMOUNT,
    CODE_OVERDUE,
    CODE_DUE_SOON,
)


def _validation_needs_review(validation):
    if validation is None:
        return False
    codes = {i.code for i in validation.issues}
    if any(c in codes for c in _NEEDS_REVIEW_CODES):
        return True
    if any(i.severity == SEVERITY_ERROR for i in validation.issues):
        return True
    if any(i.severity == SEVERITY_WARNING for i in validation.issues):
        return True
    return False


def _validation_is_possible_duplicate(validation):
    if validation is None:
        return False
    return any(i.code == CODE_POSSIBLE_DUPLICATE for i in validation.issues)


def derive_queue_status(
    *,
    parse_status,
    validation,
):
    """Compute the Queue status for one batch item, deterministically.

    Precedence (highest first):
        1. "Failed"               - PDF could not be parsed at all.
        2. "Possible Duplicate"   - duplicate detection hit the DB.
           Reported BEFORE Needs Review so reviewers see it first.
        3. "Needs Review"         - validation produced a warning or error.
        4. "Ready"                - clean parse + clean validation.
    """
    if parse_status is None or parse_status != "ok":
        return QUEUE_STATUS_FAILED
    if _validation_is_possible_duplicate(validation):
        return QUEUE_STATUS_POSSIBLE_DUPLICATE
    if _validation_needs_review(validation):
        return QUEUE_STATUS_NEEDS_REVIEW
    return QUEUE_STATUS_READY


def queue_status_priority(status):
    """Sort key so the Queue UI groups items consistently.

    Order: Failed > Possible Duplicate > Needs Review > Ready.
    Lower number = higher priority (appears first).
    """
    order = {
        QUEUE_STATUS_FAILED: 0,
        QUEUE_STATUS_POSSIBLE_DUPLICATE: 1,
        QUEUE_STATUS_NEEDS_REVIEW: 2,
        QUEUE_STATUS_READY: 3,
    }
    return order.get(status, 99)


__all__ = [
    "ALL_QUEUE_STATUSES",
    "QUEUE_STATUS_FAILED",
    "QUEUE_STATUS_NEEDS_REVIEW",
    "QUEUE_STATUS_POSSIBLE_DUPLICATE",
    "QUEUE_STATUS_READY",
    "derive_queue_status",
    "queue_status_priority",
]
