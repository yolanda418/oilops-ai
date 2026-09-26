# OilOps AI - Demo Walk-through

A scripted 2-minute demo for a non-technical audience (e.g. a small
operator's manager / owner). Designed to be run live in front of the
app at `http://localhost:8501`. **All data is synthetic.**

> **Headline:** AI handles repetitive office work; humans keep control.

## Before the demo

- `pip install -r requirements.txt` once.
- `streamlit run app.py` is already running; the browser tab is open.
- Have these two PDFs ready:
  - `sample_data/synthetic_invoices/sample_synthetic_invoice.pdf`
    (clean invoice, total ~ CAD 1,265.00).
  - `sample_data/synthetic_invoices/sample_privacy_invoice.pdf`
    (same vendor, includes a bank-account / routing block in the
    footer so the privacy redaction is visible).
- Optional: have a second sample ready that is the **same** vendor,
  invoice number, and amount as the first one, to demonstrate
  duplicate detection. The E2E fixtures generator in
  `tests/fixtures/make_e2e_invoices.py` produces these.

---

## 2-minute script (with timing)

### 0:00 - 0:15  Setup and headline

Say:

> "This is OilOps AI. It automates the boring parts of the back-office -
> PDFs, dates, totals, expense categories - and it never moves a cent
> without a human clicking the button. Everything you're about to see
> runs on synthetic data."

### 0:15 - 0:30  Step 1 - Upload

- Upload `sample_privacy_invoice.pdf` (the one with bank details).
- The UI shows the file is read locally. No upload to any third party.

### 0:30 - 0:50  Steps 2 & 3 - Parse + extract

- The UI populates vendor, invoice number, dates, subtotal, GST,
  total, currency.
- Scroll to the **Privacy filter** section. Point out:
  - The bank account, routing, SWIFT, email, and phone in the footer
    have been replaced with typed placeholders like
    `[REDACTED:BANK_ACCOUNT]`.
  - "This is the only representation that ever leaves this machine
    if you choose to use the AI features. No toggle to turn it off."

### 0:50 - 1:05  Step 4 - Validation

- Point at the validation flags: missing PO, possible duplicate,
  due-soon, etc.
- "The system flags them; a human decides what to do."

### 1:05 - 1:20  Step 5 - AI classification

- The UI shows the AI-suggested `expense_category`.
- "This is the only thing the AI is allowed to do in the workflow:
> suggest an expense category. It does not approve. It does not pay."
- Note: with no `OPENAI_API_KEY`, the deterministic mock kicks in
  and the demo still works exactly the same way.

### 1:20 - 1:40  Step 6 - Human review

- In the **Pending Review** section, click **Approve**, type a quick
  reviewer note, save.
- "Approval is a local SQLite write. Nothing leaves this machine."

### 1:40 - 1:50  Step 7 - Mark Paid

- Now that the invoice is approved, **Mark Paid** is enabled.
- Click it.
- "This is a tracking flag, not a payment. The operator still uses
> their bank or accounting system to actually pay the vendor."

### 1:50 - 2:00  Steps 8 & 9 - Dashboard + Weekly summary

- Scroll to the dashboard. Show: invoices recorded, pending review,
  outstanding totals (per currency - CAD and USD are **never**
  combined), possible duplicate groups, approved spend by category.
- "Numbers are computed by deterministic SQLite / Python aggregates.
> The AI is not doing arithmetic."

---

## Talking points (if asked)

- **Is the AI calling my real bank?** No. There is no bank
  integration. There is no ACH, wire, or payment-API hook.
- **What does the AI actually see?** Only the allow-listed fields
  in the redacted payload: vendor, dates, amounts, currency,
  description, PO number. Never bank account, routing, contact info,
  or the raw invoice text.
- **Can I disable the AI entirely?** Yes. Without an `OPENAI_API_KEY`,
  the deterministic mock classifier and a deterministic weekly
  summary are used. The full workflow still runs.
- **Is this production-ready?** No - it is a portfolio / prototype
  for a single operator or a small office. Multi-user, audit log,
  ERP integration, and OCR are listed as future work and are not
  in this repository.

---

## After the demo

- The SQLite file `database/oilops.db` now contains the invoice you
  uploaded. Delete it (or run `python -m database.db` to re-init)
  before the next demo to start clean.
- The `tests/` folder covers the entire pipeline. Run
  `python -m pytest -q`; **478 tests were collected** for this portfolio review
  (collection count, not a test run in this review).
