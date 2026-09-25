"""STEP 9 - Weekly Office Operations Summary (deterministic facts + AI narrative)."""
from __future__ import annotations

import hashlib, json, logging, os, re, sqlite3, ssl
import urllib.error, urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("oilops.weekly_summary")

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

WEEK_LENGTH_DAYS = 7
WEEK_LOOKBACK_DAYS = WEEK_LENGTH_DAYS - 1

ALLOWED_CURRENCY_CODES = ("CAD", "USD")

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
OPENAI_TIMEOUT_SECONDS = 20
OPENAI_MAX_RETRIES = 1
MAX_NARRATIVE_LEN = 4000


class WeeklySummaryError(Exception):
    pass


class PayloadSafetyError(WeeklySummaryError):
    pass


class ProviderError(WeeklySummaryError):
    pass


@dataclass
class CategoryAmount:
    category: str
    currency: str
    invoice_count: int
    total_amount: Decimal


@dataclass
class DuplicateGroupSummary:
    vendor_name: Optional[str]
    invoice_number: Optional[str]
    total_amount: Optional[Decimal]
    invoice_count: int


@dataclass
class WeeklyFacts:
    reference_date: date
    period_start: date
    period_end: date
    recorded_this_week_count: int = 0
    recorded_amounts: Dict[str, Decimal] = field(default_factory=dict)
    pending_review_count: int = 0
    outstanding_amounts: Dict[str, Decimal] = field(default_factory=dict)
    paid_this_week_amounts: Dict[str, Decimal] = field(default_factory=dict)
    due_next_7_days_count: int = 0
    overdue_count: int = 0
    possible_duplicate_groups: int = 0
    duplicate_groups: List[DuplicateGroupSummary] = field(default_factory=list)
    approved_spend_by_category: List[CategoryAmount] = field(default_factory=list)
    attention_count: int = 0

    def currencies(self):
        seen = []
        for c in (list(self.recorded_amounts.keys())
                  + list(self.outstanding_amounts.keys())
                  + list(self.paid_this_week_amounts.keys())):
            if c not in seen:
                seen.append(c)
        return sorted(seen)



# Payload allow-list + hard-ban list

_WEEKLY_PAYLOAD_ALLOWED_FIELDS = (
    "period_start",
    "period_end",
    "recorded_this_week_count",
    "recorded_amounts",
    "pending_review_count",
    "outstanding",
    "paid_this_week",
    "due_next_7_days_count",
    "overdue_count",
    "possible_duplicate_groups",
    "approved_spend_by_category",
    "attention_count",
)


_WEEKLY_PAYLOAD_FORBIDDEN_FIELDS = (
    "raw_text",
    "vendor_name",
    "invoice_number",
    "po_number",
    "invoice_date",
    "description",
    "subtotal",
    "gst",
    "extraction_json",
    "validation_flags",
    "source_filename",
    "bank_account",
    "routing_number",
    "swift",
    "iban",
    "payment_instructions",
    "payment_note",
    "email",
    "phone",
    "reviewer",
    "review_note",
    "invoice_id",
    "id",
    "paid_at",
    "created_at",
    "updated_at",
    "status",
)


