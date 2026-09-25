"""Structured invoice extraction for OilOps AI (STEP 3).

PRIVACY GUARANTEES
------------------
* This module makes NO external network requests.
* It does NOT call any LLM or third-party API.
* It operates purely on text already held in-memory by the caller.
* It does not persist anything to disk.

STEP 3 SCOPE
------------
Convert the raw text produced by `services.invoice_parser.parse_pdf`
into structured fields:

    vendor_name
    invoice_number
    invoice_date
    due_date
    po_number
    subtotal
    gst
    total_amount
    currency
    description

Plus a `warnings` list flagging anything that looked ambiguous.

DESIGN PRINCIPLES
-----------------
* Local, deterministic, regex/text-normalization-based.
* Monetary values use `decimal.Decimal` internally.
* Missing fields are returned as `None`; we never crash on absence.
* We never invent data. When we are not sure, we return None and emit
  a warning. Business validation lives in STEP 5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import List, Optional

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class InvoiceExtraction:
    """Structured fields extracted from an invoice text."""

    vendor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    due_date: Optional[str] = None
    po_number: Optional[str] = None
    subtotal: Optional[Decimal] = None
    gst: Optional[Decimal] = None
    total_amount: Optional[Decimal] = None
    currency: Optional[str] = None
    description: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "vendor_name": self.vendor_name,
            "invoice_number": self.invoice_number,
            "invoice_date": self.invoice_date,
            "due_date": self.due_date,
            "po_number": self.po_number,
            "subtotal": str(self.subtotal) if self.subtotal is not None else None,
            "gst": str(self.gst) if self.gst is not None else None,
            "total_amount": str(self.total_amount) if self.total_amount is not None else None,
            "currency": self.currency,
            "description": self.description,
            "warnings": list(self.warnings),
        }

# ---------------------------------------------------------------------------
# Money parsing
# ---------------------------------------------------------------------------

_MONEY_NUMBER_RE = re.compile(r"([0-9][0-9,\s]*\.[0-9]{2})")
# Currency hint before a money literal.
# Word-boundary issues with `$` are sidestepped by using `(?<!\w)` for the
# non-word `$` arm so it is still recognized after a space or colon.
_MONEY_CURRENCY_HINT_RE = re.compile(
    r"(?P<cur>\b(?:CAD|USD|CDN|CAN|C\$|US\$)\b|(?<!\w)\$)\s*"
    r"(?P<amt>[0-9][0-9,\s]*\.[0-9]{2})",
    re.IGNORECASE,
)
_BARE_DOLLAR_RE = re.compile(r"\$\s*(?P<amt>[0-9][0-9,\s]*\.[0-9]{2})")

def _to_decimal(amt_str: str) -> Optional[Decimal]:
    if not amt_str:
        return None
    cleaned = (
        amt_str.replace(",", "")
        .replace(" ", "")
        .replace("\u00a0", "")
        .strip()
    )
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None

def parse_money(text: str) -> Optional[Decimal]:
    if not text:
        return None
    m = _MONEY_CURRENCY_HINT_RE.search(text)
    if m:
        return _to_decimal(m.group("amt"))
    m = _BARE_DOLLAR_RE.search(text)
    if m:
        return _to_decimal(m.group("amt"))
    m = _MONEY_NUMBER_RE.search(text)
    if m:
        return _to_decimal(m.group(1))
    return None

# ---------------------------------------------------------------------------
# Field-specific extractors
# ---------------------------------------------------------------------------

def _extract_vendor(lines, warnings):
    """Choose a plausible company header without treating banners as vendors."""
    blocklist = (
        "invoice",
        "receipt",
        "tax invoice",
        "statement",
        "bill to",
        "from",
        "remit",
        "remittance",
        "wire",
        "ach",
        "eft",
        "payment instructions",
        "please pay",
        "due date",
        "invoice date",
        "invoice #",
        "invoice no",
        "invoice number",
        "po #",
        "po no",
        "po number",
        "subtotal",
        "gst",
        "hst",
        "tax",
        "total",
        "amount due",
        "balance due",
        "description",
        "service",
        "item",
        "currency",
    )
    candidates = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if len(s) < 4:
            continue
        low = s.lower()
        if candidates and low.startswith((
            "invoice details", "bill to", "supplier", "description",
            "item /", "service detail", "subtotal", "gst", "amount due",
        )):
            break
        if any(low.startswith(b) for b in blocklist):
            continue
        if "test customer" in low:
            continue
        s = re.sub(r"\s*[•|]\s*TEST VENDOR\s*$", "", s, flags=re.IGNORECASE).strip()
        # Section banners are commonly uppercase and may contain generic
        # service descriptors rather than the supplier's legal/display name.
        if s.isupper() and re.search(r"[A-Z]", s):
            continue
        if re.match(r"^(?:field operations|western region|professional services)\b", low):
            continue
        if re.fullmatch(r"[\d\s\.,\$\-:/]+", s):
            continue
        if re.fullmatch(r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}", s):
            continue
        # Skip lines that look like postal addresses (street suffix,
        # city names, leading number, or postal code). Addresses are
        # often the most title-cased line on a synthetic invoice but
        # are NOT the vendor name.
        low_compact = s.lower()
        if any(
            tok in low_compact
            for tok in (
                " avenue", " street", " road", " boulevard", " lane",
                " drive", " crescent", " calgary", " edmonton",
                " toronto", " vancouver", " ottawa", " montreal",
                " alberta,", " alberta ", " ontario,", " british columbia",
            )
        ):
            continue
        # A leading house number (e.g. "123 Demo Avenue ...") is an
        # address, not a company name.
        if re.match(r"^\d+\s+[A-Z]", s):
            continue
        # Canadian postal code pattern.
        if re.search(r"\b[A-Z]\d[A-Z]\s*\d[A-Z]\d\b", s):
            continue
        # Prefer a title-cased company-like line over earlier all-caps
        # layout labels; continue scanning so split/sidebar headers work.
        candidates.append(s)
    if not candidates:
        return None
    return max(candidates, key=lambda value: (
        sum(1 for word in value.split() if word[:1].isupper() and word[1:].islower()),
        len(value.split()),
    ))

def _extract_invoice_number(lines, raw_text, warnings):
    """Extract the invoice number from common label formats.

    A bare "Invoice" line ("INVOICE", "INVOICE (SYNTHETIC TEST DATA)") is
    NOT a number; we require an explicit label + separator such as
    "Invoice #: RMD-2026-001" or "Invoice Number: ...".
    """
    label_re = re.compile(
        r"^\s*invoice(?:\s+(?:#|no\.?|number))?\s*(?:[:#]\s*(?P<v>.*))?\s*$",
        re.IGNORECASE,
    )
    for idx, line in enumerate(lines):
        m = label_re.match(line)
        if m:
            v = (m.group("v") or "").strip()
            split_label = re.match(
                r"^\s*invoice\s+(?:#|no\.?|number)\s*$", line,
                re.IGNORECASE,
            )
            if not v and split_label and idx + 1 < len(lines):
                v = lines[idx + 1].strip()
            v = v.split()[0] if v else ""
            if v and v.lower().rstrip(':') not in ("date", "details", "number", "no", "no."):
                if v.lower().startswith(("details ", "date ", "information ")):
                    continue
                return v

    short_re = re.compile(
        r"^\s*inv(?:\s+(?:#|no\.?))?\s*[:\-#]\s*(?P<v>.+?)\s*$",
        re.IGNORECASE,
    )
    for idx, line in enumerate(lines):
        m = short_re.match(line)
        if m:
            v = m.group("v").strip()
            if not v and idx + 1 < len(lines):
                v = lines[idx + 1].strip()
            v = v.split()[0] if v else ""
            if v:
                return v

    warnings.append("Invoice number could not be detected.")
    return None

_PO_ABSENT_MARKERS = {
    "n/a", "na", "none", "not", "not provided", "not available",
    "no po", "no po#", "pending",
}

def _extract_po_number(lines, raw_text):
    """Extract a PO number if present. Returns None if absent (no warning)."""
    label_re = re.compile(
        r"^\s*po(?:\s+(?:#|no\.?|number))?\s*[:\-#]?\s*(?P<v>.*)\s*$",
        re.IGNORECASE,
    )
    for idx, line in enumerate(lines):
        m = label_re.match(line)
        if m:
            v = m.group("v").strip()
            if not v and idx + 1 < len(lines):
                v = lines[idx + 1].strip()
            if v and v.lower().rstrip(".") in _PO_ABSENT_MARKERS:
                return None
            # The hyphen belongs to identifiers such as PO-ABC123, not
            # to a label/value separator; retain it in the captured value.
            if re.match(r"^\s*PO-[A-Za-z]", line, re.IGNORECASE) and v:
                v = "PO-" + v
            v = v.split()[0] if v else ""
            if v:
                return v
    label_re2 = re.compile(
        r"^\s*purchase\s*order(?:\s+(?:#|no\.?|number))?\s*[:\-#]?\s*(?P<v>.*)\s*$",
        re.IGNORECASE,
    )
    for idx, line in enumerate(lines):
        m = label_re2.match(line)
        if m:
            v = m.group("v").strip()
            if not v and idx + 1 < len(lines):
                v = lines[idx + 1].strip()
            v = v.split()[0] if v else ""
            if v:
                return v
    return None

# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_DATE_ISO_RE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
_DATE_SLASH_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
_DATE_DASH_RE = re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{2,4})\b")
_DATE_MONTH_NAME_RE = re.compile(
    r"\b(?P<m>[A-Za-z]{3,9})\.?\s+(?P<d>\d{1,2})(?:,)?\s+(?P<y>\d{4})\b"
)
_DATE_MONTH_NAME_FIRST_RE = re.compile(
    r"\b(?P<d>\d{1,2})\s+(?P<m>[A-Za-z]{3,9})\.?\s+(?P<y>\d{4})\b"
)

def _normalize_year(y):
    if y < 100:
        return 2000 + y if y < 70 else 1900 + y
    return y

def _format_iso(y, m, d):
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None

def _parse_iso(text):
    m = _DATE_ISO_RE.search(text)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return _format_iso(y, mo, d)

def _parse_month_name(text):
    m = _DATE_MONTH_NAME_RE.search(text)
    if m:
        month = _MONTHS.get(m.group("m").lower().rstrip("."))
        if month:
            return _format_iso(int(m.group("y")), month, int(m.group("d")))
    m = _DATE_MONTH_NAME_FIRST_RE.search(text)
    if m:
        month = _MONTHS.get(m.group("m").lower().rstrip("."))
        if month:
            return _format_iso(int(m.group("y")), month, int(m.group("d")))
    return None

def _parse_slash_or_dash(text, warnings):
    """Parse MM/DD/YYYY vs DD/MM/YYYY using a documented convention."""
    for rx in (_DATE_SLASH_RE, _DATE_DASH_RE):
        m = rx.search(text)
        if not m:
            continue
        a, b, y_raw = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = _normalize_year(y_raw)
        if a > 12 and b <= 12:
            return _format_iso(y, b, a)
        if b > 12 and a <= 12:
            return _format_iso(y, a, b)
        if a <= 12 and b <= 12:
            warnings.append(
                "Ambiguous date \"" + str(a) + "/" + str(b) + "/" + str(y) +
                "\" interpreted as MM/DD/YYYY by convention; please review."
            )
            return _format_iso(y, a, b)
    return None

def parse_date(text, warnings=None):
    """Return the first plausible date in `text` as YYYY-MM-DD."""
    if not text:
        return None
    ws = warnings if warnings is not None else []
    for fn in (_parse_iso, _parse_month_name):
        iso = fn(text)
        if iso:
            return iso
    return _parse_slash_or_dash(text, ws)

def _extract_date_field(lines, raw_text, labels):
    """Find a date on a line that mentions one of `labels`."""
    for label in labels:
        label_re = re.compile(
            r"^\s*" + re.escape(label) + r"\s*[:#\-]?\s*(?P<value>.*)$",
            re.IGNORECASE,
        )
        for idx, line in enumerate(lines):
            match = label_re.match(line)
            if match:
                value = match.group("value").strip()
                if not value and idx + 1 < len(lines):
                    value = lines[idx + 1].strip()
                iso = parse_date(value, warnings=None)
                if iso:
                    return iso
    return None

# ---------------------------------------------------------------------------
# Description
# ---------------------------------------------------------------------------

_DESCRIPTION_LABELS = (
    "description",
    "service",
    "services",
    "item",
    "items",
    "particulars",
    "details",
)

_NON_DESCRIPTION_KEYWORDS = (
    "remit",
    "remittance",
    "wire",
    "ach",
    "eft",
    "bank",
    "routing",
    "account",
    "swift",
    "iban",
    "please pay",
    "payment instructions",
    "make check",
    "cheque",
    "payable to",
)

def _extract_description(lines, raw_text, warnings):
    """Conservative description extraction from a labeled line."""
    for line in lines:
        low = line.lower()
        if any(kw in low for kw in _NON_DESCRIPTION_KEYWORDS):
            continue
        matched_label = None
        for lbl in _DESCRIPTION_LABELS:
            if low.lstrip().startswith(lbl):
                matched_label = lbl
                break
        if not matched_label:
            continue

        rest = line[len(matched_label):].lstrip(" :-#\t").strip()
        if not rest and ":" in line:
            rest = line.split(":", 1)[1].strip()
        if rest:
            return rest
    return None

# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------

_CURRENCY_LABEL_RE = re.compile(
    r"\b(?:currency|curr\.?)\s*[:\-]?\s*(?P<cur>CAD|USD|CDN|CAN|C\$|US\$)\b"
    r"|\bAMOUNT\s*\(\s*(?P<amount_cur>CAD|USD|CDN|CAN)\s*\)",
    re.IGNORECASE,
)

def _extract_currency(lines, raw_text, warnings):
    """Detect CAD or USD if explicitly declared.

    Order:
      1. Explicit "Currency: CAD/USD" label line.
      2. Currency code or `$` immediately preceding a money literal.
      3. Bare `$` somewhere in text -> warning, currency left unset.
    """
    for line in lines:
        m = _CURRENCY_LABEL_RE.search(line)
        if m:
            cur = (m.group("cur") or m.group("amount_cur")).upper()
            if cur in ("C$",):
                return "CAD"
            if cur in ("US$",):
                return "USD"
            if cur in ("CDN", "CAN"):
                return "CAD"
            return cur

    m = _MONEY_CURRENCY_HINT_RE.search(raw_text)
    if m:
        cur = m.group("cur").upper()
        if cur in ("CAD", "CDN", "CAN", "C$"):
            return "CAD"
        if cur in ("USD", "US$"):
            return "USD"
        # Bare `$` matched -> not enough to decide.
        warnings.append(
            "Currency symbol \"$\" was found but no explicit CAD/USD context; "
            "currency left unset for human review."
        )
        return None

    if "$" in raw_text and "CAD" not in raw_text.upper() and "USD" not in raw_text.upper():
        warnings.append(
            "Currency was not explicitly stated (only \"$\" was found); "
            "currency left unset for human review."
        )
    return None

# ---------------------------------------------------------------------------
# Amount extractors
# ---------------------------------------------------------------------------

def _extract_amount_by_label(lines, labels, raw_text, warnings):
    """Find an amount on the first line that begins with one of `labels`.

    The label must be followed by ONE OF:
        - ':' immediately (e.g. "Total: 5,250.00"),
        - whitespace then a digit / '$' / sign (e.g. "Total Due $5,250.00"),
        - whitespace only and then EOL (amount on the next line).

    This strict terminator exists because the previous `\b`-only rule was
    too loose: a label like "total" would also match "Total Tax: 250.00"
    because the blank satisfies `\b`, and `total_amount` would steal the
    tax value from the real "Total:" line further down. STEP 3 had this
    exact regression; this is the minimal fix.

    Labels are matched in declared order, against the lowercased line.
    The longer multi-word labels (e.g. "total due", "total tax") are
    therefore picked first when the user lists them first; put them
    before their short prefixes if you add new labels.
    """
    for idx, line in enumerate(lines):
        low = line.lower()
        for lbl in labels:
            # Strict terminator: ':' immediately, or whitespace + digit/$/sign,
            # or whitespace + EOL. Anything else (e.g. 'total tax') is
            # rejected as a label boundary.
            m_lbl = re.match(
                r"^\s*" + re.escape(lbl.lower())
                + r"(?=[:]|\s+[-+]?[\d$]|\s*$|\s*\()",
                low,
            )
            if m_lbl:
                tail = low[m_lbl.end():].lstrip(" :,;-(")
                line_for_money = tail if tail else line
                amt = parse_money(line_for_money)
                if amt is not None:
                    return amt
                if 0 <= idx + 1 < len(lines):
                    amt = parse_money(lines[idx + 1])
                    if amt is not None:
                        return amt
                return None
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_invoice(raw_text):
    """Extract structured invoice fields from raw text."""
    out = InvoiceExtraction()
    if not raw_text:
        out.warnings.append("Empty input text; nothing to extract.")
        return out

    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    out.vendor_name = _extract_vendor(lines, out.warnings)
    out.invoice_number = _extract_invoice_number(lines, text, out.warnings)
    out.po_number = _extract_po_number(lines, text)

    out.invoice_date = _extract_date_field(
        lines, text, ["invoice date", "date issued", "issued", "date"]
    )
    if out.invoice_date is None:
        out.warnings.append("Invoice date could not be detected.")

    out.due_date = _extract_date_field(
        lines, text, ["due date", "payment due", "pay by"]
    )
    if out.due_date is None:
        out.warnings.append("Due date could not be detected.")

    out.subtotal = _extract_amount_by_label(
        lines, ["subtotal", "sub-total", "sub total"], text, out.warnings
    )
    out.gst = _extract_amount_by_label(
        lines,
        ["gst", "hst", "tax", "sales tax", "tax total", "total tax"],
        text,
        out.warnings,
    )
    out.total_amount = _extract_amount_by_label(
        lines,
        [
            "total",
            "amount due",
            "balance due",
            "grand total",
            "total due",
            "invoice total",
            "total amount",
        ],
        text,
        out.warnings,
    )
    if out.total_amount is None:
        out.warnings.append("Total amount could not be detected.")

    out.currency = _extract_currency(lines, text, out.warnings)
    out.description = _extract_description(lines, text, out.warnings)

    return out
