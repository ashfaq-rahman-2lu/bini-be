from concurrent.futures import ThreadPoolExecutor
import io
from decimal import Decimal
import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.main import app
from app.models import Account, SourceLink, Statement, StatementRow, Transaction
from app.parser import ParseError, parse_pdf


def upload(client, data, password="test-only-password"):
    return client.post("/api/imports", files={"file": ("synthetic.pdf", data, "application/pdf")}, data={"password": password})


def confirm(client, draft):
    return client.post(f"/api/imports/{draft['id']}/confirm", json={"expected_revision": draft["revision"]})


def test_synthetic_parser_and_decimal_serialization(client, pdf_factory):
    draft = upload(client, pdf_factory()).json()
    assert len(draft["rows"]) == 2
    assert draft["can_confirm"]
    assert all(c["passed"] for c in draft["checks"])
    assert draft["rows"][1]["amount"] == "25.50"
    assert draft["rows"][1]["location"]["top"] > 0
    assert client.get("/api/transactions").json()["total"] == 0
    result = confirm(client, draft)
    assert result.status_code == 200, result.text
    ledger = client.get("/api/transactions").json()
    assert ledger["total"] == 2
    assert ledger["net"] == "74.50"
    assert ledger["items"][0]["posting_date"] == "2025-12-03"


def test_reupload_and_repeated_confirmation(client, pdf_factory):
    pdf = pdf_factory()
    draft = upload(client, pdf).json()
    assert upload(client, pdf).json()["id"] == draft["id"]
    assert confirm(client, draft).status_code == 200
    assert confirm(client, draft).status_code == 200
    # Already processed files do not need another password.
    assert upload(client, pdf, "").json()["status"] == "confirmed"
    assert client.get("/api/transactions").json()["total"] == 2
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(SourceLink)) == 2


def test_cancel_draft_and_protect_confirmed(client, pdf_factory):
    draft = upload(client, pdf_factory()).json()
    assert client.delete("/api/imports/" + draft["id"]).status_code == 204
    assert client.get("/api/imports/" + draft["id"]).status_code == 404
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(StatementRow)) == 0
    draft = upload(client, pdf_factory()).json()
    confirm(client, draft)
    assert client.delete("/api/imports/" + draft["id"]).status_code == 409
    assert client.patch(f"/api/imports/{draft['id']}/decisions", json={"decisions": []}).status_code == 409


def test_failed_reconciliation_is_visible_and_blocks_commit(client, pdf_factory):
    draft = upload(client, pdf_factory(closing_delta="0.01")).json()
    assert not draft["can_confirm"]
    assert not next(c for c in draft["checks"] if c["code"] == "closing")["passed"]
    assert confirm(client, draft).status_code == 409
    assert client.get("/api/transactions").json()["total"] == 0


def test_password_and_invalid_input_do_not_persist(client, pdf_factory):
    assert upload(client, pdf_factory(), "incorrect").status_code == 422
    assert upload(client, b"not a PDF").status_code == 422
    assert upload(client, b"%PDF-" + b"x" * (10*1024*1024)).status_code == 413
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Statement)) == 0
        assert db.scalar(select(func.count()).select_from(Account)) == 0


