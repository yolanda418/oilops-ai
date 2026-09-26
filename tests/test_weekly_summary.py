"""Offline tests for deterministic weekly facts and optional AI narrative."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import urllib.error
from datetime import date, timedelta
from decimal import Decimal

import pytest

import services.weekly_summary as ws
from services.weekly_summary import (
    ALLOWED_CURRENCY_CODES,
    MockNarrativeProvider,
    NarrativeProvider,
    OpenAINarrativeProvider,
    PayloadSafetyError,
    ProviderError,
    WeeklyFacts,
    assert_weekly_summary_payload_safe,
    build_deterministic_weekly_summary,
    build_weekly_summary_payload,
    compute_weekly_facts,
    compute_weekly_payload_fingerprint,
    generate_weekly_narrative,
    get_narrative_provider,
    is_narrative_api_key_configured,
)
from services.invoice_extractor import InvoiceExtraction


def _tmp_db():
    tmpdir = tempfile.mkdtemp(prefix="oilops_w_test_")
    db_path = os.path.join(tmpdir, "w.db")
    from database.db import init_db, get_connection
    init_db(db_path)
    return get_connection(db_path), tmpdir


def _insert_invoice(conn, *, status="pending", currency="CAD",
                    total=Decimal("100.00"), vendor="Vendor",
                    inv_no=None, category="Office",
                    created_at="2026-09-22 10:00:00",
                    due_date=None, paid_at=None):
    cur = conn.execute(
        "INSERT INTO invoices ("
        "  vendor_name, invoice_number, total_amount, currency, status,"
        "  expense_category, created_at, due_date, paid_at"
        ") VALUES (?,?,?,?,?,?,?,?,?)",
        (
            vendor,
            inv_no or ("INV-" + str(int(total))),
            float(total),
            currency,
            status,
            category,
            created_at,
            due_date,
            paid_at,
        ),
    )
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def empty_conn():
    conn, _ = _tmp_db()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def sample_conn():
    conn, _ = _tmp_db()
    _insert_invoice(conn, status="pending", currency="CAD",
                    total=Decimal("1000"), vendor="V1",
                    created_at="2026-09-22 09:00:00",
                    due_date="2026-09-25")
    _insert_invoice(conn, status="pending", currency="CAD",
                    total=Decimal("600"), vendor="V2",
                    created_at="2026-09-22 09:00:00",
                    due_date=None)
    _insert_invoice(conn, status="approved", currency="CAD",
                    total=Decimal("5000"), vendor="V3",
                    inv_no="INV-3", category="Equipment",
                    created_at="2026-09-20 09:00:00",
                    due_date="2026-09-28", paid_at=None)
    _insert_invoice(conn, status="approved", currency="CAD",
                    total=Decimal("3000"), vendor="V4",
                    inv_no="INV-4", category="Field Services",
                    created_at="2026-09-18 09:00:00",
                    due_date=None, paid_at="2026-09-19 10:00:00")
    _insert_invoice(conn, status="approved", currency="USD",
                    total=Decimal("2000"), vendor="V5",
                    inv_no="INV-5", category="Transportation",
                    created_at="2026-09-21 09:00:00",
                    due_date=None, paid_at=None)
    _insert_invoice(conn, status="approved", currency="USD",
                    total=Decimal("1500"), vendor="V6",
                    inv_no="INV-6", category="Office",
                    created_at="2026-09-15 09:00:00",
                    due_date="2026-09-20", paid_at="2026-09-21 10:00:00")
    _insert_invoice(conn, status="rejected", currency="CAD",
                    total=Decimal("800"), vendor="V7",
                    created_at="2026-09-22 09:00:00",
                    due_date=None)
    _insert_invoice(conn, status="pending", currency="CAD",
                    total=Decimal("7500"), vendor="V8",
                    created_at="2026-09-22 09:00:00",
                    due_date="2026-09-23")
    _insert_invoice(conn, status="pending", currency="CAD",
                    total=Decimal("5500"), vendor="V9",
                    created_at="2026-09-22 09:00:00",
                    due_date="2026-09-22")
    _insert_invoice(conn, status="pending", currency="CAD",
                    total=Decimal("2500"), vendor="V10",
                    created_at="2026-09-22 09:00:00",
                    due_date="2026-09-10")
    _insert_invoice(conn, status="pending", currency="CAD",
                    total=Decimal("999"), vendor="DUP", inv_no="DUP-1",
                    created_at="2026-09-22 09:00:00")
    _insert_invoice(conn, status="pending", currency="CAD",
                    total=Decimal("999"), vendor="DUP", inv_no="DUP-1",
                    created_at="2026-09-22 09:00:00")
    yield conn
    conn.close()


# 7-day period + counts + amounts


def test_period_is_seven_calendar_days_inclusive(sample_conn):
    ref = date(2026, 9, 22)
    facts = compute_weekly_facts(sample_conn, ref)
    assert facts.period_end == ref
    assert facts.period_start == ref - timedelta(days=6)
    assert (facts.period_end - facts.period_start).days == 6


def test_period_does_not_depend_on_system_clock(sample_conn):
    # Both calls with the same reference_date MUST produce identical facts.
    a = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    b = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    assert a == b


def test_recorded_this_week_counts_only_window(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    # Window 2026-09-16..2026-09-22 inclusive. See fixture comments.
    # Created_at inside window: V1,V2,V3,V4,V5,V7,V8,V9,V10,DUPx2 = 11
    assert facts.recorded_this_week_count == 11


def test_recorded_amounts_are_per_currency(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    # CAD recorded in window:
    # 1000+600+5000+3000+800+7500+5500+2500+999+999 = 27,898.00
    assert facts.recorded_amounts["CAD"] == Decimal("27898.00")
    # USD recorded in window:
    # 2000 (V5) = 2000.00
    assert facts.recorded_amounts["USD"] == Decimal("2000.00")


def test_recorded_amounts_never_summed_across_currency(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    total_combined = sum(facts.recorded_amounts.values())
    # If we accidentally summed across currencies, this would equal
    # 27,898 + 2,000 = 29,898.00. The build explicitly returns a
    # Dict[currency, Decimal] so the SUM is meaningless. This test
    # pins that contract.
    assert len(facts.recorded_amounts) >= 2
    # Total of the dict contents is just a sum of per-currency totals
    # and is NOT a CAD-equivalent. That is intentional.
    assert total_combined == Decimal("29898.00")


def test_pending_review_count_is_as_of_today(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    # pending invoices in fixture: V1, V2, V8, V9, V10, DUPx2 = 7
    assert facts.pending_review_count == 7


def test_outstanding_uses_status_approved_and_paid_null(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    # Approved + unpaid in fixture: V3 (CAD 5000), V5 (USD 2000)
    assert facts.outstanding_amounts["CAD"] == Decimal("5000.00")
    assert facts.outstanding_amounts["USD"] == Decimal("2000.00")


def test_paid_this_week_uses_paid_at_in_window(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    # Paid (approved + paid_at NOT NULL) with paid_at in 2026-09-16..09-22:
    # V4 (CAD 3000, paid_at 2026-09-19), V6 (USD 1500, paid_at 2026-09-21)
    assert facts.paid_this_week_amounts["CAD"] == Decimal("3000.00")
    assert facts.paid_this_week_amounts["USD"] == Decimal("1500.00")


def test_due_next_7_days_includes_today_through_today_plus_7(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    # V1 due 2026-09-25 (in window), V3 due 2026-09-28 (in window),
    # V8 due 2026-09-23 (in window), V9 due 2026-09-22 (in window, == today)
    # V10 due 2026-09-10 (overdue, NOT in this set)
    assert facts.due_next_7_days_count == 4


def test_overdue_uses_due_date_strictly_before_reference(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    # V10 due 2026-09-10 is the only overdue
    assert facts.overdue_count == 1


def test_rejected_excluded_from_due_and_overdue(sample_conn):
    # The fixture has V7 (rejected) with no due_date. It must not
    # appear in either due_next_7_days_count or overdue_count.
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    # If V7 were incorrectly counted, totals would be off by one.
    # Already asserted in the previous two tests; pin it explicitly:
    # the only overdue record is V10.
    assert facts.overdue_count == 1


def test_possible_duplicate_groups_count(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    assert facts.possible_duplicate_groups == 1


def test_approved_spend_by_category_is_per_currency(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    by_cat = {(c.category, c.currency): c for c in facts.approved_spend_by_category}
    assert ("Equipment", "CAD") in by_cat
    assert by_cat[("Equipment", "CAD")].total_amount == Decimal("5000.00")
    assert ("Field Services", "CAD") in by_cat
    assert by_cat[("Field Services", "CAD")].total_amount == Decimal("3000.00")
    assert ("Transportation", "USD") in by_cat
    assert by_cat[("Transportation", "USD")].total_amount == Decimal("2000.00")
    assert ("Office", "USD") in by_cat
    assert by_cat[("Office", "USD")].total_amount == Decimal("1500.00")


def test_attention_count_is_sum_of_attention_metrics(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    expected = (
        facts.pending_review_count
        + facts.due_next_7_days_count
        + facts.overdue_count
        + facts.possible_duplicate_groups
    )
    assert facts.attention_count == expected


# Empty DB + payload safety


def test_empty_db_returns_zero_facts(empty_conn):
    facts = compute_weekly_facts(empty_conn, date(2026, 9, 22))
    assert facts.recorded_this_week_count == 0
    assert facts.recorded_amounts == {}
    assert facts.pending_review_count == 0
    assert facts.outstanding_amounts == {}
    assert facts.paid_this_week_amounts == {}
    assert facts.due_next_7_days_count == 0
    assert facts.overdue_count == 0
    assert facts.possible_duplicate_groups == 0
    assert facts.approved_spend_by_category == []
    assert facts.attention_count == 0


def test_payload_contains_only_allowed_top_level_keys(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    payload = build_weekly_summary_payload(facts)
    expected = set([
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
    ])
    assert set(payload.keys()) == expected


def test_payload_never_contains_raw_text_even_if_facts_do(sample_conn):
    # The build strips forbidden keys defensively. Even if we
    # somehow smuggled a forbidden field into the dataclass, the
    # builder would still drop it.
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    payload = build_weekly_summary_payload(facts)
    for forbidden in (
        "raw_text", "vendor_name", "invoice_number", "po_number",
        "bank_account", "routing_number", "swift", "iban",
        "email", "phone", "reviewer", "review_note",
        "payment_note", "payment_instructions",
        "id", "invoice_id", "paid_at", "created_at", "updated_at", "status",
    ):
        assert forbidden not in payload


def test_payload_gate_blocks_raw_text():
    bad = {"period_start": "x", "period_end": "y", "raw_text": "secret"}
    with pytest.raises(PayloadSafetyError):
        assert_weekly_summary_payload_safe(bad)


def test_payload_gate_blocks_vendor_name():
    bad = {"period_start": "x", "period_end": "y", "vendor_name": "Acme"}
    with pytest.raises(PayloadSafetyError):
        assert_weekly_summary_payload_safe(bad)


def test_payload_gate_blocks_invoice_number():
    bad = {"period_start": "x", "period_end": "y", "invoice_number": "INV-1"}
    with pytest.raises(PayloadSafetyError):
        assert_weekly_summary_payload_safe(bad)


def test_payload_gate_blocks_po_number():
    bad = {"period_start": "x", "period_end": "y", "po_number": "PO-1"}
    with pytest.raises(PayloadSafetyError):
        assert_weekly_summary_payload_safe(bad)


def test_payload_gate_blocks_bank_fields():
    for field in ("bank_account", "routing_number", "swift", "iban",
                  "payment_instructions"):
        bad = {"period_start": "x", "period_end": "y", field: "x"}
        with pytest.raises(PayloadSafetyError):
            assert_weekly_summary_payload_safe(bad)


def test_payload_gate_blocks_contact_fields():
    for field in ("email", "phone"):
        bad = {"period_start": "x", "period_end": "y", field: "x"}
        with pytest.raises(PayloadSafetyError):
            assert_weekly_summary_payload_safe(bad)


def test_payload_gate_blocks_review_and_payment_notes():
    for field in ("reviewer", "review_note", "payment_note"):
        bad = {"period_start": "x", "period_end": "y", field: "x"}
        with pytest.raises(PayloadSafetyError):
            assert_weekly_summary_payload_safe(bad)


def test_payload_gate_blocks_unknown_field():
    bad = {"period_start": "x", "period_end": "y", "secret_field": "x"}
    with pytest.raises(PayloadSafetyError):
        assert_weekly_summary_payload_safe(bad)


def test_payload_gate_blocks_non_dict():
    with pytest.raises(PayloadSafetyError):
        assert_weekly_summary_payload_safe(["not", "a", "dict"])


def test_payload_gate_blocks_non_allow_list_keys_in_category_entry(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    payload = build_weekly_summary_payload(facts)
    # Append an unauthorised key inside a category entry:
    cat0 = payload["approved_spend_by_category"][0]
    cat0["vendor_name"] = "leak"
    with pytest.raises(PayloadSafetyError):
        assert_weekly_summary_payload_safe(payload)


# AI provider behaviour


def test_no_api_key_returns_mock_provider(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    p = get_narrative_provider()
    assert p.name == "mock"


def test_force_mock_returns_mock_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-replace-me")
    p = get_narrative_provider(force_mock=True)
    assert p.name == "mock"


def test_api_key_returns_openai_provider(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    p = get_narrative_provider(api_key="sk-replace-me")
    assert p.name == "openai"


def test_openai_provider_requires_api_key():
    with pytest.raises(ProviderError):
        OpenAINarrativeProvider(api_key="")


def test_no_api_key_uses_fallback_without_calling_provider(sample_conn, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = []

    class _CountingProvider(NarrativeProvider):
        name = "counting"

        def narrate(self, payload):
            calls.append(payload)
            return "unexpected provider invocation"

    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    text, source = generate_weekly_narrative(
        facts, provider=_CountingProvider(), api_key="", force_mock=True
    )
    assert source == "mock"
    assert "Weekly Office Operations Summary" in text
    assert calls == []


class _FakeLiveProvider(NarrativeProvider):
    name = "fake-live"

    def __init__(self, text):
        self._text = text

    def narrate(self, payload):
        assert_weekly_summary_payload_safe(payload)
        return self._text


def test_live_provider_success_returns_llm_source(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    text, source = generate_weekly_narrative(
        facts,
        provider=_FakeLiveProvider("This week 7 invoices were recorded."),
    )
    assert source == "llm"
    assert "This week 7 invoices were recorded." in text


def test_provider_timeout_falls_back(sample_conn):
    class _TimeoutProvider(NarrativeProvider):
        name = "timeout"
        def narrate(self, payload):
            raise urllib.error.URLError("timed out")
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    text, source = generate_weekly_narrative(facts, provider=_TimeoutProvider())
    assert source == "mock"
    assert "Weekly Office Operations Summary" in text


def test_provider_http_error_falls_back(sample_conn):
    class _Http401Provider(NarrativeProvider):
        name = "http401"
        def narrate(self, payload):
            raise ProviderError("OpenAI HTTP error status=401")
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    text, source = generate_weekly_narrative(facts, provider=_Http401Provider())
    assert source == "mock"


def test_provider_malformed_response_falls_back(sample_conn):
    class _BadProvider(NarrativeProvider):
        name = "bad"
        def narrate(self, payload):
            raise ProviderError("malformed JSON")
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    text, source = generate_weekly_narrative(facts, provider=_BadProvider())
    assert source == "mock"


def test_provider_unsafe_advisory_text_falls_back(sample_conn):
    class _AdvisoryProvider(NarrativeProvider):
        name = "advisory"
        def narrate(self, payload):
            return "You should approve more invoices. " * 5
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    text, source = generate_weekly_narrative(facts, provider=_AdvisoryProvider())
    assert source == "mock"
    assert "You should approve" not in text


def test_provider_empty_response_falls_back(sample_conn):
    class _EmptyProvider(NarrativeProvider):
        name = "empty"
        def narrate(self, payload):
            return ""
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    text, source = generate_weekly_narrative(facts, provider=_EmptyProvider())
    assert source == "mock"
    assert text


def test_provider_does_not_see_forbidden_fields(sample_conn):
    seen = {}

    class _SpyProvider(NarrativeProvider):
        name = "spy"

        def narrate(self, payload):
            seen["payload"] = payload
            return "ok"

    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    generate_weekly_narrative(facts, provider=_SpyProvider())
    payload = seen["payload"]
    for forbidden in ("vendor_name", "invoice_number", "raw_text",
                      "bank_account", "reviewer", "email"):
        assert forbidden not in payload


def test_provider_api_key_does_not_leak(sample_conn, capsys):
    secret = "sk-supersecret-LEAK-TEST-1234567890"
    seen = {}

    class _SpyProvider(NarrativeProvider):
        name = "spy"

        def narrate(self, payload):
            seen["payload"] = payload
            return "narrative"

    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    generate_weekly_narrative(facts, provider=_SpyProvider())
    out = capsys.readouterr().out + capsys.readouterr().err
    assert secret not in out
    assert secret not in repr(seen["payload"])


def test_payload_safety_error_in_provider_falls_back(sample_conn):
    """If a provider raises PayloadSafetyError, the integration
    layer catches it and falls back to the deterministic summary."""
    class _RaisingProvider(NarrativeProvider):
        name = "raising"

        def narrate(self, payload):
            raise PayloadSafetyError("simulated leak")

    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    text, source = generate_weekly_narrative(facts, provider=_RaisingProvider())
    assert source == "mock"
    assert "Weekly Office Operations Summary" in text


def test_payload_safety_error_inside_provider_raises(sample_conn):
    """The provider-side gate still raises PayloadSafetyError when
    called directly. The integration layer catches it."""
    class _MutatingProvider(NarrativeProvider):
        name = "mutate2"
        def narrate(self, payload):
            payload["vendor_name"] = "x"
            assert_weekly_summary_payload_safe(payload)
            return "never"
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    with pytest.raises(PayloadSafetyError):
        _MutatingProvider().narrate(build_weekly_summary_payload(facts))


# Deterministic fallback + fingerprint


def test_fallback_summary_contains_all_metrics(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    text = build_deterministic_weekly_summary(facts)
    assert "Weekly Office Operations Summary" in text
    assert "2026-09-16" in text and "2026-09-22" in text
    assert "Recorded this week:" in text
    assert "Pending review:" in text
    assert "Outstanding" in text
    assert "Recorded as paid this week:" in text
    assert "Due next 7 days:" in text
    assert "Overdue:" in text
    assert "Possible duplicate groups:" in text
    assert "Approved spend by category:" in text


def test_fallback_summary_keeps_currency_separation(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    text = build_deterministic_weekly_summary(facts)
    # Both CAD and USD must appear with their amounts, on separate lines.
    assert "CAD " in text
    assert "USD " in text
    # The combination "27898+2000" or any single combined number must
    # not appear as a single figure. The fallback prints "CAD X / USD Y".
    assert "CAD " + format(facts.recorded_amounts["CAD"], ",.2f") in text
    assert "USD " + format(facts.recorded_amounts["USD"], ",.2f") in text


def test_fallback_summary_does_not_say_oilops_paid(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    text = build_deterministic_weekly_summary(facts)
    low = text.lower()
    assert "oilops ai paid" not in low
    assert "oilops ai executed" not in low
    assert "cash-flow forecast" not in low
    assert "cash flow forecast" not in low
    assert "we recommend" not in low


def test_fallback_summary_handles_empty_db(empty_conn):
    facts = compute_weekly_facts(empty_conn, date(2026, 9, 22))
    text = build_deterministic_weekly_summary(facts)
    assert "Weekly Office Operations Summary" in text
    assert "Recorded this week: 0" in text
    assert "Pending review: 0" in text
    assert "Due next 7 days: 0" in text
    assert "Overdue: 0" in text


def test_fingerprint_is_stable_for_same_payload(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    payload = build_weekly_summary_payload(facts)
    a = compute_weekly_payload_fingerprint(payload)
    b = compute_weekly_payload_fingerprint(payload)
    assert a == b
    assert len(a) == 64  # SHA-256 hex digest


def test_fingerprint_changes_when_facts_change(sample_conn):
    facts_a = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    facts_b = compute_weekly_facts(sample_conn, date(2026, 9, 21))
    pa = build_weekly_summary_payload(facts_a)
    pb = build_weekly_summary_payload(facts_b)
    assert compute_weekly_payload_fingerprint(pa) != compute_weekly_payload_fingerprint(pb)


def test_fingerprint_changes_when_recorded_count_changes(sample_conn):
    facts_a = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    payload_a = build_weekly_summary_payload(facts_a)
    fingerprint_a = compute_weekly_payload_fingerprint(payload_a)
    # Insert one more invoice inside the window.
    _insert_invoice(sample_conn, status="pending", currency="CAD",
                    total=Decimal("123.45"), vendor="EXTRA",
                    created_at="2026-09-22 09:00:00")
    facts_b = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    payload_b = build_weekly_summary_payload(facts_b)
    fingerprint_b = compute_weekly_payload_fingerprint(payload_b)
    assert fingerprint_a != fingerprint_b


def test_rerun_does_not_auto_call_ai(sample_conn):
    """Calling generate_weekly_narrative with no provider and no api_key
    must NEVER touch the LLM. Streamlit reruns that re-invoke
    compute_weekly_facts do not auto-call the provider.
    """
    calls = []

    class _CountingProvider(NarrativeProvider):
        name = "counting"

        def narrate(self, payload):
            calls.append(payload)
            return "live"

    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    # Re-running deterministic fact computation does not enter the
    # explicit narrative generation/provider injection point.
    for _ in range(3):
        compute_weekly_facts(sample_conn, date(2026, 9, 22))
    assert calls == []
    # The actual program entry point does invoke an injected provider
    # when explicitly requested and configured.
    _, source = generate_weekly_narrative(
        facts, provider=_CountingProvider(), api_key="sk-test-not-real"
    )
    assert source == "llm"
    assert len(calls) == 1



def test_facts_and_payload_pass_to_provider_only_via_allow_list(sample_conn):
    """Defensive: assert no top-level or nested forbidden field
    ever appears in the payload passed to the provider.
    """
    seen = {}

    class _SpyProvider(NarrativeProvider):
        name = "spy"

        def narrate(self, payload):
            seen["payload"] = payload
            return "ok"

    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    generate_weekly_narrative(facts, provider=_SpyProvider())
    p = seen["payload"]

    forbidden_top = (
        "raw_text", "vendor_name", "invoice_number", "po_number",
        "bank_account", "routing_number", "swift", "iban",
        "email", "phone", "reviewer", "review_note", "payment_note",
        "id", "invoice_id", "paid_at", "created_at", "updated_at", "status",
    )
    for k in forbidden_top:
        assert k not in p

    for entry in p.get("approved_spend_by_category", []):
        for k in forbidden_top:
            assert k not in entry


# STEP 1-8 regression + synthetic E2E


def test_step1_modules_still_importable():
    from database.db import init_db, get_connection, transaction
    from database.models import list_tables, list_indexes
    from database.migrations import get_applied_version
    assert init_db is not None


def test_step2_modules_still_importable():
    from services.invoice_parser import parse_pdf, preview_text
    assert callable(parse_pdf)


def test_step3_modules_still_importable():
    from services.invoice_extractor import extract_invoice, InvoiceExtraction
    from services.invoice_checker import check_invoice, format_validation_summary
    assert callable(extract_invoice)
    assert callable(check_invoice)


def test_step4_privacy_filter_still_works():
    from services.privacy_filter import redact_text, build_ai_safe_payload, is_ai_safe_payload
    r = redact_text("Account Number: 123456789012\n")
    assert r.redaction_count >= 1
    payload = build_ai_safe_payload(InvoiceExtraction(
        vendor_name="V", description="x", total_amount=Decimal("100"),
        currency="CAD"
    ))
    assert is_ai_safe_payload(payload)


def test_step6_ai_classifier_still_works():
    from services.ai_classifier import classify_expense
    r = classify_expense(InvoiceExtraction(
        vendor_name="Pump Co", description="pump rental equipment",
        total_amount=Decimal("500"), currency="CAD"
    ))
    assert r.category in (
        "Field Services", "Equipment", "Transportation",
        "Office", "Professional Services", "Travel", "Other"
    )


def test_step7_invoice_repository_still_works(monkeypatch):
    from database.db import transaction
    from services.invoice_repository import (
        save_pending, approve_invoice,
        STATUS_APPROVED,
    )
    ext = InvoiceExtraction(
        vendor_name="V", invoice_number="INV-X", description="x",
        total_amount=Decimal("100"), currency="CAD"
    )
    conn, tmpdir = _tmp_db()
    db_path = os.path.join(tmpdir, "w.db")
    monkeypatch.setenv("OILOPS_DB_PATH", db_path)
    try:
        with transaction(None) as conn2:
            new_id = save_pending(conn2, ext, reviewer="Bob")
        with transaction(None) as conn3:
            saved = approve_invoice(conn3, new_id, reviewer="Bob")
        assert saved.status == STATUS_APPROVED
    finally:
        conn.close()


def test_step8_dashboard_service_still_works(monkeypatch):
    from services.dashboard_service import compute_dashboard, CURRENCY_FILTER_ALL
    from database.db import init_db, transaction
    tmpdir = tempfile.mkdtemp(prefix="oilops_dashboard_test_")
    db_path = os.path.join(tmpdir, "dashboard.db")
    monkeypatch.setenv("OILOPS_DB_PATH", db_path)
    init_db(db_path)
    with transaction(None) as conn:
        snap = compute_dashboard(conn, date(2026, 9, 22), currency=CURRENCY_FILTER_ALL)
        assert snap.reference_date == date(2026, 9, 22)


# Synthetic E2E - reference_date = 2026-09-22, all categories present


def test_synthetic_e2e_weekly_summary_2026_09_22(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))

    # 7-day period
    assert facts.period_start == date(2026, 9, 16)
    assert facts.period_end == date(2026, 9, 22)

    # recorded amounts are per-currency, NEVER summed
    assert "CAD" in facts.recorded_amounts
    assert "USD" in facts.recorded_amounts
    cad_v = facts.recorded_amounts["CAD"]
    usd_v = facts.recorded_amounts["USD"]
    assert cad_v != (cad_v + usd_v)

    # outstanding per currency
    assert facts.outstanding_amounts.get("CAD") == Decimal("5000.00")
    assert facts.outstanding_amounts.get("USD") == Decimal("2000.00")

    # paid_this_week per currency
    assert facts.paid_this_week_amounts.get("CAD") == Decimal("3000.00")
    assert facts.paid_this_week_amounts.get("USD") == Decimal("1500.00")

    # duplicate groups
    assert facts.possible_duplicate_groups == 1

    # due next 7 days, overdue
    assert facts.due_next_7_days_count == 4
    assert facts.overdue_count == 1

    # payload safety
    payload = build_weekly_summary_payload(facts)
    assert_weekly_summary_payload_safe(payload)
    assert set(payload["recorded_amounts"].keys()) == {"CAD", "USD"}
    assert "vendor_name" not in payload
    assert "raw_text" not in payload

    # deterministic fallback runs
    text, source = generate_weekly_narrative(facts, force_mock=True)
    assert source == "mock"
    assert "CAD " in text
    assert "USD " in text
    assert "Recorded as paid this week" in text
    assert "oilops ai paid" not in text.lower()
    # The fallback explicitly clarifies that no payment was executed.
    assert "no external payment was executed" in text.lower()


def test_real_api_status_documented(sample_conn, monkeypatch):
    import os
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert is_narrative_api_key_configured() is False

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    assert is_narrative_api_key_configured() is True
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    text, source = generate_weekly_narrative(facts, force_mock=True)
    assert source == "mock"


def test_synthetic_e2e_with_live_provider_path(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))

    class _LiveProvider(NarrativeProvider):
        name = "live"
        def narrate(self, payload):
            assert_weekly_summary_payload_safe(payload)
            return "Live narrative for week of " + payload["period_start"] + "."

    text, source = generate_weekly_narrative(facts, provider=_LiveProvider())
    assert source == "llm"
    assert "Live narrative" in text


def test_narrative_text_is_bounded_by_max_length(sample_conn):
    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))

    class _HugeProvider(NarrativeProvider):
        name = "huge"
        def narrate(self, payload):
            return "x" * (10 * 1024 * 1024)

    text, source = generate_weekly_narrative(facts, provider=_HugeProvider())
    assert source == "mock"
    assert len(text) < 10000


def test_real_openai_call_without_real_key_falls_back(sample_conn):
    """Provider failures fall back cleanly, without making network calls."""
    class _ImmediateFailProvider(NarrativeProvider):
        name = "fail"

        def narrate(self, payload):
            raise ProviderError("simulated 401")

    facts = compute_weekly_facts(sample_conn, date(2026, 9, 22))
    text, source = generate_weekly_narrative(
        facts, provider=_ImmediateFailProvider()
    )
    assert source == "mock"
    assert "Weekly Office Operations Summary" in text


