"""Reviewed investment imports, capital/profit splits and explicit money tracing."""
import hashlib
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .performance import phase
from .db import get_db
from .investment_csv import CSVError, ZERO, parse_investment_csv
from .investment_models import Institution, InvestmentBankLink, InvestmentEvent, InvestmentImport, InvestmentProject, InvestmentSource, ReturnAllocation, ReconciliationCase, ReinvestmentAssociation
from .investment_reconciliation import accepted_differences, bank_evidence, ordered_events
from .models import Account, Transaction

router = APIRouter(prefix='/api/investments', tags=['investments'])
DB = Annotated[Session, Depends(get_db)]
Money = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=2, allow_inf_nan=False)]
PositiveMoney = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2, allow_inf_nan=False)]


def lock(db):
    # Shared by all investment writes, including cross-institution bank allocations.
    db.connection()
    with phase('lock_wait'):
        db.execute(text('SELECT pg_advisory_xact_lock(2048261106)'))


def find(db, model, identity):
    value = db.get(model, identity)
    if value is None:
        raise HTTPException(404, 'Investment record not found.')
    return value


def cash(value):
    return f'{value or ZERO:.2f}'


def total(db, model, column, *conditions):
    return db.scalar(select(func.coalesce(func.sum(column), 0)).select_from(model).where(*conditions))


def event_view(event):
    return dict(id=str(event.id), institution_id=str(event.institution_id), project_id=str(event.project_id) if event.project_id else None,
                posting_date=event.posting_date.isoformat(), kind=event.kind, amount=cash(event.amount), description=event.description,
                reported_profit=cash(event.reported_profit) if event.reported_profit is not None else None,
                principal=cash(event.principal) if event.principal is not None else None,
                profit=cash(event.amount - event.principal) if event.principal is not None else None,
                ready_balance=cash(event.ready_balance), reported_capital=cash(event.reported_capital))


@phase('investment_preview')
def preview(db, draft, institution=None):
    institution = institution if institution is not None else find(db, Institution, draft.institution_id)
    replacing = draft.mode == 'replace' and draft.status == 'draft'
    existing = {} if replacing else {(e.signature, e.occurrence): str(e.id) for e in db.scalars(select(InvestmentEvent).where(InvestmentEvent.institution_id == institution.id))}
    rows = [dict(r, existing_id=existing.get((r['signature'], r['occurrence']))) for r in draft.parsed['rows']]
    replacement = None
    if replacing:
        event_ids = select(InvestmentEvent.id).where(InvestmentEvent.institution_id == institution.id)
        def count(model, *conditions):
            return db.scalar(select(func.count()).select_from(model).where(*conditions))
        replacement = dict(events=count(InvestmentEvent, InvestmentEvent.institution_id == institution.id),
                           projects=count(InvestmentProject, InvestmentProject.institution_id == institution.id),
                           imports=count(InvestmentImport, InvestmentImport.institution_id == institution.id, InvestmentImport.id != draft.id),
                           principal_splits=count(InvestmentEvent, InvestmentEvent.institution_id == institution.id, InvestmentEvent.principal.is_not(None)),
                           allocations=count(ReturnAllocation, or_(ReturnAllocation.source_id.in_(event_ids), ReturnAllocation.target_id.in_(event_ids))),
                           bank_links=count(InvestmentBankLink, InvestmentBankLink.event_id.in_(event_ids)),
                           reconciliation_cases=count(ReconciliationCase, ReconciliationCase.institution_id == institution.id),
                           reinvestment_associations=count(ReinvestmentAssociation, ReinvestmentAssociation.funding_id.in_(event_ids)))
    return dict(id=str(draft.id), institution=institution.name, filename=draft.filename, status=draft.status,
                mode=draft.mode, replacement=replacement,
                revision=institution.revision, rows=rows, warnings=draft.parsed['warnings'], summary=draft.parsed['summary'],
                opening_wallet=draft.parsed['opening_wallet'], closing_wallet=draft.parsed['closing_wallet'],
                new_count=sum(r['existing_id'] is None for r in rows), linked_count=sum(r['existing_id'] is not None for r in rows))


