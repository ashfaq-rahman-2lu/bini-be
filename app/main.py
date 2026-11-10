import base64
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .request_logging import RequestTimingMiddleware, TimedJSONResponse
from .auth import require_auth, router as auth_router
from .db import get_db
from .matching import FIELDS, review, transaction_view
from .models import Account, SourceLink, Statement, StatementRow, Transaction
from .schemas import AccountView, Confirmation, Decisions, ImportSummary, ImportView, TransactionList
from .investments import router as investment_router, lock as investment_lock
from .reconciliation_api import router as reconciliation_router
from .jobs import lifespan, router as jobs_router
from .performance import phase

app = FastAPI(title="Bini · Bank statement ledger", version="0.1.0", lifespan=lifespan, default_response_class=TimedJSONResponse)
app.include_router(auth_router)
protected = APIRouter(dependencies=[Depends(require_auth)])
protected.include_router(investment_router)
protected.include_router(reconciliation_router)
protected.include_router(jobs_router)
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins,
                   allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Content-Type", "X-Bini-Request"], allow_credentials=True)
@app.middleware("http")
async def private_api_responses(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.add_middleware(RequestTimingMiddleware)


DB = Annotated[Session, Depends(get_db)]


@app.get("/api/health")
def health(db: DB):
    db.execute(select(1))
    return {"status": "ok"}


def statement_or_404(db, statement_id, lock=False):
    statement = db.get(Statement, statement_id)
    if statement is None:
        raise HTTPException(404, "Import not found.")
    if lock:
        # All modifying paths lock account then statement in the same order.
        with phase('row_lock_wait'):
            db.scalar(select(Account).where(Account.id == statement.account_id).with_for_update().execution_options(populate_existing=True))
            statement = db.scalar(select(Statement).where(Statement.id == statement_id).with_for_update().execution_options(populate_existing=True))
        if statement is None:
            raise HTTPException(404, "Import not found.")
    return statement


def process_pdf(data, password, operation="extract"):
    payload = json.dumps({"pdf": base64.b64encode(data).decode(), "password": password, "operation": operation})
    try:
        result = subprocess.run([sys.executable, "-m", "app.parser_worker"], input=payload,
            capture_output=True, text=True, timeout=60, cwd=Path(__file__).resolve().parents[1])
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "PDF processing exceeded 60 seconds. No transactions were saved.") from None
    if result.returncode:
        raise HTTPException(422, "PDF processing failed. No transactions were saved.")
    try:
        output = json.loads(result.stdout)
    except ValueError:
        raise HTTPException(422, "Unable to read the PDF processing result.") from None
    if "error" in output:
        raise HTTPException(422, output["error"])
    return output["result"]


@protected.post("/api/pdfs/unlock", response_class=Response)
def unlock(file: UploadFile = File(...), password: str = Form("")):
    data = file.file.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "PDF must be at most 10 MB.")
    output = base64.b64decode(process_pdf(data, password, operation="unlock"), validate=True)
    filename = Path((file.filename or "document.pdf").replace("\\", "/")).stem[:150] + "_unlocked.pdf"
    return Response(output, media_type="application/pdf", headers={
        "Content-Disposition": "attachment; filename=\"unlocked.pdf\"; filename*=UTF-8''" + quote(filename, safe=""),
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })


@protected.get("/api/imports", response_model=list[ImportSummary])
def list_imports(db: DB, limit: int = Query(50, ge=1, le=100)):
    return [dict(row._mapping) for row in db.execute(select(
        Statement.id, Statement.filename, Statement.status, Statement.created_at,
    ).order_by(Statement.created_at.desc(), Statement.id.desc()).limit(limit))]


@protected.post("/api/imports", response_model=ImportView)
def upload(db: DB, file: UploadFile = File(...), password: str = Form("")):
    data = file.file.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "PDF must be at most 10 MB.")
    fingerprint = hashlib.sha256(data).hexdigest()
    previous = db.scalar(select(Statement).where(Statement.fingerprint == fingerprint))
    if previous:
        return review(db, previous)
    # Release the lookup connection before CPU/subprocess parsing.
    db.rollback()
    with phase('pdf_parse'):
        parsed = process_pdf(data, password)
    del password
    try:
        statement, account, rows = persist_import(db, file, data, fingerprint, parsed)
        return review(db, statement, account=account, rows=rows)
    except IntegrityError:
        db.rollback()
        statement = db.scalar(select(Statement).where(Statement.fingerprint == fingerprint))
        if statement is None:
            raise
    return review(db, statement)