def _to_decimal(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        try:
            return Decimal(str(v))
        except Exception:
            return None
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return Decimal(s)
    except Exception:
        return None


def _iso(d):
    return d.isoformat()


def _normalise_period(reference_date):
    period_end = reference_date
    period_start = reference_date - timedelta(days=WEEK_LOOKBACK_DAYS)
    return period_start, period_end


def _is_safe_currency(code):
    if code is None:
        return None
    s = str(code).strip().upper()
    if s in ALLOWED_CURRENCY_CODES:
        return s
    return None


# SQL aggregations


def _recorded_this_week(conn, period_start_iso, period_end_iso):
    cur = conn.execute(
        "SELECT currency, SUM(total_amount) AS total, COUNT(*) AS c "
        "FROM invoices "
        "WHERE substr(created_at, 1, 10) >= ? "
        "  AND substr(created_at, 1, 10) <= ? "
        "GROUP BY currency",
        (period_start_iso, period_end_iso),
    )
    rows = cur.fetchall()
    counts = 0
    amounts = {}
    for row in rows:
        ccy = _is_safe_currency(row["currency"])
        if ccy is None:
            continue
        cnt = int(row["c"] or 0)
        v = _to_decimal(row["total"])
        if v is None:
            v = Decimal("0")
        counts += cnt
        amounts[ccy] = amounts.get(ccy, Decimal("0")) + v
    return counts, amounts


def _pending_review_count(conn):
    cur = conn.execute(
        "SELECT COUNT(*) AS c FROM invoices WHERE status = ?",
        (STATUS_PENDING,),
    )
    row = cur.fetchone()
    if row is None:
        return 0
    return int(row["c"] or 0)


def _outstanding_amounts(conn):
    cur = conn.execute(
        "SELECT currency, SUM(total_amount) AS total "
        "FROM invoices "
        "WHERE status = ? AND paid_at IS NULL "
        "GROUP BY currency",
        (STATUS_APPROVED,),
    )
    out = {}
    for row in cur.fetchall():
        ccy = _is_safe_currency(row["currency"])
        if ccy is None:
            continue
        v = _to_decimal(row["total"])
        if v is None:
            continue
        out[ccy] = v
    return out


def _paid_this_week_amounts(conn, period_start_iso, period_end_iso):
    cur = conn.execute(
        "SELECT currency, SUM(total_amount) AS total "
        "FROM invoices "
        "WHERE status = ? AND paid_at IS NOT NULL "
        "  AND substr(paid_at, 1, 10) >= ? "
        "  AND substr(paid_at, 1, 10) <= ? "
        "GROUP BY currency",
        (STATUS_APPROVED, period_start_iso, period_end_iso),
    )
    out = {}
    for row in cur.fetchall():
        ccy = _is_safe_currency(row["currency"])
        if ccy is None:
            continue
        v = _to_decimal(row["total"])
        if v is None:
            continue
        out[ccy] = v
    return out


def _due_next_7_days_count(conn, reference_date):
    cur = conn.execute(
        "SELECT COUNT(*) AS c FROM invoices "
        "WHERE status != ? AND paid_at IS NULL "
        "  AND due_date IS NOT NULL "
        "  AND substr(due_date, 1, 10) >= ? "
        "  AND substr(due_date, 1, 10) <= ?",
        (
            STATUS_REJECTED,
            _iso(reference_date),
            _iso(reference_date + timedelta(days=WEEK_LOOKBACK_DAYS + 1)),
        ),
    )
    row = cur.fetchone()
    if row is None:
        return 0
    return int(row["c"] or 0)


def _overdue_count(conn, reference_date):
    cur = conn.execute(
        "SELECT COUNT(*) AS c FROM invoices "
        "WHERE status != ? AND paid_at IS NULL "
        "  AND due_date IS NOT NULL "
        "  AND substr(due_date, 1, 10) < ?",
        (STATUS_REJECTED, _iso(reference_date)),
    )
    row = cur.fetchone()
    if row is None:
        return 0
    return int(row["c"] or 0)


def _duplicate_group_summaries(conn):
    cur = conn.execute(
        "SELECT vendor_name, invoice_number, total_amount, COUNT(*) AS c "
        "FROM invoices "
        "GROUP BY vendor_name, invoice_number, total_amount "
        "HAVING COUNT(*) > 1 "
        "ORDER BY COUNT(*) DESC, total_amount DESC",
    )
    out = []
    for row in cur.fetchall():
        out.append(DuplicateGroupSummary(
            vendor_name=row["vendor_name"],
            invoice_number=row["invoice_number"],
            total_amount=_to_decimal(row["total_amount"]),
            invoice_count=int(row["c"] or 0),
        ))
    return len(out), out


def _approved_spend_by_category(conn):
    cur = conn.execute(
        "SELECT expense_category AS category, currency, "
        "       COUNT(*) AS c, SUM(total_amount) AS total "
        "FROM invoices "
        "WHERE status = ? "
        "GROUP BY expense_category, currency "
        "ORDER BY expense_category, currency",
        (STATUS_APPROVED,),
    )
    out = []
    for row in cur.fetchall():
        ccy = _is_safe_currency(row["currency"])
        if ccy is None:
            continue
        v = _to_decimal(row["total"])
        if v is None:
            continue
        cat = row["category"]
        if not cat:
            cat = "(Uncategorised)"
        out.append(CategoryAmount(
            category=str(cat),
            currency=ccy,
            invoice_count=int(row["c"] or 0),
            total_amount=v,
        ))
    return out


def compute_weekly_facts(conn, reference_date):
    """Compute WeeklyFacts from a SQLite connection.

    The caller controls reference_date so unit tests are deterministic.
    """
    if not isinstance(reference_date, date):
        raise ValueError(
            "reference_date must be a datetime.date instance, got "
            + type(reference_date).__name__
        )
    period_start, period_end = _normalise_period(reference_date)
    period_start_iso = _iso(period_start)
    period_end_iso = _iso(period_end)

    rec_count, rec_amounts = _recorded_this_week(
        conn, period_start_iso, period_end_iso
    )
    pending = _pending_review_count(conn)
    outstanding = _outstanding_amounts(conn)
    paid_w = _paid_this_week_amounts(conn, period_start_iso, period_end_iso)
    due = _due_next_7_days_count(conn, reference_date)
    overdue_n = _overdue_count(conn, reference_date)
    dup_count, dup_groups = _duplicate_group_summaries(conn)
    by_cat = _approved_spend_by_category(conn)

    attention = (
        int(pending) + int(due) + int(overdue_n) + int(dup_count)
    )

    return WeeklyFacts(
        reference_date=reference_date,
        period_start=period_start,
        period_end=period_end,
        recorded_this_week_count=rec_count,
        recorded_amounts=rec_amounts,
        pending_review_count=pending,
        outstanding_amounts=outstanding,
        paid_this_week_amounts=paid_w,
        due_next_7_days_count=due,
        overdue_count=overdue_n,
        possible_duplicate_groups=dup_count,
        duplicate_groups=dup_groups,
        approved_spend_by_category=by_cat,
        attention_count=attention,
    )


# AI-safe aggregate payload


def _serialise_money(d):
    if d is None:
        return None
    return format(d, "f")


def build_weekly_summary_payload(facts):
    """Build an AI-safe aggregate payload from WeeklyFacts.

    The payload contains ONLY top-level fields on the allow-list. No
    invoice-level raw data, no per-row identifiers, no contact info,
    no bank fields.
    """
    recorded_amounts = {
        ccy: _serialise_money(v)
        for ccy, v in sorted(facts.recorded_amounts.items())
        if ccy in ALLOWED_CURRENCY_CODES
    }
    outstanding = {
        ccy: _serialise_money(v)
        for ccy, v in sorted(facts.outstanding_amounts.items())
        if ccy in ALLOWED_CURRENCY_CODES
    }
    paid = {
        ccy: _serialise_money(v)
        for ccy, v in sorted(facts.paid_this_week_amounts.items())
        if ccy in ALLOWED_CURRENCY_CODES
    }

    cat_breakdown = []
    for c in facts.approved_spend_by_category:
        if c.currency not in ALLOWED_CURRENCY_CODES:
            continue
        cat_breakdown.append({
            "category": str(c.category),
            "currency": c.currency,
            "invoice_count": int(c.invoice_count),
            "total_amount": _serialise_money(c.total_amount),
        })

    payload = {
        "period_start": _iso(facts.period_start),
        "period_end": _iso(facts.period_end),
        "recorded_this_week_count": int(facts.recorded_this_week_count),
        "recorded_amounts": recorded_amounts,
        "pending_review_count": int(facts.pending_review_count),
        "outstanding": outstanding,
        "paid_this_week": paid,
        "due_next_7_days_count": int(facts.due_next_7_days_count),
        "overdue_count": int(facts.overdue_count),
        "possible_duplicate_groups": int(facts.possible_duplicate_groups),
        "approved_spend_by_category": cat_breakdown,
        "attention_count": int(facts.attention_count),
    }

    # Hard guarantee: forbidden fields NEVER appear in the payload.
    for forbidden in _WEEKLY_PAYLOAD_FORBIDDEN_FIELDS:
        payload.pop(forbidden, None)
    # Hard guarantee: only allow-listed top-level keys remain.
    payload = {k: v for k, v in payload.items()
               if k in _WEEKLY_PAYLOAD_ALLOWED_FIELDS}

    return payload


def assert_weekly_summary_payload_safe(payload):
    """Raise PayloadSafetyError if payload contains forbidden fields.

    Privacy gate: BLOCKS, does NOT strip.
    """
    if not isinstance(payload, dict):
        raise PayloadSafetyError(
            "Weekly summary payload must be a dict, got "
            + type(payload).__name__
        )

    forbidden_present = [k for k in payload.keys()
                         if k in _WEEKLY_PAYLOAD_FORBIDDEN_FIELDS]
    if forbidden_present:
        raise PayloadSafetyError(
            "Weekly summary payload contains forbidden field(s) "
            + repr(forbidden_present) + ". AI call blocked."
        )

    unknown = [k for k in payload.keys()
               if k not in _WEEKLY_PAYLOAD_ALLOWED_FIELDS]
    if unknown:
        raise PayloadSafetyError(
            "Weekly summary payload contains unknown field(s) "
            + repr(unknown) + ". AI call blocked."
        )

    cat_list = payload.get("approved_spend_by_category")
    if cat_list is not None:
        if not isinstance(cat_list, list):
            raise PayloadSafetyError(
                "approved_spend_by_category must be a list."
            )
        cat_allowed = {"category", "currency", "invoice_count", "total_amount"}
        for i, item in enumerate(cat_list):
            if not isinstance(item, dict):
                raise PayloadSafetyError(
                    "approved_spend_by_category[" + str(i) + "] is not a dict."
                )
            bad_keys = [k for k in item.keys() if k not in cat_allowed]
            if bad_keys:
                raise PayloadSafetyError(
                    "approved_spend_by_category[" + str(i)
                    + "] contains non-allow-listed keys: " + repr(bad_keys)
                )


def compute_weekly_payload_fingerprint(payload):
    """Stable SHA-256 fingerprint of the aggregate payload.

    If weekly facts change, payload changes, fingerprint changes.
    """
    s = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# Deterministic fallback narrative (NO network)


def _fmt_amount_pair(d):
    if d is None:
        return "0.00"
    return format(d, ",.2f")


def _join_currency_lines(amounts):
    if not amounts:
        return "none recorded"
    parts = []
    for ccy in sorted(amounts.keys()):
        if ccy not in ALLOWED_CURRENCY_CODES:
            continue
        parts.append(ccy + " " + _fmt_amount_pair(amounts[ccy]))
    return " / ".join(parts) if parts else "none recorded"


def build_deterministic_weekly_summary(facts):
    """Build a deterministic management summary from WeeklyFacts.

    This NEVER calls any external service. It is the guaranteed,
    always-available narrative for the Weekly Management Summary.
    """
    lines = []
    lines.append("Weekly Office Operations Summary")
    lines.append(
        "Period: " + _iso(facts.period_start)
        + " to " + _iso(facts.period_end)
    )
    lines.append("")
    lines.append(
        "Recorded this week: "
        + str(facts.recorded_this_week_count)
        + " invoice(s). Amounts by currency: "
        + _join_currency_lines(facts.recorded_amounts) + "."
    )
    lines.append(
        "Pending review: "
        + str(facts.pending_review_count)
        + " invoice(s) awaiting human review."
    )
    if facts.outstanding_amounts:
        lines.append(
            "Outstanding (approved, unpaid): "
            + _join_currency_lines(facts.outstanding_amounts) + "."
        )
    else:
        lines.append("Outstanding (approved, unpaid): none.")
    if facts.paid_this_week_amounts:
        lines.append(
            "Recorded as paid this week: "
            + _join_currency_lines(facts.paid_this_week_amounts) + "."
        )
    else:
        lines.append("Recorded as paid this week: none.")
    lines.append(
        "Due next 7 days: "
        + str(facts.due_next_7_days_count)
        + " invoice(s)."
    )
    lines.append(
        "Overdue: "
        + str(facts.overdue_count)
        + " invoice(s)."
    )
    lines.append(
        "Possible duplicate groups: "
        + str(facts.possible_duplicate_groups)
        + "."
    )
    if facts.approved_spend_by_category:
        cat_lines = []
        for c in facts.approved_spend_by_category:
            if c.currency not in ALLOWED_CURRENCY_CODES:
                continue
            cat_lines.append(
                c.category + " ("
                + c.currency + ") "
                + str(c.invoice_count) + " inv(s), "
                + c.currency + " " + _fmt_amount_pair(c.total_amount)
            )
        if cat_lines:
            lines.append("Approved spend by category:")
            for cl in cat_lines:
                lines.append("  - " + cl)
        else:
            lines.append("Approved spend by category: none.")
    else:
        lines.append("Approved spend by category: none.")

    lines.append("")
    lines.append(
        "Note: amounts above are recorded in OilOps AI. "
        "Recorded-as-paid means the invoice was marked paid in OilOps AI; "
        "no external payment was executed. Multi-currency totals are "
        "kept separate by currency."
    )
    return "\n".join(lines)


# AI narrative providers (optional)


_WEEKLY_SYSTEM_PROMPT = (
    "You write concise weekly office operations summaries for a small oil "
    "and gas company.\n"
    "Use ONLY the facts supplied in the structured data.\n"
    "Do not calculate new totals.\n"
    "Do not infer missing facts.\n"
    "Do not predict cash flow or future spending.\n"
    "Do not provide accounting, tax, legal, engineering, or payment advice.\n"
    "Do not approve or reject invoices.\n"
    "Do not state that OilOps AI executed a payment.\n"
    "Preserve currency separation.\n"
    "Never combine amounts from different currencies.\n"
    "Return concise management prose only."
)


def _build_user_message(payload):
    body = json.dumps(payload, indent=2, sort_keys=True, default=str)
    return (
        "Weekly facts (JSON):\n"
        + body
        + "\n\nWrite a concise weekly office operations summary based ONLY on "
        + "these facts. Preserve currency separation. Do not introduce new "
        + "numbers."
    )


class NarrativeProvider:
    name = "abstract"

    def narrate(self, payload):
        raise NotImplementedError


class MockNarrativeProvider(NarrativeProvider):
    name = "mock"

    def narrate(self, payload):
        facts = _payload_to_facts_view(payload)
        return build_deterministic_weekly_summary(facts)


def _payload_to_facts_view(payload):
    ref = date.fromisoformat(payload["period_end"])
    start = date.fromisoformat(payload["period_start"])
    f = WeeklyFacts(
        reference_date=ref,
        period_start=start,
        period_end=ref,
        recorded_this_week_count=int(payload.get("recorded_this_week_count", 0)),
        pending_review_count=int(payload.get("pending_review_count", 0)),
        due_next_7_days_count=int(payload.get("due_next_7_days_count", 0)),
        overdue_count=int(payload.get("overdue_count", 0)),
        possible_duplicate_groups=int(payload.get("possible_duplicate_groups", 0)),
        attention_count=int(payload.get("attention_count", 0)),
    )
    for ccy, val in (payload.get("recorded_amounts") or {}).items():
        f.recorded_amounts[str(ccy)] = _to_decimal(val) or Decimal("0")
    for ccy, val in (payload.get("outstanding") or {}).items():
        f.outstanding_amounts[str(ccy)] = _to_decimal(val) or Decimal("0")
    for ccy, val in (payload.get("paid_this_week") or {}).items():
        f.paid_this_week_amounts[str(ccy)] = _to_decimal(val) or Decimal("0")
    for item in (payload.get("approved_spend_by_category") or []):
        try:
            f.approved_spend_by_category.append(
                CategoryAmount(
                    category=str(item["category"]),
                    currency=str(item["currency"]),
                    invoice_count=int(item.get("invoice_count", 0)),
                    total_amount=_to_decimal(item.get("total_amount")) or Decimal("0"),
                )
            )
        except (KeyError, TypeError):
            continue
    return f


class OpenAINarrativeProvider(NarrativeProvider):
    name = "openai"

    def __init__(self, *, api_key,
                 model=OPENAI_DEFAULT_MODEL,
                 timeout=OPENAI_TIMEOUT_SECONDS,
                 max_retries=OPENAI_MAX_RETRIES):
        if not api_key or not isinstance(api_key, str):
            raise ProviderError("OpenAI provider requires a non-empty api_key.")
        self._api_key = api_key
        self._model = model
        self._timeout = float(timeout)
        self._max_retries = int(max(max_retries, 0))

    def narrate(self, payload):
        assert_weekly_summary_payload_safe(payload)

        body = json.dumps({
            "model": self._model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _WEEKLY_SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_message(payload)},
            ],
        }, separators=(",", ":")).encode("utf-8")

        attempts = self._max_retries + 1
        last_error = None
        for _ in range(attempts):
            try:
                raw = self._post(body)
                return _parse_openai_message(raw)
            except ProviderError as e:
                raise e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_error = e
                continue
        raise ProviderError(
            "OpenAI narrative request failed after "
            + str(attempts) + " attempt(s): "
            + (type(last_error).__name__ if last_error else "unknown")
        )

    def _post(self, body):
        req = urllib.request.Request(
            OPENAI_API_URL, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self._api_key,
            },
        )
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=ctx) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            try:
                _ = e.read()
            except Exception:
                _ = b""
            raise ProviderError(
                "OpenAI HTTP error status=" + str(e.code)
            )
        except urllib.error.URLError as e:
            raise ProviderError(
                "OpenAI URL error: " + type(e.reason).__name__
            )