@router.post('/imports')
def upload(db: DB, file: UploadFile = File(...), institution: str = Form('Biniyog'), mode: Literal['extend', 'replace'] = Form('extend')):
    name = ' '.join(institution.split()).casefold()
    if not 1 <= len(name) <= 120:
        raise HTTPException(422, 'Enter an institution name of 1–120 characters.')
    data = file.file.read(2 * 1024 * 1024 + 1)
    try:
        with phase('csv_parse'):
            parsed = parse_investment_csv(data)
    except CSVError as exc:
        raise HTTPException(422, str(exc)) from None
    lock(db)
    owner = db.scalar(insert(Institution).values(id=uuid.uuid4(), name=name, revision=0)
        .on_conflict_do_nothing(index_elements=['name']).returning(Institution))
    if owner is None:
        owner = db.scalar(select(Institution).where(Institution.name == name))
    fingerprint = hashlib.sha256(data).hexdigest()
    query = select(InvestmentImport).where(InvestmentImport.institution_id == owner.id, InvestmentImport.fingerprint == fingerprint, InvestmentImport.mode == mode)
    if mode == 'replace':
        query = query.where(InvestmentImport.status == 'draft')
    draft = db.scalar(query)
    if draft is None:
        draft = InvestmentImport(institution_id=owner.id, fingerprint=fingerprint,
                                 filename=Path((file.filename or 'transactions.csv').replace('\\', '/')).name[:255], original_csv=data, parsed=parsed, mode=mode)
        db.add(draft)
        db.flush()
    result = preview(db, draft, owner)
    db.commit()
    return result


@router.get('/imports')
def imports(db: DB):
    return [dict(id=str(d.id), filename=d.filename, institution=d.institution, status=d.status, mode=d.mode, created_at=d.created_at.isoformat())
            for d in db.execute(select(InvestmentImport.id, InvestmentImport.filename, InvestmentImport.status, InvestmentImport.mode, InvestmentImport.created_at, Institution.name.label('institution')).join(Institution).order_by(InvestmentImport.created_at.desc()).limit(100))]


@router.get('/imports/{identity}')
def get_import(identity: uuid.UUID, db: DB):
    return preview(db, find(db, InvestmentImport, identity))


class Confirm(BaseModel):
    expected_revision: int = Field(ge=0)
    acknowledge_warnings: bool = False
    acknowledge_replace: bool = False


def clear_institution(db, owner_id, keep_import):
    """Called only inside the locked confirmation transaction; never delete bank rows."""
    event_ids = select(InvestmentEvent.id).where(InvestmentEvent.institution_id == owner_id)
    import_ids = select(InvestmentImport.id).where(InvestmentImport.institution_id == owner_id)
    db.execute(delete(ReconciliationCase).where(ReconciliationCase.institution_id == owner_id))
    db.execute(delete(ReinvestmentAssociation).where(ReinvestmentAssociation.funding_id.in_(event_ids)))
    db.execute(delete(InvestmentBankLink).where(InvestmentBankLink.event_id.in_(event_ids)))
    db.execute(delete(ReturnAllocation).where(or_(ReturnAllocation.source_id.in_(event_ids), ReturnAllocation.target_id.in_(event_ids))))
    db.execute(delete(InvestmentSource).where(or_(InvestmentSource.event_id.in_(event_ids), InvestmentSource.import_id.in_(import_ids))))
    db.execute(delete(InvestmentEvent).where(InvestmentEvent.institution_id == owner_id))
    db.execute(delete(InvestmentProject).where(InvestmentProject.institution_id == owner_id))
    db.execute(delete(InvestmentImport).where(InvestmentImport.institution_id == owner_id, InvestmentImport.id != keep_import))


@router.post('/imports/{identity}/confirm')
def confirm(identity: uuid.UUID, body: Confirm, db: DB):
    return confirm_import(identity, body, db)