def test_timeout_does_not_persist(client, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("parser", 60)
    monkeypatch.setattr("app.main.subprocess.run", timeout)
    assert upload(client, b"%PDF-1.7\n").status_code == 408
    assert client.get("/api/transactions").json()["total"] == 0


def test_page_limit_and_scanned_or_unsupported_documents(client):
    from reportlab.pdfgen.canvas import Canvas
    buffer = io.BytesIO()
    canvas = Canvas(buffer)
    for _ in range(51):
        canvas.drawString(30, 700, 'Synthetic test page')
        canvas.showPage()
    canvas.save()
    response = upload(client, buffer.getvalue(), '')
    assert response.status_code == 422 and '50 pages' in response.json()['detail']
    buffer = io.BytesIO(); canvas = Canvas(buffer); canvas.showPage(); canvas.save()
    assert upload(client, buffer.getvalue(), '').status_code == 422
    assert client.get('/api/transactions').json()['total'] == 0


def test_printed_count_and_out_of_period_failures(client, pdf_factory):
    draft = upload(client, pdf_factory(debit_count_delta=1)).json()
    assert not draft['can_confirm']
    assert not next(c for c in draft['checks'] if c['code'] == 'debit_count')['passed']
    assert confirm(client, draft).status_code == 409
    draft = upload(client, pdf_factory([('01-Jan-2026','credit','50.00','Out of period','REF-C')])).json()
    assert not next(c for c in draft['checks'] if c['code'] == 'dates')['passed']
    assert confirm(client, draft).status_code == 409


def test_source_details_and_database_persistence_across_clients(client, pdf_factory):
    first = upload(client, pdf_factory()).json(); confirm(client, first)
    second = upload(client, pdf_factory(memo='Overlapping copy')).json(); confirm(client, second)
    with TestClient(app, headers=client.headers, cookies=client.cookies) as restarted:
        items = restarted.get('/api/transactions').json()['items']
        assert len(items) == 2
        details = restarted.get('/api/transactions/' + items[0]['id']).json()
        assert isinstance(details['amount'], str) and isinstance(details['balance'], str)
        assert len(details['sources']) == 2
        assert all(source['page'] == 1 and source['raw_text'] for source in details['sources'])


def test_similar_payments_require_a_decision(client, pdf_factory):
    first = upload(client, pdf_factory()).json(); confirm(client, first)
    changed = [("02-Dec-2025", "credit", "100.00", "A different deposit", "REF-C"), ("03-Dec-2025", "debit", "25.50", "Coffee purchase", "REF-B")]
    second = upload(client, pdf_factory(changed)).json()
    assert second["summary"] == {"new": 0, "duplicate": 1, "unresolved": 1}
    assert confirm(client, second).status_code == 409
    row = second["rows"][0]
    result = client.patch(f"/api/imports/{second['id']}/decisions", json={"decisions": [{"row_id": row["id"], "decision": "new"}]})
    assert result.status_code == 200
    assert confirm(client, result.json()).status_code == 200
    assert client.get("/api/transactions").json()["total"] == 3


def test_repeated_legitimate_occurrences_and_one_to_one_matches(client, pdf_factory):
    entries = [("03-Dec-2025", "debit", "10.00", "Coffee", ""), ("03-Dec-2025", "credit", "10.00", "Refund", ""), ("03-Dec-2025", "debit", "10.00", "Coffee", "")]
    first = upload(client, pdf_factory(entries)).json(); confirm(client, first)
    assert client.get("/api/transactions").json()["total"] == 3
    second = upload(client, pdf_factory(entries, memo="Second statement occurrence")).json()
    assert second["summary"]["unresolved"] == 2
    debit_rows = [r for r in second["rows"] if r["direction"] == "debit"]
    same_target = debit_rows[0]["candidates"][0]["id"]
    decisions = [{"row_id": r["id"], "decision": "match", "transaction_id": same_target} for r in debit_rows]
    updated = client.patch(f"/api/imports/{second['id']}/decisions", json={"decisions": decisions}).json()
    assert updated["summary"]["unresolved"] == 2
    assert confirm(client, updated).status_code == 409


def test_accounts_are_never_cross_matched(client, pdf_factory):
    first = upload(client, pdf_factory()).json(); confirm(client, first)
    second = upload(client, pdf_factory(account="200000000002")).json()
    assert second["summary"]["new"] == 2
    assert confirm(client, second).status_code == 200
    assert len(client.get("/api/accounts").json()) == 2
    assert client.get("/api/transactions").json()["total"] == 4
    assert client.get("/api/transactions", params={"account_id": second["account"]["id"]}).json()["total"] == 2


def test_filter_pagination_and_total_consistency(client, pdf_factory):
    draft = upload(client, pdf_factory()).json(); confirm(client, draft)
    ledger = client.get("/api/transactions", params={"page_size": 1, "sort": "amount_asc"}).json()
    assert ledger["total"] == 2 and len(ledger["items"]) == 1
    assert ledger["credit_total"] == "100.00" and ledger["debit_total"] == "25.50"
    result = client.get("/api/transactions", params={"search": "COFFEE", "from_date": "2025-12-03", "to_date": "2025-12-03", "direction": "debit"}).json()
    assert result["total"] == 1 and result["net"] == "-25.50"
    assert client.get("/api/transactions", params={"search": "%"}).json()["total"] == 0
    assert client.get("/api/transactions", params={"search": "_"}).json()["total"] == 0
    assert client.get("/api/transactions", params={"from_date": "2025-12-04", "to_date": "2025-12-03"}).status_code == 422
    assert client.get("/api/transactions", params={"page": 0}).status_code == 422


def test_stale_review_after_decision_and_other_import(client, pdf_factory):
    first = upload(client, pdf_factory()).json()
    client.patch(f"/api/imports/{first['id']}/decisions", json={"decisions": [{"row_id": first["rows"][0]["id"], "decision": "new"}]})
    assert confirm(client, first).status_code == 409
    refreshed = client.get("/api/imports/" + first["id"]).json()
    other = upload(client, pdf_factory(memo="overlapping copy")).json(); confirm(client, other)
    assert confirm(client, refreshed).status_code == 409
    refreshed = client.get("/api/imports/" + first["id"]).json()
    # Explicit keep-separate decision is retained for user review.
    assert refreshed["rows"][0]["decision"] == "new"


def test_concurrent_confirmations_are_atomic(client, pdf_factory):
    first = upload(client, pdf_factory()).json()
    second = upload(client, pdf_factory(memo="concurrent overlap")).json()
    def send(draft):
        with TestClient(app, headers=client.headers, cookies=client.cookies) as c:
            return confirm(c, draft).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(send, [first, second]))
    assert sorted(statuses) == [200, 409]
    for draft in [first, second]:
        fresh = client.get('/api/imports/' + draft['id']).json()
        assert confirm(client, fresh).status_code == 200
    assert client.get('/api/transactions').json()['total'] == 2
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(SourceLink)) == 4