def _parse_openai_message(raw):
    try:
        outer = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ProviderError(
            "OpenAI narrative response not JSON: " + type(e).__name__
        )
    if not isinstance(outer, dict):
        raise ProviderError("OpenAI narrative response is not a JSON object.")
    choices = outer.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError("OpenAI narrative response missing 'choices'.")
    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderError("OpenAI narrative 'choices[0]' not an object.")
    msg = first.get("message")
    if not isinstance(msg, dict):
        raise ProviderError("OpenAI narrative response missing 'message'.")
    content = msg.get("content")
    if not isinstance(content, str):
        raise ProviderError("OpenAI narrative response missing 'content'.")
    text = content.strip()
    if len(text) > MAX_NARRATIVE_LEN:
        text = text[: MAX_NARRATIVE_LEN - 3].rstrip() + "..."
    if not text:
        raise ProviderError("OpenAI narrative response was empty.")
    return text


def get_narrative_provider(*, api_key=None, force_mock=False):
    if force_mock:
        return MockNarrativeProvider()
    key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
    if key:
        return OpenAINarrativeProvider(api_key=key)
    return MockNarrativeProvider()


def is_narrative_api_key_configured(api_key=None):
    if api_key:
        return True
    return bool(os.environ.get("OPENAI_API_KEY"))