def confirm_import(identity, body, db, *, commit=True):
    lock(db)
    draft = find(db, InvestmentImport, identity)
    if draft.status == 'confirmed':
        return preview(db, draft)
    owner = find(db, Institution, draft.institution_id)
    if owner.revision != body.expected_revision:
        raise HTTPException(409, 'Investment data changed. Refresh this preview before confirming.')
    if draft.parsed['warnings'] and not body.acknowledge_warnings:
        raise HTTPException(422, 'Acknowledge the source warnings before confirming.')
    if draft.mode == 'replace':
        if not body.acknowledge_replace:
            raise HTTPException(422, 'Acknowledge replacement of all investment data for this institution before confirming.')
        clear_institution(db, owner.id, draft.id)
    projects = {p.name: p for p in db.scalars(select(InvestmentProject).where(InvestmentProject.institution_id == owner.id))}
    existing = {(e.signature, e.occurrence): e for e in db.scalars(select(InvestmentEvent).where(InvestmentEvent.institution_id == owner.id))}
    new_projects, new_events, sources = [], [], []
    for row in draft.parsed['rows']:
        key = (row['signature'], row['occurrence'])
        event = existing.get(key)
        if event is None:
            name = row['project']
            if name and name not in projects:
                projects[name] = InvestmentProject(id=uuid.uuid4(), institution_id=owner.id, name=name, status='active')
                new_projects.append(projects[name])
            if name and projects[name].status == 'closed':
                raise HTTPException(409, 'This export adds activity to a closed project. Reopen the project and refresh before confirming.')
            event = InvestmentEvent(id=uuid.uuid4(), institution_id=owner.id, project_id=projects[name].id if name else None,
                                    **{k: row[k] for k in ('signature', 'occurrence', 'kind', 'amount', 'description', 'reported_profit', 'ready_balance', 'reported_capital')},
                                    posting_date=date.fromisoformat(row['posting_date']))
            new_events.append(event); existing[key] = event
        sources.append(InvestmentSource(id=uuid.uuid4(), import_id=draft.id, ordinal=row['ordinal'], event_id=event.id))
    # Respect FK order without one database flush per CSV row. All phases remain
    # in the same transaction; any validation or insert failure rolls back all.
    with phase('investment_write'):
        db.add_all(new_projects)
        db.flush()
        db.add_all(new_events)
        db.flush()
        db.add_all(sources)
    draft.status = 'confirmed'
    owner.revision += 1
    db.flush()
    result = preview(db, draft)
    if commit:
        db.commit()
    return result


@router.delete('/imports/{identity}', status_code=204)
def cancel(identity: uuid.UUID, db: DB):
    lock(db)
    draft = find(db, InvestmentImport, identity)
    if draft.status != 'draft':
        raise HTTPException(409, 'Confirmed investment imports cannot be cancelled.')
    db.delete(draft); db.commit()