def test_concurrent_identical_uploads_reuse_draft(client, pdf_factory):
    pdf = pdf_factory()
    def send(_):
        with TestClient(app, headers=client.headers, cookies=client.cookies) as c:
            response = upload(c, pdf)
            assert response.status_code == 200, response.text
            return response.json()['id']
    with ThreadPoolExecutor(max_workers=2) as executor:
        ids = list(executor.map(send, range(2)))
    assert len(set(ids)) == 1


def test_cross_account_manual_match_rejected(client, pdf_factory):
    first = upload(client, pdf_factory()).json(); confirm(client, first)
    transaction = client.get('/api/transactions').json()['items'][0]
    second = upload(client, pdf_factory(account='333333333333')).json()
    response = client.patch(f"/api/imports/{second['id']}/decisions", json={'decisions': [{'row_id':second['rows'][0]['id'], 'decision':'match', 'transaction_id':transaction['id']}]})
    assert response.status_code == 422


def test_no_password_columns_or_decrypted_pdf_copies(client, pdf_factory):
    data = pdf_factory()
    draft = upload(client, data).json()
    with SessionLocal() as db:
        statement = db.get(Statement, draft['id'])
        assert statement.original_pdf == data
        for table in [Statement.__table__, StatementRow.__table__, Account.__table__, Transaction.__table__]:
            assert not any('password' in column.name for column in table.columns)


@pytest.mark.parametrize('reverse', [False, True])
def test_private_samples_in_both_orders(client, samples, reverse):
    root, password = samples
    names = ['acc_statement_02_Nov_2026.pdf', 'acc_statement_02_Nov_2026_2.pdf']
    if reverse: names.reverse()
    counts = {'acc_statement_02_Nov_2026.pdf':9, 'acc_statement_02_Nov_2026_2.pdf':11}
    for index, name in enumerate(names):
        response = upload(client, (root/name).read_bytes(), password)
        assert response.status_code == 200, response.text
        draft = response.json()
        assert len(draft['rows']) == counts[name]
        assert all(c['passed'] for c in draft['checks'])
        assert draft['summary']['duplicate'] == (5 if index else 0)
        assert confirm(client, draft).status_code == 200
    ledger = client.get('/api/transactions').json()
    assert ledger['total'] == 15
    assert Decimal(ledger['credit_total']) - Decimal(ledger['debit_total']) == Decimal(ledger['net'])
    for name in names:
        assert upload(client, (root/name).read_bytes(), '').json()['status'] == 'confirmed'
    assert client.get('/api/transactions').json()['total'] == 15
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(SourceLink)) == 20