@phase('bank_import_write')
def persist_import(db, file, data, fingerprint, parsed):
    account = db.scalar(insert(Account).values(
        id=uuid.uuid4(), bank="EBL", identifier=parsed["identifier"], currency="BDT",
        label="EBL · •••• " + parsed["identifier"][-4:], revision=0,
    ).on_conflict_do_nothing(index_elements=["bank", "identifier", "currency"]).returning(Account))
    if account is None:
        account = db.scalar(select(Account).where(Account.bank == "EBL", Account.identifier == parsed["identifier"], Account.currency == "BDT"))
    statement = Statement(account_id=account.id, fingerprint=fingerprint,
        filename=Path(file.filename or "statement.pdf").name[:255], original_pdf=data,
        period_from=date.fromisoformat(parsed["period_from"]), period_to=date.fromisoformat(parsed["period_to"]),
        opening_balance=parsed["opening_balance"], closing_balance=parsed["closing_balance"],
        controls=parsed["controls"], checks=parsed["checks"])
    db.add(statement)
    db.flush()
    rows = []
    for source in parsed["rows"]:
        source["posting_date"] = date.fromisoformat(source["posting_date"])
        source["amount"] = Decimal(source["amount"])
        source["balance"] = Decimal(source["balance"])
        row = StatementRow(statement_id=statement.id, **source)
        db.add(row)
        rows.append(row)
    db.commit()
    return statement, account, rows


@protected.get("/api/imports/{statement_id}", response_model=ImportView)
def get_import(statement_id: uuid.UUID, db: DB):
    return review(db, statement_or_404(db, statement_id))


@protected.patch("/api/imports/{statement_id}/decisions", response_model=ImportView)
def decisions(statement_id: uuid.UUID, body: Decisions, db: DB):
    statement = statement_or_404(db, statement_id, lock=True)
    if statement.status != "draft":
        raise HTTPException(409, "Confirmed imports cannot be edited.")
    current = review(db, statement)
    proposals = {row["id"]: row for row in current["rows"]}
    if len({decision.row_id for decision in body.decisions}) != len(body.decisions):
        raise HTTPException(422, "Each source row can have only one decision.")
    for decision in body.decisions:
        if decision.row_id not in proposals:
            raise HTTPException(422, "Source row does not belong to this import.")
        if decision.decision == "match" and decision.transaction_id not in {c["id"] for c in proposals[decision.row_id]["candidates"]}:
            raise HTTPException(422, "Match must have the same account, posting date, direction and amount.")
        row = db.get(StatementRow, decision.row_id)
        row.decision, row.match_id = decision.decision, decision.transaction_id
    statement.review_revision += 1
    db.commit()
    return review(db, statement)


@protected.post("/api/imports/{statement_id}/confirm", response_model=ImportView)
def confirm(statement_id: uuid.UUID, body: Confirmation, db: DB):
    return confirm_import(statement_id, body, db)


def confirm_import(statement_id, body, db, *, commit=True):
    # A new bank occurrence can change uniqueness in an investment review.
    # Take the shared lock first, before the existing account/statement locks.
    investment_lock(db)
    statement = statement_or_404(db, statement_id, lock=True)
    if statement.status == "confirmed":
        return review(db, statement)
    # Retain ORM rows through validation and persistence. Reloading each row and
    # flushing each transaction makes confirmation depend on per-row DB latency.
    rows = list(db.scalars(select(StatementRow).where(
        StatementRow.statement_id == statement.id).order_by(StatementRow.ordinal)))
    current = review(db, statement, rows=rows)
    if current["revision"] != body.expected_revision:
        raise HTTPException(409, "The account changed since this review. Refresh the review and confirm again.")
    if not current["can_confirm"]:
        raise HTTPException(409, "Resolve duplicate decisions and reconciliation failures before confirming.")
    rows_by_id = {row.id: row for row in rows}
    new_transactions = {}
    for proposal in current["rows"]:
        row = rows_by_id[proposal["id"]]
        if proposal["effective"] == "new":
            new_transactions[row.id] = Transaction(id=uuid.uuid4(), account_id=statement.account_id,
                **{field: getattr(row, field) for field in (*FIELDS, "description")})
    db.add_all(new_transactions.values())
    # Insert all canonical rows together before writing their foreign-key links.
    # This flush and all subsequent updates remain in the same locked transaction.
    db.flush()
    for proposal in current["rows"]:
        row = rows_by_id[proposal["id"]]
        target = new_transactions[row.id].id if proposal["effective"] == "new" else proposal["target_id"]
        row.decision, row.match_id = proposal["effective"], target
        db.add(SourceLink(transaction_id=target, statement_row_id=row.id))
    statement.status = "confirmed"
    db.get(Account, statement.account_id).revision += 1
    if commit:
        db.commit()
    else:
        db.flush()
    return review(db, statement, rows=rows)


