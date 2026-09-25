"""STEP 7 - Streamlit session_state helpers.

These helpers exist so the UI code in ``app.py`` does not silently
re-classify or re-persist data when the page reruns for unrelated
reasons (a checkbox toggle, a selectbox change, expanding a panel,
typing in an unrelated text input, etc.).

They are deliberately split out of ``app.py`` so they can be unit
tested without importing the full Streamlit runtime.

Design notes
------------
* ``compute_classifier_fingerprint`` is a stable, deterministic hash
  of ONLY the fields that are sent to the live LLM. If any of them
  changes, the previous classification is considered stale.
* ``reset_review_session`` clears every per-upload artefact from
  ``session_state`` so the next uploaded PDF cannot inherit the
  classification, approval state, or payment state of the previous
  one.

No external network calls, no I/O, no side effects on the database.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Mapping, Optional


# Fields that, if changed, must invalidate the cached classification.
# Keep this list aligned with
# services.ai_classifier._CLA_PAYLOAD_ALLOWED_FIELDS.
_FINGERPRINT_FIELDS: tuple = (
    "vendor_name",
    "description",
    "total_amount",
    "currency",
)


# Every session_state key that must be wiped when the user uploads a
# new PDF (or otherwise clears the current invoice).
_REVIEW_SESSION_KEYS: tuple = (
    # Parsing / extraction (STEP 2-3)
    "parsed_invoice",
    "extraction",
    "reviewed_extraction",
    # STEP 6 - AI classification
    "classification_result",
    "classification_fingerprint",
    "classification_source_label",
    # STEP 7 - human review & persistence
    "reviewed_category",
    "reviewed_category_suggested",
    "reviewer_name",
    "review_note",
    "current_invoice_id",
    "current_invoice_status",
    "current_invoice_payment_state",
    "ack_duplicate",
    "duplicate_acknowledged_fingerprint",
    # Approval action guards
    "approve_clicked_at",
    "reject_clicked_at",
    "mark_paid_clicked_at",
    "last_save_message",
)


def _normalise_field(value: Any) -> str:
    """Render a single field as a stable string for fingerprinting.

    The fingerprint must be insensitive to:
        * case differences in strings
        * ``None`` vs empty string
        * ``Decimal("5250.00")`` vs ``"5250"`` vs ``"5250.0"``

    We deliberately do NOT serialise the value with str() alone
    because Decimals can have trailing-zero differences across
    code paths. Using ``strip().lower()`` keeps the fingerprint
    collision-free for the actual data we feed into the AI
    classifier.
    """
    if value is None:
        return ""
    if hasattr(value, "as_dict"):
        try:
            value = value.as_dict()
        except Exception:
            value = str(value)
    if isinstance(value, Mapping):
        parts = []
        for k in sorted(value.keys()):
            parts.append(str(k) + "=" + _normalise_field(value[k]))
        return "{" + "|".join(parts) + "}"
    s = str(value).strip()
    return s.lower()


def compute_classifier_fingerprint(extraction: Any) -> str:
    """Return a stable SHA-256 fingerprint of the classifier input."""
    raw: Dict[str, Any]
    if hasattr(extraction, "as_dict"):
        raw = extraction.as_dict()
    elif isinstance(extraction, Mapping):
        raw = dict(extraction)
    else:
        raw = {}

    parts = []
    for f in _FINGERPRINT_FIELDS:
        parts.append(f + "=" + _normalise_field(raw.get(f)))
    payload = "||".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fingerprint_matches(cached: Optional[str], extraction: Any) -> bool:
    """Return ``True`` iff ``cached`` equals the current fingerprint."""
    if not cached:
        return False
    return cached == compute_classifier_fingerprint(extraction)


def reset_review_session(session_state: Any) -> None:
    """Wipe every per-upload artefact from a Streamlit session_state."""
    for k in _REVIEW_SESSION_KEYS:
        try:
            if k in session_state:
                del session_state[k]
            else:
                try:
                    session_state.pop(k, None)
                except Exception:
                    pass
        except Exception:
            try:
                session_state.pop(k, None)
            except Exception:
                pass


def session_has_invoice(session_state: Any) -> bool:
    """Return True iff a reviewed invoice is currently in flight."""
    return bool(
        session_state.get("reviewed_extraction")
        or session_state.get("current_invoice_id")
    )