@router.get('')
@phase('investment_workspace')
def workspace(db: DB):
    # One consistent snapshot for totals, links and available allocation amounts.
    db.connection(execution_options={'isolation_level': 'REPEATABLE READ'})
    reports = select(InvestmentImport.institution_id,
        InvestmentImport.parsed['closing_wallet'].astext.label('balance'),
        InvestmentImport.parsed['rows'][-1]['posting_date'].astext.label('as_of')).where(
        InvestmentImport.status == 'confirmed').distinct(InvestmentImport.institution_id).order_by(
        InvestmentImport.institution_id, InvestmentImport.parsed['rows'][-1]['posting_date'].astext.desc(),
        InvestmentImport.created_at.desc()).subquery()
    headers = list(db.execute(select(Institution, InvestmentProject, reports.c.balance, reports.c.as_of)
        .outerjoin(InvestmentProject, InvestmentProject.institution_id == Institution.id)
        .outerjoin(reports, reports.c.institution_id == Institution.id).order_by(Institution.name, InvestmentProject.name)))
    institutions = list({i.id: i for i, _, _, _ in headers}.values())
    projects = sorted([p for _, p, _, _ in headers if p is not None], key=lambda p: p.name)
    latest_reports = {i.id: dict(balance=balance, as_of=as_of) for i, _, balance, as_of in headers if as_of is not None}
    events = list(db.scalars(select(InvestmentEvent).order_by(InvestmentEvent.posting_date, InvestmentEvent.id)))
    allocations = list(db.scalars(select(ReturnAllocation)))
    bank_links = list(db.execute(select(InvestmentBankLink, Transaction, Account).select_from(InvestmentBankLink)
                               .join(Transaction, Transaction.id == InvestmentBankLink.transaction_id).join(Account, Account.id == Transaction.account_id)))
    differences = accepted_differences(events, allocations, list(db.scalars(select(ReconciliationCase))))
    by_id = {e.id: e for e in events}
    outgoing, incoming, bank = {}, {}, {}
    for allocation in allocations:
        outgoing[allocation.source_id] = outgoing.get(allocation.source_id, ZERO) + allocation.amount
        incoming[allocation.target_id] = incoming.get(allocation.target_id, ZERO) + allocation.amount
    for link, _, _ in bank_links:
        bank[link.event_id] = bank.get(link.event_id, ZERO) + link.amount
    events_by_project, events_by_institution, allocations_by_source = defaultdict(list), defaultdict(list), defaultdict(list)
    for e in events:
        events_by_project[e.project_id].append(e)
        events_by_institution[e.institution_id].append(e)
    for a in allocations:
        allocations_by_source[a.source_id].append(a)
    result = []
    for p in projects:
        rows = events_by_project[p.id]
        project_allocations = [a for e in rows for a in allocations_by_source[e.id]]
        invested = sum((e.amount for e in rows if e.kind in ('investment', 'reinvestment')), ZERO)
        repaid = sum((e.amount for e in rows if e.kind in ('repayment', 'refund')), ZERO)
        classified = [e for e in rows if e.kind == 'repayment' and e.principal is not None]
        refunds = sum((e.amount for e in rows if e.kind == 'refund'), ZERO)
        principal = refunds + sum((e.principal for e in classified), ZERO)
        unclassified = sum((e.amount for e in rows if e.kind == 'repayment' and e.principal is None), ZERO)
        profit = sum((e.amount - e.principal for e in classified), ZERO)
        cashout = sum((a.amount for a in project_allocations if by_id[a.target_id].kind == 'cashout'), ZERO)
        reinvested = sum((a.amount for a in project_allocations if by_id[a.target_id].kind == 'reinvestment'), ZERO)
        result.append(dict(id=str(p.id), institution_id=str(p.institution_id), name=p.name, status=p.status,
                           expected_total=cash(p.expected_total) if p.expected_total is not None else None,
                           invested=cash(invested), returned=cash(repaid), external_funding=cash(sum((e.amount for e in rows if e.kind == 'investment'), ZERO)),
                           reinvestment_funding=cash(sum((e.amount for e in rows if e.kind == 'reinvestment'), ZERO)),
                           classified_profit=cash(profit), principal_returned=cash(principal), unclassified_returns=cash(unclassified),
                           principal_outstanding='0.00' if p.status == 'closed' else cash(invested - principal) if unclassified == ZERO and principal <= invested else None,
                           capital_written_off=cash(max(ZERO, invested - principal)) if p.status == 'closed' and unclassified == ZERO else None,
                           net_recovery=cash(repaid - invested), final_gain=cash(repaid - invested) if p.status == 'closed' else None,
                           roi_percent=cash((repaid - invested) / invested * 100) if p.status == 'closed' and invested else None,
                           expected_remaining=cash(max(ZERO, p.expected_total - repaid)) if p.expected_total is not None else None,
                           has_settlement_difference=any(str(a.target_id) in differences for a in project_allocations),
                           traced_to_bank_payout=cash(cashout), traced_to_reinvestment=cash(reinvested),
                           untraced_returns=cash(repaid - cashout - reinvested)))
    source_rows = list(db.execute(select(InvestmentSource, InvestmentImport.id, InvestmentImport.filename, InvestmentImport.status).join(InvestmentImport)))
    sources_by_event = defaultdict(list)
    for source, import_id, filename, status in source_rows:
        sources_by_event[source.event_id].append(dict(import_id=str(import_id), filename=filename, ordinal=source.ordinal))
    sequences = {}
    for source, import_id, _, status in sorted(source_rows, key=lambda pair: (str(pair[0].import_id), pair[0].ordinal)):
        if status == 'confirmed': sequences.setdefault(import_id, []).append(source.event_id)
    ordered = []
    for institution in institutions:
        own = events_by_institution[institution.id]
        ids = {e.id for e in own}
        rows, _ = ordered_events(own, [seq for seq in sequences.values() if seq and seq[0] in ids])
        ordered.extend(rows or own)
    events = sorted(ordered, key=lambda e: e.posting_date)
    views = []
    for e in events:
        views.append(dict(event_view(e), difference=differences.get(str(e.id)), allocated_out=cash(outgoing.get(e.id, ZERO)), allocated_in=cash(incoming.get(e.id, ZERO)), bank_linked=cash(bank.get(e.id, ZERO)),
                          sources=sources_by_event[e.id]))
    return dict(institutions=[dict(id=str(i.id), name=i.name,
                                  wallet_balance=latest_reports[i.id]['balance'] if i.id in latest_reports else None,
                                  wallet_as_of=latest_reports[i.id]['as_of'] if i.id in latest_reports else None) for i in institutions], projects=result, events=views,
                allocations=[dict(id=str(a.id), source_id=str(a.source_id), target_id=str(a.target_id), amount=cash(a.amount)) for a in allocations],
                bank_links=[dict(id=str(l.id), event_id=str(l.event_id), transaction_id=str(t.id), amount=cash(l.amount), posting_date=t.posting_date.isoformat(), description=t.description, account=a.label) for l, t, a in bank_links])