@protected.delete("/api/imports/{statement_id}", status_code=204)
def cancel(statement_id: uuid.UUID, db: DB):
    statement = statement_or_404(db, statement_id, lock=True)
    if statement.status == "confirmed":
        raise HTTPException(409, "Confirmed imports cannot be cancelled.")
    db.delete(statement)
    db.commit()
    return Response(status_code=204)


@protected.get("/api/accounts", response_model=list[AccountView])
def accounts(db: DB):
    return [{"id": a.id, "label": a.label, "currency": a.currency} for a in db.scalars(select(Account).order_by(Account.label, Account.id))]


@protected.get("/api/transactions", response_model=TransactionList)
def transactions(db: DB, search: str = Query("", max_length=500), from_date: date | None = None,
                 to_date: date | None = None, direction: Literal["debit", "credit"] | None = None,
                 account_id: uuid.UUID | None = None, page: int = Query(1, ge=1),
                 page_size: int = Query(50, ge=1, le=200),
                 sort: Literal["date_desc", "date_asc", "amount_desc", "amount_asc"] = "date_desc"):
    if from_date and to_date and from_date > to_date:
        raise HTTPException(422, "From date must not be after to date.")
    filters = []
    if search:
        filters.append(Transaction.description.icontains(search, autoescape=True))
    if from_date: filters.append(Transaction.posting_date >= from_date)
    if to_date: filters.append(Transaction.posting_date <= to_date)
    if direction: filters.append(Transaction.direction == direction)
    if account_id: filters.append(Transaction.account_id == account_id)
    # One repeatable-read snapshot keeps rows, counts and totals consistent.
    db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    total, debit, credit = db.execute(select(
        func.count(Transaction.id),
        func.coalesce(func.sum(case((Transaction.direction == "debit", Transaction.amount), else_=0)), 0),
        func.coalesce(func.sum(case((Transaction.direction == "credit", Transaction.amount), else_=0)), 0),
    ).where(*filters)).one()
    order = {"date_desc": Transaction.posting_date.desc(), "date_asc": Transaction.posting_date.asc(),
             "amount_desc": Transaction.amount.desc(), "amount_asc": Transaction.amount.asc()}[sort]
    items = db.scalars(select(Transaction).where(*filters).order_by(order, Transaction.id.desc()).offset((page-1)*page_size).limit(page_size))
    return {"items": [transaction_view(t) for t in items], "total": total, "page": page,
            "page_size": page_size, "debit_total": debit, "credit_total": credit, "net": credit-debit}


@protected.get("/api/transactions/{transaction_id}")
def transaction_detail(transaction_id: uuid.UUID, db: DB):
    transaction = db.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(404, "Transaction not found.")
    sources = db.execute(select(StatementRow, Statement).join(SourceLink, SourceLink.statement_row_id == StatementRow.id)
        .join(Statement, Statement.id == StatementRow.statement_id).where(SourceLink.transaction_id == transaction_id)
        .order_by(Statement.created_at, StatementRow.ordinal))
    # Explicit decimal strings also on the detail endpoint.
    output = transaction_view(transaction)
    output.update(amount=str(transaction.amount), balance=str(transaction.balance))
    output["sources"] = [{"statement_id": s.id, "filename": s.filename, "page": r.page,
                          "ordinal": r.ordinal, "raw_text": r.raw_text, "location": r.location} for r, s in sources]
    return output


app.include_router(protected)
