"""Batch Invoice Intake - per-file + batch processing engine (Batch 2).

Orchestrates the SAME per-file pipeline that Batch 1's Process
Invoice page runs (parse -> extract -> privacy -> validate ->
AI classify -> save pending -> store PDF), but for a LIST of files
and with hard guarantees:

  * per-file failure isolation (one bad file does not stop the rest)
  * a deterministic Queue status per item
  * original PDF bytes NEVER reach the LLM (only the AI-safe payload)
  * document storage still writes under data/documents/ the same way
    Batch 1 does (deterministic filename, path-traversal defence)
  * Human Review is preserved: AI never approves, rejects, or marks
    paid. All saved rows are pending until a human clicks a button.

REUSED FROM BATCH 1 (NONE of those modules are modified):
  services.invoice_parser.parse_pdf
  services.invoice_extractor.extract_invoice
  services.privacy_filter.redact_text
  services.invoice_checker.check_invoice
  services.ai_classifier.classify_expense
  services.invoice_repository.save_pending, update_document_ref
  services.document_storage.save_pdf

FAILURE ISOLATION
-----------------
Every step for every file is wrapped in its own try/except. An
exception in one file (corrupt PDF, extractor crash, IO error to
the docs directory, etc.) marks ONLY that item "Failed" with a
human-readable error string. The batch loop continues.

DUPLICATE HANDLING
------------------
Duplicate detection still runs against the database. If the triplet
(vendor, invoice_number, total_amount) already exists, the new
invoice IS still saved - we never silently drop an upload. Its
queue status is "Possible Duplicate", and the human reviewer must
acknowledge before approval in Batch 1's review flow.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Callable, List, Optional, Sequence

from .invoice_parser import parse_pdf, InvoiceParseResult
from .invoice_extractor import extract_invoice, InvoiceExtraction
from .privacy_filter import redact_text, RedactionResult
from .invoice_checker import (
    check_invoice,
    InvoiceValidationResult,
    CODE_POSSIBLE_DUPLICATE,
)
from .ai_classifier import (
    classify_expense,
    ClassificationResult,
    SOURCE_MOCK,
)
from .invoice_repository import save_pending, update_document_ref
from .document_storage import save_pdf, StoredDocument
from .queue_status import (
    derive_queue_status,
    QUEUE_STATUS_READY,
    QUEUE_STATUS_NEEDS_REVIEW,
    QUEUE_STATUS_POSSIBLE_DUPLICATE,
    QUEUE_STATUS_FAILED,
)

logger = logging.getLogger("oilops.batch_intake")

BATCH_MAX_PDF_BYTES = 25 * 1024 * 1024


@dataclass
class BatchIntakeFile:
    """A single upload supplied by the UI."""
    filename: str
    raw_bytes: bytes


@dataclass
class BatchIntakeItem:
    """The result of running the per-file pipeline on ONE upload."""
    filename: str
    raw_size: int = 0
    parse_status: str = QUEUE_STATUS_FAILED
    parse_error: Optional[str] = None
    page_count: int = 0
    extraction: Optional[InvoiceExtraction] = None
    redaction: Optional[RedactionResult] = None
    validation: Optional[InvoiceValidationResult] = None
    classification: Optional[ClassificationResult] = None
    invoice_id: Optional[int] = None
    document: Optional[StoredDocument] = None
    queue_status: str = QUEUE_STATUS_FAILED
    error: Optional[str] = None
    is_possible_duplicate: bool = False
    duplicate_existing_ids: List[int] = field(default_factory=list)


def _safe_filename(name):
    if not name:
        return "(unnamed)"
    base = os.path.basename(name)
    return base or "(unnamed)"


def _extract_duplicate_ids(validation):
    if validation is None:
        return []
    out = []
    for issue in validation.issues:
        if issue.code != CODE_POSSIBLE_DUPLICATE:
            continue
        msg = issue.message or ""
        idx = msg.find("invoice id")
        if idx < 0:
            continue
        rest = msg[idx + len("invoice id"):]
        buf = []
        for ch in rest:
            if ch.isdigit():
                buf.append(ch)
            else:
                if buf:
                    try:
                        out.append(int("".join(buf)))
                    except ValueError:
                        pass
                    buf = []
        if buf:
            try:
                out.append(int("".join(buf)))
            except ValueError:
                pass
    return out
def _process_one(f, *, conn, docs_root, classify_fn, reference_date):
    """Run the full Batch 1 per-file pipeline for a single upload.

    NEVER raises. Every step is wrapped so that one bad file in a
    batch cannot poison the others.
    """
    item = BatchIntakeItem(
        filename=_safe_filename(f.filename),
        raw_size=len(f.raw_bytes or b""),
    )
    if not isinstance(f.raw_bytes, (bytes, bytearray)):
        item.parse_error = "Uploaded payload is not bytes."
        item.error = item.parse_error
        item.queue_status = QUEUE_STATUS_FAILED
        return item
    if len(f.raw_bytes) == 0:
        item.parse_error = "Uploaded file is empty."
        item.error = item.parse_error
        item.parse_status = "empty"
        item.queue_status = QUEUE_STATUS_FAILED
        return item
    if len(f.raw_bytes) > BATCH_MAX_PDF_BYTES:
        item.parse_error = "PDF exceeds per-file size cap."
        item.error = item.parse_error
        item.parse_status = "failed"
        item.queue_status = QUEUE_STATUS_FAILED
        return item
    try:
        parsed = parse_pdf(bytes(f.raw_bytes), filename=item.filename)
    except Exception as exc:
        item.parse_status = "failed"
        item.parse_error = "Parse exception: " + str(exc)
        item.error = item.parse_error
        item.queue_status = QUEUE_STATUS_FAILED
        return item
    item.parse_status = parsed.parsing_status
    item.parse_error = parsed.error
    item.page_count = parsed.page_count
    if parsed.parsing_status != "ok":
        item.queue_status = QUEUE_STATUS_FAILED
        item.error = parsed.error or "PDF could not be parsed."
        return item
    try:
        extraction = extract_invoice(parsed.raw_text)
    except Exception as exc:
        item.error = "Extraction exception: " + str(exc)
        item.queue_status = QUEUE_STATUS_FAILED
        return item
    item.extraction = extraction
    try:
        item.redaction = redact_text(parsed.raw_text)
    except Exception as exc:
        item.redaction = None
        logger.warning("redact_text failed for %s: %s", item.filename, exc)
    try:
        item.validation = check_invoice(
            extraction, db_conn=conn, reference_date=reference_date,
        )
    except Exception as exc:
        item.error = "Validation exception: " + str(exc)
        item.queue_status = QUEUE_STATUS_FAILED
        return item
    item.is_possible_duplicate = any(
        i.code == CODE_POSSIBLE_DUPLICATE for i in item.validation.issues
    )
    item.duplicate_existing_ids = _extract_duplicate_ids(item.validation)
    try:
        cls = classify_fn or classify_expense
        item.classification = cls(extraction)
    except Exception as exc:
        logger.warning("classify_expense failed for %s: %s", item.filename, exc)
        item.classification = None
    try:
        item.invoice_id = save_pending(
            conn, extraction,
            source_filename=item.filename,
            expense_category=(
                item.classification.category
                if item.classification is not None else None
            ),
            classification_source=(
                item.classification.source
                if item.classification is not None else SOURCE_MOCK
            ),
            classification=item.classification,
            validation=item.validation,
        )
    except Exception as exc:
        item.error = "Save pending failed: " + str(exc)
        item.queue_status = QUEUE_STATUS_FAILED
        return item
    try:
        item.document = save_pdf(
            bytes(f.raw_bytes),
            invoice_id=int(item.invoice_id),
            original_filename=item.filename,
        )
        update_document_ref(
            conn, int(item.invoice_id),
            document_path=item.document.document_path,
            document_sha256=item.document.document_sha256,
            document_size_bytes=item.document.document_size_bytes,
        )
    except Exception as exc:
        logger.warning("save_pdf failed for %s: %s", item.filename, exc)
        item.document = None
        item.error = "Document storage failed: " + str(exc)
    item.queue_status = derive_queue_status(
        parse_status=item.parse_status, validation=item.validation,
    )
    return item
def process_pdf_batch(
    files, *, conn, docs_root=None, classify_fn=None, reference_date=None,
):
    """Run the per-file pipeline on every entry in files.

    Returns list of BatchIntakeItem, one per input file, in the same order.
    No exceptions bubble out. Items carry their own queue_status and error.
    The caller is responsible for conn.commit()/rollback().
    """
    if docs_root is not None:
        os.environ["OILOPS_DOCS_DIR"] = str(docs_root)
    out = []
    for f in files:
        item = _process_one(
            f, conn=conn, docs_root=docs_root,
            classify_fn=classify_fn, reference_date=reference_date,
        )
        out.append(item)
    return out


def queue_row_from_item(item):
    """Project a BatchIntakeItem into the flat dict the Queue UI renders.

    Contains ONLY business metadata. Deliberately omits raw_text,
    redacted_text, and any sensitive identifiers.
    """
    ext = item.extraction
    cls = item.classification
    val = item.validation
    def _money(field_name):
        v = getattr(ext, field_name, None) if ext is not None else None
        if v is None:
            return None
        try:
            return str(v)
        except Exception:
            return None
    return {
        "filename": item.filename,
        "vendor": (ext.vendor_name if ext is not None else None) or "?",
        "invoice_number": (ext.invoice_number if ext is not None else None) or "?",
        "total": _money("total_amount"),
        "currency": (ext.currency if ext is not None else None) or "?",
        "classification": (cls.category if cls is not None else "(none)"),
        "classification_source": (cls.source if cls is not None else "(none)"),
        "validation": (val.as_dict() if val is not None else {
            "issue_count": 0, "error_count": 0,
            "warning_count": 0, "info_count": 0, "issues": [],
        }),
        "status": item.queue_status,
        "invoice_id": item.invoice_id,
        "document_path": (
            item.document.document_path if item.document is not None else None
        ),
        "error": item.error,
        "is_possible_duplicate": item.is_possible_duplicate,
        "duplicate_existing_ids": list(item.duplicate_existing_ids),
    }


__all__ = [
    "BATCH_MAX_PDF_BYTES",
    "BatchIntakeFile",
    "BatchIntakeItem",
    "process_pdf_batch",
    "queue_row_from_item",
]
