import io
import subprocess

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.pdfencrypt import StandardEncryption
from sqlalchemy import func, select

from app.db import SessionLocal, get_db
from app.main import app
from app.models import Account, Statement


def unlock(client, data, password="test-only-password", filename="statement.pdf"):
    return client.post("/api/pdfs/unlock", files={"file": (filename, data, "application/pdf")}, data={"password": password})


@pytest.mark.parametrize("encrypted", [True, False])
def test_unlock_preserves_pages_metadata_outlines_and_does_not_import(client, pdf_factory, encrypted):
    original = PdfReader(io.BytesIO(pdf_factory(password="")))
    writer = PdfWriter(clone_from=original)
    writer.add_metadata({"/Title": "Synthetic statement"})
    writer.add_outline_item("Posted transactions", 0)
    if encrypted:
        writer.encrypt("test-only-password")
    buffer = io.BytesIO(); writer.write(buffer)
    response = unlock(client, buffer.getvalue(), "test-only-password" if encrypted else "")
    assert response.status_code == 200, response.text
    result = PdfReader(io.BytesIO(response.content))
    assert not result.is_encrypted
    assert len(result.pages) == len(original.pages)
    assert result.pages[0].extract_text() == original.pages[0].extract_text()
    assert result.metadata.title == "Synthetic statement"
    assert result.outline[0].title == "Posted transactions"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    assert "statement_unlocked.pdf" in response.headers["content-disposition"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Statement)) == 0
        assert db.scalar(select(func.count()).select_from(Account)) == 0


def test_unlock_retains_interactive_fields_for_non_bank_pdf(client):
    buffer = io.BytesIO()
    canvas = Canvas(buffer, encrypt=StandardEncryption("test-only-password"))
    canvas.drawString(50, 750, "Synthetic form")
    canvas.acroForm.textfield(name="example", value="Kept value", x=50, y=700)
    canvas.showPage(); canvas.save()
    response = unlock(client, buffer.getvalue())
    assert response.status_code == 200
    result = PdfReader(io.BytesIO(response.content))
    assert not result.is_encrypted
    assert result.get_fields()["example"]["/V"] == "Kept value"
    assert len(result.pages[0]["/Annots"]) == 1


def test_unlock_errors_and_safe_attachment_filename(client, pdf_factory, caplog):
    assert unlock(client, pdf_factory(), "incorrect-secret").json()["detail"] == "Incorrect PDF password."
    assert "incorrect-secret" not in caplog.text
    assert unlock(client, b"not a pdf").status_code == 422
    assert unlock(client, b"%PDF-" + b"x" * (10 * 1024 * 1024)).status_code == 413
    response = unlock(client, pdf_factory(), filename='../../statement\r\nInjected: x.pdf')
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert "../" not in disposition


def test_unlock_page_limit_and_timeout(client, monkeypatch):
    writer = PdfWriter()
    for _ in range(51): writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO(); writer.write(buffer)
    response = unlock(client, buffer.getvalue(), "")
    assert response.status_code == 422 and "50 pages" in response.json()["detail"]
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("parser", 60)
    monkeypatch.setattr("app.main.subprocess.run", timeout)
    assert unlock(client, b"%PDF-1.7\n").status_code == 408


def test_unlock_does_not_require_a_database_connection(client, pdf_factory):
    def unavailable():
        raise AssertionError("The PDF utility must not access the ledger database")
    app.dependency_overrides[get_db] = unavailable
    try:
        assert unlock(client, pdf_factory()).status_code == 200
    finally:
        app.dependency_overrides.pop(get_db)


def test_recent_imports_recover_drafts_and_reflect_confirm_cancel(client, pdf_factory):
    ids = []
    for index in range(2):
        data = pdf_factory(memo=f"Selection {index}")
        draft = client.post("/api/imports", files={"file": (f"statement-{index}.pdf", data)}, data={"password": "test-only-password"}).json()
        ids.append(draft["id"])
    listed = client.get("/api/imports").json()
    assert {item["id"] for item in listed} == set(ids)
    assert all(set(item) == {"id", "filename", "status", "created_at"} for item in listed)
    assert all(item["status"] == "draft" for item in listed)
    assert len(client.get("/api/imports", params={"limit": 1}).json()) == 1
    assert client.get("/api/imports", params={"limit": 101}).status_code == 422
    draft = client.get("/api/imports/" + ids[0]).json()
    assert client.post(f"/api/imports/{ids[0]}/confirm", json={"expected_revision": draft["revision"]}).status_code == 200
    assert client.delete("/api/imports/" + ids[1]).status_code == 204
    listed = client.get("/api/imports").json()
    assert len(listed) == 1 and listed[0]["status"] == "confirmed"
