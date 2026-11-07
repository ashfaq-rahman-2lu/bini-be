"""Physical EBL column alignment must survive variable-width PDF text extraction."""
import io
import os
from pathlib import Path

import pytest
from reportlab.pdfgen.canvas import Canvas

from app.parser import ParseError, parse_pdf
from app.pdf_tools import unlock_pdf


def proportional_statement(*, debit="1000.00", credit="", split_description=False, footer_web="Website:", continuation_pages=1):
    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=(595, 842))
    canvas.setFont("Helvetica", 7)
    def line(y, text, x=22): canvas.drawString(x, y, text)
    def headers():
        for x, text in [(22, "TRN. DATE"), (81, "DESCRIPTION"), (259, "REFERENCE"), (386, "DEBITS"), (458, "CREDITS"), (537, "BALANCE")]:
            line(620, text, x)
    line(790, "Eastern Bank Limited EBL")
    line(775, "Account No : 100000000001")
    line(760, "Period From : 01-Dec-2025 - 31-Dec-2025")
    line(745, "Currency Name : BANGLADESH TAKA")
    headers()
    line(600, "01-Dec-2025"); line(600, "Opening Balance", 81)
    canvas.drawRightString(486, 600, "3,000.00"); canvas.drawRightString(570, 600, "3,000.00")
    line(575, "02-Dec-2025"); line(575, "Debit transfer", 81)
    if debit: canvas.drawRightString(410, 575, debit)
    if credit: canvas.drawRightString(486, 575, credit)
    canvas.drawRightString(570, 575, "2,000.00")
    if not split_description:
        line(565, "Wrapped description //REFA", 81)
        line(565, "e-statement", 217)
    def footer():
        line(30, "This document is verifiable by scanning the above QR code")
        line(20, "IP: synthetic Email: synthetic Contact Center: synthetic " + footer_web + " synthetic Swift: TEST")
        # Neither a footer fragment nor anything below it belongs to a transaction.
        line(10, "trailing-footer-fragment", 81)
    footer()
    for index in range(continuation_pages if split_description else 1):
        canvas.showPage()
        canvas.setFont("Helvetica", 7)
        line(790, "Eastern Bank Limited EBL")
        line(760, "Repeated account heading - exclude", 81)
        headers()
        if split_description:
            line(600, f"Continued merchant segment {index + 1}", 79.35)
            line(590, "e-statement", 217)
            if index + 1 == continuation_pages:
                line(580, "//REFA", 79.35)
        if index + 1 < continuation_pages:
            footer()
    credit_y = 550 if split_description else 600
    line(credit_y, "03-Dec-2025"); line(credit_y, "Credit transfer", 81)
    canvas.drawRightString(486, credit_y, "2,000.00"); canvas.drawRightString(570, credit_y, "4,000.00")
    # Some EBL labels have no word spacing in the underlying PDF.
    line(credit_y - 30, "STATEMENTCLOSINGBALANCE", 79)
    canvas.drawRightString(570, credit_y - 30, "4,000.00")
    line(credit_y - 50, "DEBITS 1000.00 #DRCOUNT: 1", 79)
    line(credit_y - 65, "CREDITS 2000.00 #CRCOUNT: 1", 79)
    line(credit_y - 90, "UNSETTLED DEBIT CARD TRANSACTION")
    line(credit_y - 110, "04-Dec-2025"); line(credit_y - 110, "Excluded unsettled payment", 81)
    canvas.drawRightString(410, credit_y - 110, "999.00")
    canvas.save()
    return buffer.getvalue()


def test_proportional_fonts_right_aligned_amounts_wrapping_and_page_footers():
    result = parse_pdf(proportional_statement())
    assert len(result["rows"]) == 2
    debit, credit = result["rows"]
    assert (debit["direction"], debit["amount"], debit["balance"]) == ("debit", "1000.00", "2000.00")
    assert (credit["direction"], credit["amount"], credit["balance"]) == ("credit", "2000.00", "4000.00")
    assert debit["description"] == "Debit transfer Wrapped description //REFA"
    assert debit["reference"] == "REFA"
    assert debit["page"] == 1 and credit["page"] == 2
    assert "verifiable" not in debit["raw_text"]
    assert all(check["passed"] for check in result["checks"])
    assert parse_pdf(unlock_pdf(proportional_statement()))["rows"] == result["rows"]


@pytest.mark.parametrize("debit,credit", [("1000.00", "1000.00"), ("", ""), ("-1000.00", "")])
def test_ambiguous_or_invalid_amount_cells_remain_blocked(debit, credit):
    with pytest.raises(ParseError, match="exactly one positive debit or credit"):
        parse_pdf(proportional_statement(debit=debit, credit=credit))


@pytest.mark.parametrize("footer_web", ["Web:", "Website:"])
@pytest.mark.parametrize("continuation_pages", [1, 2])
def test_description_continues_across_pages_without_footer_or_header_text(footer_web, continuation_pages):
    result = parse_pdf(proportional_statement(split_description=True, footer_web=footer_web,
                                              continuation_pages=continuation_pages))
    assert len(result["rows"]) == 2
    row = result["rows"][0]
    expected = "Debit transfer " + " ".join(f"Continued merchant segment {i + 1}" for i in range(continuation_pages)) + " //REFA"
    assert row["description"] == expected
    assert row["normalized_description"] == expected.casefold()
    assert row["reference"] == "REFA"
    assert row["page"] == 1
    assert row["ordinal"] == 1 and result["rows"][1]["ordinal"] == 2
    for text in ["IP:", "Email:", "Contact Center:", "Swift:", "footer", "Repeated account", "TRN. DATE"]:
        assert text not in row["raw_text"]
        assert text not in row["description"]
    assert all(check["passed"] for check in result["checks"])


@pytest.mark.parametrize("filename,count", [("01_unlocked.pdf", 74), ("02_unlocked.pdf", 71), ("03_unlocked.pdf", 60), ("04_unlocked.pdf", 60)])
def test_private_unlocked_statement_imports(client, filename, count):
    root = Path(os.environ.get("SAMPLE_DIR", Path(__file__).resolve().parents[2] / "sample"))
    path = root / filename
    if not os.environ.get("RUN_PRIVATE_SAMPLES") or not path.is_file():
        pytest.skip("Optional private unlocked statement fixture is unavailable.")
    response = client.post("/api/imports", files={"file": (filename, path.read_bytes(), "application/pdf")}, data={"password": ""})
    assert response.status_code == 200
    result = response.json()
    assert len(result["rows"]) == count
    assert all(check["passed"] for check in result["checks"])
    assert result["can_confirm"]