class ProjectEdit(BaseModel):
    status: Literal['active', 'closed']
    expected_total: Money | None = None
    complete_history: bool = False
    confirm_wallet_empty: bool = False


@router.patch('/projects/{identity}')
def edit_project(identity: uuid.UUID, body: ProjectEdit, db: DB):
    lock(db)
    project = find(db, InvestmentProject, identity)
    if body.status == 'closed' and not body.complete_history:
        raise HTTPException(422, 'Confirm that all investment history is imported and all project payments are final before closing.')
    if body.status == 'closed' and total(db, InvestmentEvent, InvestmentEvent.amount, InvestmentEvent.project_id == project.id, InvestmentEvent.kind.in_(['investment', 'reinvestment'])) <= ZERO:
        raise HTTPException(422, 'Import this project’s investment funding before calculating its final gain or loss.')
    if body.status == 'closed' and body.confirm_wallet_empty:
        from .reconciliation_api import analyze
        context = analyze(db)
        credits = [e for e in context['events'] if e.project_id == project.id and e.kind in ('repayment', 'refund')]
        wallet = context['wallets'][str(project.institution_id)]['credits']
        for credit in credits:
            used = sum((a.amount for a in context['allocations'] if a.source_id == credit.id), ZERO)
            state = wallet.get(str(credit.id), {})
            if used != credit.amount or Decimal(state.get('reserved', '0.00')) != ZERO or Decimal(state.get('available') or '0.00') != ZERO:
                raise HTTPException(409, 'Wallet funds remain or their destinations are unresolved. Match cashouts or settle reinvestments in Money reconciliation before completing.')
    project.status, project.expected_total = body.status, body.expected_total
    find(db, Institution, project.institution_id).revision += 1
    db.commit()
    return {'status': project.status}


class Split(BaseModel):
    principal: Money | None


@router.patch('/events/{identity}/split')
def split(identity: uuid.UUID, body: Split, db: DB):
    lock(db)
    event = find(db, InvestmentEvent, identity)
    if event.kind != 'repayment' or (body.principal is not None and body.principal > event.amount):
        raise HTTPException(422, 'Principal must be between zero and the repayment amount.')
    if find(db, InvestmentProject, event.project_id).status == 'closed':
        raise HTTPException(409, 'Reopen this project before changing its return classification.')
    if body.principal is not None:
        invested = total(db, InvestmentEvent, InvestmentEvent.amount, InvestmentEvent.project_id == event.project_id, InvestmentEvent.kind.in_(['investment', 'reinvestment']))
        other_principal = total(db, InvestmentEvent, InvestmentEvent.principal, InvestmentEvent.project_id == event.project_id, InvestmentEvent.id != event.id)
        refunds = total(db, InvestmentEvent, InvestmentEvent.amount, InvestmentEvent.project_id == event.project_id, InvestmentEvent.kind == 'refund')
        if other_principal + refunds + body.principal > invested:
            raise HTTPException(422, 'Classified returned capital exceeds this project’s recorded investment. Check the split or import its earlier investment history.')
    event.principal = body.principal
    find(db, Institution, event.institution_id).revision += 1
    db.commit()
    return {'id': str(event.id)}


class AllocationBody(BaseModel):
    source_id: uuid.UUID
    target_id: uuid.UUID
    amount: PositiveMoney


@router.post('/allocations')
def allocate(body: AllocationBody, db: DB):
    lock(db)
    source, target = find(db, InvestmentEvent, body.source_id), find(db, InvestmentEvent, body.target_id)
    if source.institution_id != target.institution_id or source.kind not in ('repayment', 'refund') or target.kind not in ('reinvestment', 'cashout') or source.posting_date > target.posting_date:
        raise HTTPException(422, 'Trace a repayment/refund to a same-institution reinvestment or bank payout on or after its date.')
    if db.scalar(select(ReturnAllocation).where(ReturnAllocation.source_id == source.id, ReturnAllocation.target_id == target.id)):
        raise HTTPException(409, 'This pair already has an allocation. Remove it before replacing it.')
    if body.amount + total(db, ReturnAllocation, ReturnAllocation.amount, ReturnAllocation.source_id == source.id) > source.amount or body.amount + total(db, ReturnAllocation, ReturnAllocation.amount, ReturnAllocation.target_id == target.id) > target.amount:
        raise HTTPException(422, 'The allocation exceeds the available return or the destination amount.')
    allocation = ReturnAllocation(**body.model_dump())
    find(db, Institution, source.institution_id).revision += 1
    db.add(allocation); db.flush(); identity = str(allocation.id); db.commit()
    return {'id': identity}