# Public narrative entry point


def generate_weekly_narrative(facts, *, provider=None, force_mock=False, api_key=None):
    """Return (narrative_text, source).

    ``source`` is one of:
        ``"llm"``  - AI provider succeeded
        ``"mock"`` - deterministic fallback was used

    Privacy gate runs first. ProviderError, PayloadSafetyError, or a
    malformed response triggers the deterministic fallback. No network
    call if no API key.

    NOTE: this function does NOT raise on AI failure. Always returns
    a (text, source) pair. Source tells UI which path was used.
    """
    payload = build_weekly_summary_payload(facts)
    assert_weekly_summary_payload_safe(payload)

    if provider is None:
        provider = get_narrative_provider(api_key=api_key, force_mock=force_mock)

    if provider.name == "mock" or not is_narrative_api_key_configured(api_key):
        return build_deterministic_weekly_summary(facts), "mock"

    try:
        text = provider.narrate(payload)
        if not _is_safe_narrative(text):
            return build_deterministic_weekly_summary(facts), "mock"
        return text, "llm"
    except (ProviderError, PayloadSafetyError):
        return build_deterministic_weekly_summary(facts), "mock"
    except Exception:
        logger.exception("Unexpected error in weekly narrative provider.")
        return build_deterministic_weekly_summary(facts), "mock"


