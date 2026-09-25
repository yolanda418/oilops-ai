"""Generate a SYNTHETIC test invoice PDF that exercises the privacy filter.

This script writes 'sample_privacy_invoice.pdf' into the same directory.
Every value is fake. The goal is to give the Streamlit UI a fixture
that produces at least one detection per category in STEP 4.

Run from project root:
    python sample_data/synthetic_invoices/make_synthetic_privacy.py

Requires reportlab.
"""

from pathlib import Path


VENDOR = "Prairie Field Equipment Ltd. (SYNTHETIC TEST DATA)"
INVOICE_NUMBER = "PFE-2026-009"
PO_NUMBER = "PO-8821"
INVOICE_DATE = "2026-09-22"
DUE_DATE = "2026-10-22"
SUBTOTAL = "CAD 5,000.00"
GST = "CAD 250.00"
TOTAL = "CAD 5,250.00"
DESCRIPTION = "Pump equipment rental (SYNTHETIC)"

# All values below are SYNTHETIC. They must never be used against a real
# person / vendor / bank. They are designed only to exercise the
# redaction regexes in services/privacy_filter.py.
BANK_ACCOUNT = "123456789012"
ROUTING_NUMBER = "021000021"
SWIFT = "BOFMCAM2"
IBAN = "GB82WEST12345698765432"
EMAIL = "billing@example.com"
PHONE = "(403) 555-0123"


def main():
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except ImportError:
        print("reportlab is required (pip install reportlab).")
        raise

    out_path = Path(__file__).parent / "sample_privacy_invoice.pdf"
    c = canvas.Canvas(str(out_path), pagesize=letter)
    width, height = letter

    y_pos = height - 60
    c.setFont("Helvetica-Bold", 16)
    c.drawString(60, y_pos, "INVOICE (SYNTHETIC TEST DATA)")
    y_pos -= 30

    c.setFont("Helvetica", 11)
    lines = [
        VENDOR,
        "Invoice #: " + INVOICE_NUMBER,
        "PO #: " + PO_NUMBER,
        "Invoice Date: " + INVOICE_DATE,
        "Due Date: " + DUE_DATE,
        "Description: " + DESCRIPTION,
        "",
        "Subtotal: " + SUBTOTAL,
        "GST: " + GST,
        "Total: " + TOTAL,
        "",
        "Remit To:",
        "Bank Account: " + BANK_ACCOUNT,
        "Routing Number: " + ROUTING_NUMBER,
        "SWIFT: " + SWIFT,
        "IBAN: " + IBAN,
        "Email: " + EMAIL,
        "Phone: " + PHONE,
    ]
    for line in lines:
        c.drawString(60, y_pos, line)
        y_pos -= 18

    c.showPage()
    c.save()
    print("Wrote " + str(out_path))


if __name__ == "__main__":
    main()