@router.delete('/allocations/{identity}', status_code=204)
def remove_allocation(identity: uuid.UUID, db: DB):
    lock(db)
    allocation = find(db, ReturnAllocation, identity)
    find(db, Institution, find(db, InvestmentEvent, allocation.source_id).institution_id).revision += 1
    db.delete(allocation); db.commit()


@router.get('/events/{identity}/bank-candidates')
def candidates(identity: uuid.UUID, db: DB, days: int = Query(3, ge=0, le=30), account_id: uuid.UUID | None = None):
    event = find(db, InvestmentEvent, identity)
    if event.kind not in ('investment', 'cashout'):
        raise HTTPException(422, 'Only external investments and cashouts have bank transfers.')
    depositing = event.kind == 'investment'
    start = event.posting_date - timedelta(days=days) if depositing else event.posting_date
    end = event.posting_date if depositing else event.posting_date + timedelta(days=days)
    conditions = [Transaction.direction == ('debit' if depositing else 'credit'), Transaction.posting_date.between(start, end), Account.currency == 'BDT']
    if account_id: conditions.append(Account.id == account_id)
    matches = []
    for t, a in db.execute(select(Transaction, Account).join(Account).where(*conditions).order_by(Transaction.posting_date, Transaction.id)):
        remaining = t.amount - total(db, InvestmentBankLink, InvestmentBankLink.amount, InvestmentBankLink.transaction_id == t.id)
        if remaining <= ZERO: continue
        matches.append(dict(id=str(t.id), account_id=str(a.id), account=a.label, posting_date=t.posting_date.isoformat(), description=t.description,
                            amount=cash(t.amount), available=cash(remaining), exact_amount=remaining == event.amount - total(db, InvestmentBankLink, InvestmentBankLink.amount, InvestmentBankLink.event_id == event.id),
                            **bank_evidence(event, t),
                            days_difference=(event.posting_date - t.posting_date).days))
    return {'from_date': start.isoformat(), 'to_date': end.isoformat(), 'items': sorted(matches, key=lambda m: not m['exact_amount'])}


class BankBody(BaseModel):
    event_id: uuid.UUID
    transaction_id: uuid.UUID
    amount: PositiveMoney


@router.post('/bank-links')
def link_bank(body: BankBody, db: DB):
    lock(db)
    event, transaction = find(db, InvestmentEvent, body.event_id), find(db, Transaction, body.transaction_id)
    if event.kind not in ('investment', 'cashout') or transaction.direction != ('debit' if event.kind == 'investment' else 'credit') or find(db, Account, transaction.account_id).currency != 'BDT':
        raise HTTPException(422, 'Select a BDT debit for an external investment or a BDT credit for a bank payout.')
    if db.scalar(select(InvestmentBankLink).where(InvestmentBankLink.event_id == event.id, InvestmentBankLink.transaction_id == transaction.id)):
        raise HTTPException(409, 'This bank transaction is already linked to the event.')
    if body.amount + total(db, InvestmentBankLink, InvestmentBankLink.amount, InvestmentBankLink.event_id == event.id) > event.amount or body.amount + total(db, InvestmentBankLink, InvestmentBankLink.amount, InvestmentBankLink.transaction_id == transaction.id) > transaction.amount:
        raise HTTPException(422, 'The link exceeds the unlinked investment or bank transaction amount.')
    find(db, Institution, event.institution_id).revision += 1
    link = InvestmentBankLink(**body.model_dump()); db.add(link); db.flush(); identity = str(link.id); db.commit()
    return {'id': identity}


@router.delete('/bank-links/{identity}', status_code=204)
def unlink_bank(identity: uuid.UUID, db: DB):
    lock(db)
    link = find(db, InvestmentBankLink, identity)
    find(db, Institution, find(db, InvestmentEvent, link.event_id).institution_id).revision += 1
    db.delete(link); db.commit()