# Lightweight narrative sanity checks


_FORBIDDEN_ADVISORY_PATTERNS = (
    r"\byou should (approve|reject|pay)\b",
    r"\b(invest|buy|sell|short)\b",
    r"\b(accounting advice|tax advice|legal advice)\b",
    r"\bwe recommend\b",
    r"\bOilOps AI (paid|executed|sent|transferred)\b",
    r"\bcash[- ]?flow (forecast|projection)\b",
    r"\b(predicted|projected) (spend|revenue|cash)\b",
)


def _is_safe_narrative(text):
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s:
        return False
    if len(s) > MAX_NARRATIVE_LEN:
        return False
    low = s.lower()
    for pat in _FORBIDDEN_ADVISORY_PATTERNS:
        if re.search(pat, low, flags=re.IGNORECASE):
            return False
    return True


# __all__


__all__ = [
    "ALLOWED_CURRENCY_CODES",
    "CategoryAmount",
    "DuplicateGroupSummary",
    "MockNarrativeProvider",
    "NarrativeProvider",
    "OpenAINarrativeProvider",
    "PayloadSafetyError",
    "ProviderError",
    "WeeklyFacts",
    "WeeklySummaryError",
    "WEEK_LENGTH_DAYS",
    "WEEK_LOOKBACK_DAYS",
    "assert_weekly_summary_payload_safe",
    "build_deterministic_weekly_summary",
    "build_weekly_summary_payload",
    "compute_weekly_facts",
    "compute_weekly_payload_fingerprint",
    "generate_weekly_narrative",
    "get_narrative_provider",
    "is_narrative_api_key_configured",
]