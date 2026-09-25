"""Tests for the structured invoice extractor (STEP 3).

All fixtures are SYNTHETIC. No real vendor data is used.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from services.invoice_extractor import (
    InvoiceExtraction,
    extract_invoice,
    parse_date,
    parse_money,
)


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------


FULL_INVOICE = (
    "INVOICE (SYNTHETIC TEST DATA)\n"
    "Rocky Mountain Drilling Services (SYNTHETIC TEST DATA)\n"
    "Invoice #: RMD-2026-001\n"
    "PO #: PO-1042\n"
    "Invoice Date: 2026-01-15\n"
    "Due Date: 2026-02-15\n"
    "Description: Field service rig rental - January 2026 (SYNTHETIC)\n"
    "Subtotal: $10,000.00\n"
    "GST: $500.00\n"
    "Total: $10,500.00"
)


MISSING_PO_INVOICE = (
    "Northern Well Services Inc.\n"
    "Invoice Number: NWS-7788\n"
    "Invoice Date: 2026-03-01\n"
    "Due Date: 2026-03-31\n"
    "Subtotal: $2,400.00\n"
    "GST: $120.00\n"
    "Total: $2,520.00"
)


MISSING_DUE_DATE_INVOICE = (
    "Prairie Field Equipment Ltd.\n"
    "Invoice #: PFE-2026-099\n"
    "Invoice Date: September 1, 2026\n"
    "Subtotal: $1,000.00\n"
    "GST: $50.00\n"
    "Total: $1,050.00"
)


EXPLICIT_CAD_INVOICE = (
    "Rocky Mountain Drilling Services\n"
    "Invoice #: RMD-2026-002\n"
    "Invoice Date: 2026-09-01\n"
    "Due Date: 2026-10-01\n"
    "PO #: PO-1043\n"
    "Description: Field service rig rental\n"
    "Subtotal: CAD 10,000.00\n"
    "GST: CAD 500.00\n"
    "Total: CAD 10,500.00"
)


EXPLICIT_USD_INVOICE = (
    "Northern Well Services Inc.\n"
    "Invoice #: NWS-USD-9\n"
    "Invoice Date: 2026-09-01\n"
    "Due Date: 2026-10-01\n"
    "Description: Service work\n"
    "Subtotal: USD 4,200.00\n"
    "Total: USD 4,410.00"
)


DIFFERENT_DATE_FORMAT_INVOICE = (
    "Prairie Field Equipment Ltd.\n"
    "Invoice #: PFE-2026-100\n"
    "Invoice Date: 09/01/2026\n"
    "Due Date: 10/01/2026\n"
    "Subtotal: $1,000.00\n"
    "GST: $50.00\n"
    "Total: $1,050.00"
)


DIFFERENT_AMOUNT_FORMAT_INVOICE = (
    "Northern Well Services Inc.\n"
    "Invoice #: NWS-AMT-1\n"
    "Invoice Date: 2026-09-01\n"
    "Due Date: 2026-10-01\n"
    "Subtotal: 10 500.00\n"
    "Total: 10 500.00"
)


BANK_FOOTER_INVOICE = (
    "Rocky Mountain Drilling Services\n"
    "Invoice #: RMD-2026-200\n"
    "Invoice Date: 2026-09-01\n"
    "Due Date: 2026-10-01\n"
    "Description: Rig time charge\n"
    "Subtotal: $5,000.00\n"
    "GST: $250.00\n"
    "Total: $5,250.00\n"
    "\n"
    "Remit To: Bank of Calgary\n"
    "Routing: 123456789\n"
    "Account: 987654321\n"
    "Please pay by EFT.\n"
)


# ---------------------------------------------------------------------------
# parse_money
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$10,500.00", Decimal("10500.00")),
        ("10,500.00", Decimal("10500.00")),
        ("CAD 10,500.00", Decimal("10500.00")),
        ("USD 10,500.00", Decimal("10500.00")),
        ("10 500.00", Decimal("10500.00")),
        ("Subtotal: $10,000.00", Decimal("10000.00")),
    ],
)
def test_parse_money_supported_formats(text, expected):
    assert parse_money(text) == expected


def test_parse_money_returns_none_on_no_match():
    assert parse_money("hello world") is None
    assert parse_money("") is None


# ---------------------------------------------------------------------------
# parse_date
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-09-01", "2026-09-01"),
        ("09/01/2026", "2026-09-01"),
        ("10/01/2026", "2026-10-01"),
        ("September 1, 2026", "2026-09-01"),
        ("Sep 1, 2026", "2026-09-01"),
    ],
)
def test_parse_date_supported_formats(text, expected):
    assert parse_date(text) == expected


def test_parse_date_returns_none_on_empty():
    assert parse_date("") is None
    assert parse_date(None) is None


def test_parse_date_disambiguates_when_one_side_over_12():
    assert parse_date("13/01/2026") == "2026-01-13"
    assert parse_date("01/13/2026") == "2026-01-13"


def test_parse_date_ambiguous_emits_warning():
    warnings = []
    result = parse_date("09/01/2026", warnings=warnings)
    assert result == "2026-09-01"
    assert any("MM/DD/YYYY" in w for w in warnings)


# ---------------------------------------------------------------------------
# Full invoice
# ---------------------------------------------------------------------------


def test_extract_full_synthetic_invoice():
    ext = extract_invoice(FULL_INVOICE)
    assert ext.vendor_name == "Rocky Mountain Drilling Services (SYNTHETIC TEST DATA)"
    assert ext.invoice_number == "RMD-2026-001"
    assert ext.invoice_date == "2026-01-15"
    assert ext.due_date == "2026-02-15"
    assert ext.po_number == "PO-1042"
    assert ext.subtotal == Decimal("10000.00")
    assert ext.gst == Decimal("500.00")
    assert ext.total_amount == Decimal("10500.00")
    assert ext.description == "Field service rig rental - January 2026 (SYNTHETIC)"
    assert ext.currency is None
    assert any("Currency" in w for w in ext.warnings)


def test_extract_invoice_dataclass_type():
    ext = extract_invoice(FULL_INVOICE)
    assert isinstance(ext, InvoiceExtraction)
    d = ext.as_dict()
    assert d["subtotal"] == "10000.00"
    assert d["total_amount"] == "10500.00"
    assert isinstance(d["warnings"], list)


# ---------------------------------------------------------------------------
# Missing-field scenarios
# ---------------------------------------------------------------------------


def test_extract_missing_po_returns_none_no_warning():
    ext = extract_invoice(MISSING_PO_INVOICE)
    assert ext.po_number is None
    assert not any("PO" in w for w in ext.warnings)
    assert ext.invoice_number == "NWS-7788"
    assert ext.total_amount == Decimal("2520.00")


def test_extract_missing_due_date_returns_none_with_warning():
    ext = extract_invoice(MISSING_DUE_DATE_INVOICE)
    assert ext.due_date is None
    assert any("Due date" in w for w in ext.warnings)
    assert ext.invoice_date == "2026-09-01"
    assert ext.total_amount == Decimal("1050.00")


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------


def test_extract_explicit_cad():
    ext = extract_invoice(EXPLICIT_CAD_INVOICE)
    assert ext.currency == "CAD"
    assert ext.subtotal == Decimal("10000.00")
    assert ext.total_amount == Decimal("10500.00")


def test_extract_explicit_usd():
    ext = extract_invoice(EXPLICIT_USD_INVOICE)
    assert ext.currency == "USD"
    assert ext.subtotal == Decimal("4200.00")
    assert ext.total_amount == Decimal("4410.00")


def test_extract_bare_dollar_is_unknown_with_warning():
    ext = extract_invoice(FULL_INVOICE)
    assert ext.currency is None
    assert any(
        "Currency" in w
        and ("not explicitly stated" in w or "no explicit CAD/USD context" in w)
        for w in ext.warnings
    )


# ---------------------------------------------------------------------------
# Different formats
# ---------------------------------------------------------------------------


def test_extract_different_date_format_normalizes():
    ext = extract_invoice(DIFFERENT_DATE_FORMAT_INVOICE)
    assert ext.invoice_date == "2026-09-01"
    assert ext.due_date == "2026-10-01"


def test_extract_different_amount_format_handles_spaces():
    ext = extract_invoice(DIFFERENT_AMOUNT_FORMAT_INVOICE)
    assert ext.subtotal == Decimal("10500.00")
    assert ext.total_amount == Decimal("10500.00")


# ---------------------------------------------------------------------------
# Description safety
# ---------------------------------------------------------------------------


def test_extract_description_does_not_pick_up_bank_footer():
    ext = extract_invoice(BANK_FOOTER_INVOICE)
    assert ext.description == "Rig time charge"
    assert "Routing" not in (ext.description or "")
    assert "Bank of Calgary" not in (ext.description or "")


def test_extract_missing_description_returns_none_no_warning():
    text = (
        "Northern Well Services Inc.\n"
        "Invoice #: NWS-NODESC\n"
        "Invoice Date: 2026-09-01\n"
        "Due Date: 2026-10-01\n"
        "Subtotal: $1,000.00\n"
        "Total: $1,050.00"
    )
    ext = extract_invoice(text)
    assert ext.description is None


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_extract_empty_text_returns_object_with_warning():
    ext = extract_invoice("")
    assert ext.vendor_name is None
    assert any("Empty" in w for w in ext.warnings)


def test_extract_does_not_crash_on_garbage():
    ext = extract_invoice("!@#$%^&*()_+=\n\n\n")
    assert ext.total_amount is None
    assert ext.invoice_number is None


def test_extract_vendor_skips_invoice_header_line():
    ext = extract_invoice(FULL_INVOICE)
    assert ext.vendor_name is not None
    assert not ext.vendor_name.lower().startswith("invoice")


# ---------------------------------------------------------------------------
# Decimal type guarantees
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("attr", ["subtotal", "gst", "total_amount"])
def test_extract_money_fields_are_decimal(attr):
    ext = extract_invoice(FULL_INVOICE)
    val = getattr(ext, attr)
    if val is not None:
        assert isinstance(val, Decimal)


# ---------------------------------------------------------------------------
# STEP 3 regression: amount label disambiguation
# ---------------------------------------------------------------------------
#
# A label like "total" must NOT also match "Total Tax: 250.00" just
# because the blank after "total" satisfies \b. Without the strict
# terminator fix in services/invoice_extractor.py, total_amount would
# steal the tax value (250.00) from the real "Total:" line (5250.00)
# when the document happens to contain a "Total Tax" line.
#
# These fixtures cover both orderings that surfaced during the STEP 3
# hand-off audit.


TOTAL_BEFORE_SUBTOTAL = (
    "INVOICE (SYNTHETIC TEST DATA)\n"
    "Prairie Field Equipment Ltd. (SYNTHETIC TEST DATA)\n"
    "Invoice #: PFE-2026-009\n"
    "PO #: PO-8821\n"
    "Invoice Date: 2026-09-22\n"
    "Due Date: 2026-10-22\n"
    "Description: Pump equipment rental\n"
    "Total Due: CAD 5,250.00\n"
    "\n"
    "Subtotal: CAD 5,000.00\n"
    "GST: CAD 250.00\n"
)


TOTAL_TAX_BEFORE_TOTAL = (
    "INVOICE (SYNTHETIC TEST DATA)\n"
    "Prairie Field Equipment Ltd. (SYNTHETIC TEST DATA)\n"
    "Invoice #: PFE-2026-010\n"
    "PO #: PO-8830\n"
    "Invoice Date: 2026-09-22\n"
    "Due Date: 2026-10-22\n"
    "Description: Service rig day rate\n"
    "Subtotal: CAD 5,000.00\n"
    "Total Tax: CAD 250.00\n"
    "Total: CAD 5,250.00\n"
)


def test_total_line_before_subtotal_is_not_swapped():
    """Total Due line appearing BEFORE the Subtotal line must not poison
    the subtotal value. Subtotal must still be the actual subtotal."""
    ext = extract_invoice(TOTAL_BEFORE_SUBTOTAL)
    assert ext.subtotal == Decimal("5000.00")
    assert ext.total_amount == Decimal("5250.00")
    assert ext.gst == Decimal("250.00")


def test_total_tax_line_does_not_steal_total_amount():
    """A 'Total Tax: 250.00' line must not steal the total_amount from
    the real 'Total: 5250.00' line further down."""
    ext = extract_invoice(TOTAL_TAX_BEFORE_TOTAL)
    assert ext.subtotal == Decimal("5000.00")
    assert ext.gst == Decimal("250.00")
    assert ext.total_amount == Decimal("5250.00")


def test_total_due_is_recognized_as_total():
    """The 'total due' label must still be recognized as a total line
    when no plain 'Total:' is present, proving we did not over-correct
    and break the longer multi-word label."""
    text = (
        "INVOICE (SYNTHETIC TEST DATA)\n"
        "Prairie Field Equipment Ltd.\n"
        "Invoice #: PFE-2026-011\n"
        "Subtotal: CAD 1,000.00\n"
        "GST: CAD 50.00\n"
        "Total Due: CAD 1,050.00\n"
    )
    ext = extract_invoice(text)
    assert ext.total_amount == Decimal("1050.00")


def test_multiline_labels_preserve_invoice_and_po_values():
    ext = extract_invoice(
        "Prairie Field Equipment Ltd.\n"
        "INVOICE DETAILS\nInvoice Number\nPFE-2026-101\n"
        "Invoice Date\n2026-09-01\nDue Date\n2026-10-01\n"
        "PO Number\nPO-TEST-4101\n"
        "Subtotal\n1,000.00\nGST\n50.00\nTotal\n1,050.00\n"
        "AMOUNT (CAD)"
    )
    assert ext.invoice_number == "PFE-2026-101"
    assert ext.invoice_date == "2026-09-01"
    assert ext.due_date == "2026-10-01"
    assert ext.po_number == "PO-TEST-4101"
    assert ext.gst == Decimal("50.00")
    assert ext.currency == "CAD"


@pytest.mark.parametrize("currency", ["CAD", "USD"])
def test_currency_from_amount_column_header(currency):
    ext = extract_invoice(
        "Test Services Ltd.\nInvoice #: T-1\n"
        f"AMOUNT ({currency})\nSubtotal\n100.00\nGST\n5.00\nTotal\n105.00"
    )
    assert ext.currency == currency


def test_vendor_skips_generic_uppercase_layout_banner():
    ext = extract_invoice(
        "WESTERN REGION • PROFESSIONAL SERVICES\n"
        "Western Environmental Demo\nINVOICE\n"
        "INVOICE DETAILS\nINVOICE NUMBER\nWED-1\n"
        "Subtotal\n100.00\nGST\n5.00\nTOTAL DUE\n105.00"
    )
    assert ext.vendor_name == "Western Environmental Demo"

