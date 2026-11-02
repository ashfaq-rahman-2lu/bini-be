import io
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import landscape, A4
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.pdfencrypt import StandardEncryption

# Destructive test setup is restricted to a dedicated *_test database.
TEST_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://bini:bini_local_only@127.0.0.1:55433/bini_test")
from sqlalchemy.engine import make_url
if not (make_url(TEST_URL).database or "").endswith("_test"):
    raise RuntimeError("Tests require a separate database whose name ends with _test.")
os.environ["DATABASE_URL"] = TEST_URL

from fastapi.testclient import TestClient
from sqlalchemy import text
from app.db import engine
from app.main import app


@pytest.fixture(autouse=True)
def empty_ledger():
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE source_links, statement_rows, transactions, statements, accounts CASCADE"))


@pytest.fixture
def client():
    with TestClient(app) as instance:
        yield instance


def synthetic_pdf(entries=None, *, account="100000000001", password="test-only-password", opening="1000.00", closing_delta="0", period_from="01-Dec-2025", memo="", debit_count_delta=0):
    entries = entries if entries is not None else [("02-Dec-2025", "credit", "100.00", "Test deposit", "REF-A"), ("03-Dec-2025", "debit", "25.50", "Coffee purchase", "REF-B")]
    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=landscape(A4), pageCompression=0,
                    encrypt=StandardEncryption(password, ownerPassword="test-owner", canPrint=1) if password else None)
    canvas.setFont("Courier", 7)
    lines = ["Eastern Bank Limited EBL", f"Account No : {account}", f"Period From : {period_from} - 31-Dec-2025", "Currency Name : BANGLADESH TAKA", memo,
             f"{'TRN. DATE':<18}{'DESCRIPTION':<64}{'REFERENCE':<23}{'DEBITS':<20}{'CREDITS':<20}BALANCE",
             f"{period_from:<18}{'Opening Balance':<127}{opening}"]
    balance = Decimal(opening)
    debit = credit = Decimal("0")
    debit_count = credit_count = 0
    for posting, direction, amount, description, reference in entries:
        amount = Decimal(amount)
        if direction == "debit":
            balance -= amount; debit += amount; debit_count += 1
        else:
            balance += amount; credit += amount; credit_count += 1
        lines.append(f"{posting:<18}{description:<64}{reference:<23}{str(amount) if direction == 'debit' else '':<20}{str(amount) if direction == 'credit' else '':<20}{balance:.2f}")
    lines += [f"STATEMENT CLOSING BALANCE {balance + Decimal(closing_delta):.2f}", f"DEBITS {debit:.2f} #DRCOUNT: {debit_count + debit_count_delta}", f"CREDITS {credit:.2f} #CRCOUNT: {credit_count}", "UNSETTLED DEBIT CARD TRANSACTION"]
    y = 555
    for line in lines:
        canvas.drawString(20, y, line)
        y -= 14
    canvas.save()
    return buffer.getvalue()


@pytest.fixture
def pdf_factory():
    return synthetic_pdf


@pytest.fixture
def samples():
    root = Path(os.environ.get("SAMPLE_DIR", Path(__file__).resolve().parents[2] / "sample")).expanduser().resolve()
    if not os.environ.get("RUN_PRIVATE_SAMPLES") or not (root / "password.txt").is_file():
        pytest.skip("Set RUN_PRIVATE_SAMPLES=1 and optionally SAMPLE_DIR to validate private local PDFs.")
    return root, (root / "password.txt").read_text().strip()
