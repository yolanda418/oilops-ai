"""Deterministic invoice validation for OilOps AI (STEP 5).

PRIVACY GUARANTEES
------------------
* This module makes NO external network requests.
* It does NOT call any LLM or third-party API.
* It is fully deterministic: no `datetime.now()`, no random IDs.
  Every check either accepts an explicit `reference_date` or uses
  no date at all.
* It does NOT persist anything to disk on its own. The duplicate
  query is read-only.
* Validation operates only on the structured `InvoiceExtraction`
  business fields. It deliberately never touches raw invoice text,
  bank account numbers, routing numbers, SWIFT, IBAN, email, or
  phone values. Those fields are not part of `InvoiceExtraction`,
  so they cannot leak into `ValidationIssue` messages.

STEP 5 SCOPE
------------
On top of STEP 3 (structured extraction) and STEP 4 (privacy filter
+ human-reviewed extraction), this step performs **deterministic,
local, explainable** business validation. Every issue has:

    - a stable `code` string for downstream UI / persistence
    - a human-readable `message`
    - a `severity` (error / warning / info)
    - an optional `field` it pertains to

The checker is intentionally NOT allowed to:
    * reject, delete or auto-cancel an invoice
    * make payment decisions
    * invent business policy (e.g. a fixed "$25k = suspicious" rule)
      that the prototype cannot actually justify

DESIGN PRINCIPLES
-----------------
* Local, deterministic, fully testable without I/O.
* Monetary arithmetic uses `decimal.Decimal` with a cent-level
  tolerance of `Decimal("0.01")`. No `float`.
* Date comparisons use `datetime.date`. The caller passes
  `reference_date` so tests stay deterministic.
* Duplicate detection queries a SQLite connection with
  parameterised SQL. Strings are NEVER concatenated into SQL.
* Duplicate detection returns *minimal metadata* (id + business
  triplet). It deliberately does not return raw text or sensitive
  fields.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, List, Optional, Sequence

from .invoice_extractor import InvoiceExtraction


# ---------------------------------------------------------------------------
# Severity / Codes
# ---------------------------------------------------------------------------

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

ALL_SEVERITIES: tuple = (SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO)


# Stable validation codes. Keep this list explicit so downstream UI
# and (future) persistence can switch on them.
CODE_POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
CODE_MISSING_INVOICE_NUMBER = "MISSING_INVOICE_NUMBER"
CODE_MISSING_PO = "MISSING_PO"
CODE_MISSING_DUE_DATE = "MISSING_DUE_DATE"
CODE_DUE_SOON = "DUE_SOON"
CODE_OVERDUE = "OVERDUE"
CODE_AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
CODE_ZERO_AMOUNT = "ZERO_AMOUNT"
CODE_NEGATIVE_AMOUNT = "NEGATIVE_AMOUNT"
CODE_LARGE_AMOUNT_REVIEW = "LARGE_AMOUNT_REVIEW"


# Cent-level tolerance for arithmetic checks. Anything within this
# delta is treated as equal. Anything beyond it is flagged.
AMOUNT_TOLERANCE = Decimal("0.01")

# A due_date within this many days of the reference_date is "due soon".
# Mutually exclusive with OVERDUE: an invoice that is already past due
# is flagged as OVERDUE, not DUE_SOON.
DUE_SOON_DAYS = 7


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ValidationIssue:
    """A single, explainable validation finding.

    Attributes
    ----------
    code: stable string identifier (see module constants).
    severity: one of "error", "warning", "info".
    message: human-readable explanation, safe to render directly.
    field: optional structured field name the issue pertains to.
    """

    code: str
    severity: str
    message: str
    field: Optional[str] = None


@dataclass
class InvoiceValidationResult:
    """Aggregate of all validation issues for one invoice."""

    issues: List[ValidationIssue] = field(default_factory=list)

    # Summary helpers (computed, not stored separately, so they can
    # never drift out of sync with `issues`).
    @property
    def issue_count(self) -> int:
        return len(self.issues)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == SEVERITY_ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == SEVERITY_WARNING)

    @property
    def info_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == SEVERITY_INFO)

    @property
    def has_errors(self) -> bool:
        return self.error_count > 0

    @property
    def has_warnings(self) -> bool:
        return self.warning_count > 0

    @property
    def has_infos(self) -> bool:
        return self.info_count > 0

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)

    def codes(self) -> List[str]:
        return [i.code for i in self.issues]

    def by_code(self, code: str) -> List[ValidationIssue]:
        return [i for i in self.issues if i.code == code]

    def as_dict(self) -> dict:
        """Plain-dict projection, safe for JSON / AI-safe payload reuse.

        NOTE: this projection is *metadata only* (code, severity,
        field, message). It deliberately never contains the raw
        invoice text, bank account numbers, routing, SWIFT, IBAN,
        email, or phone. Those fields are not part of the input
        `InvoiceExtraction` in the first place.
        """
        return {
            "issue_count": self.issue_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
            "issues": [
                {
                    "code": i.code,
                    "severity": i.severity,
                    "message": i.message,
                    "field": i.field,
                }
                for i in self.issues
            ],
        }


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _parse_iso_date(s: Optional[str]) -> Optional[date]:
    """Parse an ISO-8601 date string (``YYYY-MM-DD``).

    Returns ``None`` on any parse failure or non-string input. We do
    not raise: missing / unparseable dates are surfaced via the
    validation pipeline, not as exceptions.
    """
    if not isinstance(s, str) or not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Duplicate detection (database-side, read-only, parameterised SQL)
# ---------------------------------------------------------------------------


# Minimal projection we return from a duplicate hit. Deliberately
# excludes raw_text, bank_account, routing_number, swift, iban,
# email, phone. The caller never asked us for those, and we never
# ship them back.
_DUPLICATE_PROJECTION = (
    "id, vendor_name, invoice_number, total_amount"
)


def find_duplicate_invoices(
    conn: sqlite3.Connection,
    vendor_name: Optional[str],
    invoice_number: Optional[str],
    total_amount: Optional[Decimal],
) -> List[sqlite3.Row]:
    """Return invoice rows that match the (vendor, invoice_number,
    total_amount) business triplet.

    Matching rules
    --------------
    * All three fields must be present and non-empty on the input
      side; otherwise the triplet is incomplete and we return ``[]``.
      This is by design: we cannot meaningfully claim "possible
      duplicate" without at least two of three components.
    * `vendor_name` and `invoice_number` are matched exactly
      (case-sensitive). The original database column stores
      whatever the parser produced; humans can correct it in STEP 3.
    * `total_amount` is matched on the exact Decimal value. We do
      NOT round or apply tolerance here; rounding would let
      5250.00 and 5250.01 collapse into a single hit and confuse
      the reviewer.

    The query is fully parameterised. Strings are NEVER concatenated
    into SQL. The returned rows contain only the safe projection
    listed in `_DUPLICATE_PROJECTION`.
    """
    if conn is None:
        return []
    if not vendor_name or not invoice_number or total_amount is None:
        return []

    sql = (
        "SELECT " + _DUPLICATE_PROJECTION + " "
        "FROM invoices "
        "WHERE vendor_name = ? "
        "  AND invoice_number = ? "
        "  AND total_amount = ? "
        "ORDER BY id"
    )
    # Decimal must be serialised to float / str for the sqlite driver.
    # We compare on the exact numeric value, so the serialisation is
    # safe (SQLite REAL preserves the value for these magnitudes).
    cur = conn.execute(
        sql,
        (vendor_name, invoice_number, float(total_amount)),
    )
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Per-check helpers
# ---------------------------------------------------------------------------


def _check_missing_invoice_number(ext: InvoiceExtraction) -> List[ValidationIssue]:
    inv = (ext.invoice_number or "").strip()
    if inv:
        return []
    return [
        ValidationIssue(
            code=CODE_MISSING_INVOICE_NUMBER,
            severity=SEVERITY_ERROR,
            message=(
                "Invoice number is missing. "
                "An invoice without a number cannot be reliably matched "
                "or paid."
            ),
            field="invoice_number",
        )
    ]


def _check_missing_po(ext: InvoiceExtraction) -> List[ValidationIssue]:
    po = (ext.po_number or "").strip()
    if po:
        return []
    return [
        ValidationIssue(
            code=CODE_MISSING_PO,
            severity=SEVERITY_WARNING,
            message=(
                "PO number is missing \u2014 please confirm whether a PO "
                "is required for this invoice."
            ),
            field="po_number",
        )
    ]


def _check_missing_due_date(ext: InvoiceExtraction) -> List[ValidationIssue]:
    if ext.due_date:
        return []
    return [
        ValidationIssue(
            code=CODE_MISSING_DUE_DATE,
            severity=SEVERITY_WARNING,
            message=(
                "Due date is missing. Without a due date, payment "
                "planning and overdue tracking are not possible."
            ),
            field="due_date",
        )
    ]


def _check_due_status(
    ext: InvoiceExtraction,
    reference_date: Optional[date],
) -> List[ValidationIssue]:
    """Compute DUE_SOON / OVERDUE / nothing, mutually exclusive."""
    if reference_date is None:
        # Without a reference date we cannot reason about due status.
        return []
    parsed = _parse_iso_date(ext.due_date)
    if parsed is None:
        return []  # missing-due-date is reported separately

    days = (parsed - reference_date).days
    if days < 0:
        return [
            ValidationIssue(
                code=CODE_OVERDUE,
                severity=SEVERITY_WARNING,
                message=(
                    "Invoice is overdue (due " + str(parsed)
                    + ", reference " + str(reference_date) + ")."
                ),
                field="due_date",
            )
        ]
    if days <= DUE_SOON_DAYS:
        return [
            ValidationIssue(
                code=CODE_DUE_SOON,
                severity=SEVERITY_WARNING,
                message=(
                    "Invoice is due within " + str(days) + " day"
                    + ("s" if days != 1 else "")
                    + " (due " + str(parsed) + ")."
                ),
                field="due_date",
            )
        ]
    return []


def _check_amounts(ext: InvoiceExtraction) -> List[ValidationIssue]:
    """Subtotal + GST ~= Total. Zero / negative totals."""
    out: List[ValidationIssue] = []

    subtotal: Optional[Decimal] = ext.subtotal
    gst: Optional[Decimal] = ext.gst
    total: Optional[Decimal] = ext.total_amount

    # Sign / zero checks first \u2014 they take precedence over the
    # arithmetic check (a negative total is not "arithmetic mismatch").
    if total is not None and total < 0:
        out.append(
            ValidationIssue(
                code=CODE_NEGATIVE_AMOUNT,
                severity=SEVERITY_WARNING,
                message=(
                    "Negative total amount detected \u2014 confirm whether "
                    "this is a credit note."
                ),
                field="total_amount",
            )
        )
    elif total is not None and total == 0:
        out.append(
            ValidationIssue(
                code=CODE_ZERO_AMOUNT,
                severity=SEVERITY_WARNING,
                message=(
                    "Total amount is zero. Confirm the invoice has been "
                    "finalised and is not a draft."
                ),
                field="total_amount",
            )
        )

    # Arithmetic check. We need subtotal AND gst AND total to even
    # attempt this. If any one is missing, the check is skipped
    # silently \u2014 "missing value" is already surfaced elsewhere.
    if (
        subtotal is not None
        and gst is not None
        and total is not None
    ):
        diff = (subtotal + gst) - total
        if abs(diff) > AMOUNT_TOLERANCE:
            out.append(
                ValidationIssue(
                    code=CODE_AMOUNT_MISMATCH,
                    severity=SEVERITY_WARNING,
                    message=(
                        "Subtotal + GST does not match Total. "
                        "subtotal=" + str(subtotal)
                        + ", gst=" + str(gst)
                        + ", total=" + str(total)
                        + ", diff=" + str(diff) + "."
                    ),
                    field="total_amount",
                )
            )

    return out


def _check_large_amount(
    ext: InvoiceExtraction,
    large_amount_threshold: Optional[Decimal],
) -> List[ValidationIssue]:
    """Optional / configurable policy.

    The prototype deliberately does NOT bake in a fixed dollar
    threshold. We only flag `LARGE_AMOUNT_REVIEW` when the caller
    explicitly passes a threshold. Without one, the system stays
    silent here.
    """
    if large_amount_threshold is None:
        return []
    if ext.total_amount is None:
        return []
    threshold = Decimal(large_amount_threshold)
    if ext.total_amount >= threshold:
        return [
            ValidationIssue(
                code=CODE_LARGE_AMOUNT_REVIEW,
                severity=SEVERITY_INFO,
                message=(
                    "Total amount (" + str(ext.total_amount)
                    + ") is at or above the configured review threshold "
                    "(" + str(threshold) + "). "
                    "Confirm policy compliance before approval."
                ),
                field="total_amount",
            )
        ]
    return []


def _check_possible_duplicate(
    ext: InvoiceExtraction,
    db_conn: Optional[sqlite3.Connection],
) -> List[ValidationIssue]:
    """POSSIBLE_DUPLICATE is `info` severity.

    The system NEVER rejects, deletes or auto-cancels a duplicate.
    """
    if db_conn is None:
        return []
    matches = find_duplicate_invoices(
        db_conn,
        ext.vendor_name,
        ext.invoice_number,
        ext.total_amount,
    )
    if not matches:
        return []
    match_ids = ", ".join(str(row["id"]) for row in matches)
    return [
        ValidationIssue(
            code=CODE_POSSIBLE_DUPLICATE,
            severity=SEVERITY_INFO,
            message=(
                "Possible duplicate of existing record"
                + ("s" if len(matches) != 1 else "")
                + " (invoice id " + match_ids + "). "
                + "The duplicate has been kept in the database; "
                "human review is required."
            ),
            field="invoice_number",
        )
    ]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def check_invoice(
    extraction: InvoiceExtraction,
    *,
    db_conn: Optional[sqlite3.Connection] = None,
    reference_date: Optional[date] = None,
    large_amount_threshold: Optional[Decimal] = None,
) -> InvoiceValidationResult:
    """Run all validation checks against a structured invoice.

    Parameters
    ----------
    extraction: structured invoice fields (STEP 3). If the human
        reviewer has already corrected values, pass the *corrected*
        extraction here \u2014 we never re-parse.
    db_conn: optional SQLite connection for duplicate detection.
        Pass ``None`` to skip the duplicate check entirely. Strings
        are NEVER concatenated into SQL.
    reference_date: optional explicit "today" for due-date checks.
        Tests MUST pass an explicit value to stay deterministic.
        If omitted, due-date checks (DUE_SOON / OVERDUE) are
        skipped silently.
    large_amount_threshold: optional Decimal. When ``None``
        (default), the LARGE_AMOUNT_REVIEW check is a no-op. This
        is by design: the prototype does not pretend to know a
        company's actual large-amount policy.
    """
    if extraction is None:
        # Defensive: an empty extraction is not a normal call site,
        # but we still return a clean empty result rather than crash.
        return InvoiceValidationResult()

    issues: List[ValidationIssue] = []
    issues.extend(_check_missing_invoice_number(extraction))
    issues.extend(_check_missing_po(extraction))
    issues.extend(_check_missing_due_date(extraction))
    issues.extend(_check_due_status(extraction, reference_date))
    issues.extend(_check_amounts(extraction))
    issues.extend(_check_large_amount(extraction, large_amount_threshold))
    issues.extend(_check_possible_duplicate(extraction, db_conn))

    return InvoiceValidationResult(issues=issues)


# ---------------------------------------------------------------------------
# Convenience: human-readable summary string for the UI
# ---------------------------------------------------------------------------


def format_validation_summary(result: InvoiceValidationResult) -> str:
    """Return a single-line summary such as ``"0 Errors, 2 Warnings, 1 Info"``.

    Always renders all three counts even when zero, so the human
    reviewer can see the full picture.
    """
    return (
        str(result.error_count) + " Errors, "
        + str(result.warning_count) + " Warnings, "
        + str(result.info_count) + " Info"
    )
