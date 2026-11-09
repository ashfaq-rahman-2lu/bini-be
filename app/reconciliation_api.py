"""Transactional persistence and read models for investment reconciliation."""
import hashlib
import json
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .investment_models import (Institution, InvestmentBankLink, InvestmentEvent, InvestmentImport,
    InvestmentProject, InvestmentSource, ReconciliationCase, ReinvestmentAssociation, ReturnAllocation)
from .investment_reconciliation import ZERO, DIFFERENCE_LIMIT, accepted_differences, bank_evidence, cash, wallet_replay
from .investments import DB, PositiveMoney, event_view, find, lock
from .models import Account, Transaction

router = APIRouter(prefix='/api/investments', tags=['investment reconciliation'])


def record(row, fields):
    return SimpleNamespace(**{k: str(v) if isinstance(v := getattr(row, k), uuid.UUID) else v for k in fields})


def analyze(db):
    owners = list(db.scalars(select(Institution).order_by(Institution.id)))
    events = list(db.scalars(select(InvestmentEvent)))
    by_id = {str(e.id): e for e in events}
    allocations = list(db.scalars(select(ReturnAllocation)))
    associations = list(db.scalars(select(ReinvestmentAssociation)))
    links = list(db.scalars(select(InvestmentBankLink)))
    cases = list(db.scalars(select(ReconciliationCase)))
    accounts = list(db.scalars(select(Account).order_by(Account.id)))
    bank = list(db.scalars(select(Transaction)))
    bank_by_id = {str(t.id): t for t in bank}
    account_by_id = {a.id: a for a in accounts}
    sources = list(db.execute(select(InvestmentSource, InvestmentImport).join(InvestmentImport).where(InvestmentImport.status == 'confirmed').order_by(InvestmentSource.import_id, InvestmentSource.ordinal)))
    sequences = defaultdict(list)
    for source, report in sources: sequences[str(report.id)].append(str(source.event_id))
    token = hashlib.sha256(json.dumps([[(str(i.id), i.revision) for i in owners], [(str(a.id), a.revision) for a in accounts]], sort_keys=True).encode()).hexdigest()
    wallets, issues, proposed, groups = {}, [], [], []
    for owner in owners:
        identity = str(owner.id)
        own = [record(e, ('id', 'project_id', 'posting_date', 'kind', 'amount', 'ready_balance')) for e in events if e.institution_id == owner.id]
        own_ids = {e.id for e in own}
        reviewed = {}
        decisions = [(str(c.event_id), r) for c in cases if c.institution_id == owner.id for r in c.resolutions if r.get('delayed_booking') or r.get('accept_difference')]
        for key, resolution in sorted(decisions, key=lambda pair: pair[1]['reviewed_at']): reviewed[key] = resolution
        outcome = wallet_replay(own, [seq for seq in sequences.values() if seq and seq[0] in own_ids],
            [record(a, ('source_id', 'target_id', 'amount')) for a in allocations if str(a.target_id) in own_ids and a.provenance == 'manual'],
            [record(a, ('funding_id', 'event_id')) for a in associations if str(a.funding_id) in own_ids and a.provenance == 'manual'], reviewed)
        desired = {(p['source_id'], p['target_id'], p['amount']) for p in outcome['proposals']}
        wanted_groups = {(g['funding_id'], k) for g in outcome['groups'] for k in g['event_ids']}
        conflicts = [a for a in allocations if str(a.target_id) in own_ids and a.provenance == 'automatic' and (str(a.source_id), str(a.target_id), cash(a.amount)) not in desired]
        group_conflicts = [a for a in associations if str(a.funding_id) in own_ids and a.provenance == 'automatic' and (str(a.funding_id), str(a.event_id)) not in wanted_groups]
        if conflicts or group_conflicts:
            outcome['issues'].append(dict(key='prior_automatic:wallet', kind='prior_automatic', event_id=None,
                message='Earlier automatic decisions no longer follow uniquely from the current source history. Existing links were preserved. Remove the conflicting links and reconcile again.',
                allocation_ids=[str(a.id) for a in conflicts], association_ids=[str(a.id) for a in group_conflicts]))
            outcome['proposals'], outcome['groups'] = [], []
            outcome['wallet_status'] = 'needs_review'
            for credit in outcome['credits'].values(): credit['available'] = None
        wallets[identity] = outcome
        issues.extend(dict(i, institution_id=identity) for i in outcome['issues'])
        proposed.extend(dict(p, institution_id=identity) for p in outcome['proposals'])
        groups.extend(dict(g, institution_id=identity) for g in outcome['groups'])
    # Capacity is shared across every institution. Only mutual unique bank matches
    # qualify: two events competing for one transaction are both left for review.
    used_bank, used_event = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    for link in links:
        used_bank[str(link.transaction_id)] += link.amount
        used_event[str(link.event_id)] += link.amount
    bank_proposals, options, competitors = [], {}, defaultdict(list)
    manual_bank, manual_event = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    manual_pairs = set()
    for link in links:
        if link.provenance == 'manual':
            manual_pairs.add((str(link.event_id), str(link.transaction_id)))
            manual_bank[str(link.transaction_id)] += link.amount
            manual_event[str(link.event_id)] += link.amount
    invalid_bank = set()
    for link in links:
        e, t = by_id[str(link.event_id)], bank_by_id[str(link.transaction_id)]
        evidence = bank_evidence(e, t)
        if (e.kind not in ('investment', 'cashout') or t.direction != ('debit' if e.kind == 'investment' else 'credit')
            or account_by_id[t.account_id].currency != 'BDT' or used_bank[str(t.id)] > t.amount or used_event[str(e.id)] > e.amount
            or link.provenance == 'automatic' and not evidence['eligible']):
            invalid_bank.add(str(e.id))
            issues.append(dict(key=f'bank_conflict:{e.id}', kind='bank_conflict', event_id=str(e.id), institution_id=str(e.institution_id),
                               message='An existing bank link conflicts with direction, currency, amount capacity, or automatic matching evidence. Remove or correct the preserved link.'))
    for e in events:
        if e.kind not in ('investment', 'cashout') or str(e.id) in invalid_bank: continue
        remaining = e.amount - manual_event[str(e.id)]
        if remaining <= ZERO: continue
        matches = []
        for t in bank:
            if (str(e.id), str(t.id)) in manual_pairs: continue
            if account_by_id[t.account_id].currency != 'BDT' or t.amount - manual_bank[str(t.id)] != remaining: continue
            evidence = bank_evidence(e, t)
            if evidence['eligible']:
                matches.append(dict(transaction_id=str(t.id), amount=cash(remaining), posting_date=t.posting_date.isoformat(),
                                    description=t.description, business_date=evidence['business_date']))
                competitors[str(t.id)].append(str(e.id))
        options[str(e.id)] = matches
    desired_bank = set()
    existing_bank = {(str(l.event_id), str(l.transaction_id), cash(l.amount)) for l in links if l.provenance == 'automatic'}
    for key, matches in options.items():
        e = by_id[key]
        if len(matches) == 1 and len(competitors[matches[0]['transaction_id']]) == 1:
            candidate = (key, matches[0]['transaction_id'], matches[0]['amount'])
            desired_bank.add(candidate)
            if candidate not in existing_bank:
                bank_proposals.append(dict(event_id=key, institution_id=str(e.institution_id), **matches[0],
                                           explanation='Unique exact remaining amount, expected bank description, direction and 0–3 day window.'))
        else:
            issues.append(dict(key=f'bank_match:{key}', kind='bank_match', event_id=key, institution_id=str(e.institution_id),
                message='No unique bank match. Review candidates or use the movement’s manual bank matching for wider dates and split transfers.', candidates=matches))
    conflicts = existing_bank - desired_bank
    if conflicts:
        # Preserve decisions for review and do not spend any potentially disputed
        # transaction capacity in this run, including across institutions.
        bank_proposals = []
        for key, transaction_id, _ in conflicts:
            e = by_id[key]
            if key in invalid_bank: continue
            issues.append(dict(key=f'bank_conflict:{key}', kind='bank_conflict', event_id=key, institution_id=str(e.institution_id),
                message='An earlier automatic bank match is no longer unique under the current history. The link was preserved; review and remove it before rematching.', transaction_id=transaction_id))
    issues = list({(i['institution_id'], i['key']): i for i in issues}.values())
    return dict(owners=owners, events=events, by_id=by_id, allocations=allocations, associations=associations, links=links,
                cases=cases, bank=bank_by_id, accounts=account_by_id, sources=sources, token=token, wallets=wallets,
                issues=issues, proposals=proposed, groups=groups, bank_proposals=bank_proposals,
                differences=accepted_differences(events, allocations, cases))


def resolved_matches(context):
    """Group saved connections by payment, retaining reviews without double counting."""
    active = {(i['institution_id'], i['key']) for i in context['issues']}
    cases = [c for c in context['cases'] if c.status == 'resolved' and (str(c.institution_id), c.key) not in active]
    groups = defaultdict(list)
    for link in context['links']:
        groups[('bank', str(link.event_id))].append(link)
    for allocation in context['allocations']:
        groups[('wallet', str(allocation.target_id))].append(allocation)
    result, included = [], set()
    for (side, event_id), connections in groups.items():
        event = context['by_id'][event_id]
        # A preserved link can be incomplete or conflicting; it is not resolved.
        if any(i['institution_id'] == str(event.institution_id)
               and i['event_id'] in (None, event_id)
               and i['kind'].startswith('bank_') == (side == 'bank') for i in context['issues']):
            continue
        difference = Decimal(context['differences'].get(event_id, {}).get('amount', '0')) if side == 'wallet' else ZERO
        if sum((link.amount for link in connections), ZERO) != event.amount + difference:
            continue
        reviews = [c for c in cases if str(c.event_id) == event_id and c.kind.startswith('bank_') == (side == 'bank')]
        included.update(c.id for c in reviews)
        resolutions = sorted([r for c in reviews for r in c.resolutions], key=lambda r: r['reviewed_at'])
        dates = [link.created_at for link in connections] + [c.resolved_at for c in reviews if c.resolved_at]
        result.append(dict(id=f'{side}:{event_id}', kind=f'{side}_match', event_id=event_id, amount=cash(event.amount),
                           institution_id=str(event.institution_id), status='resolved', connection_count=len(connections),
                           provenance='manual' if resolutions or any(link.provenance == 'manual' for link in connections) else 'automatic',
                           explanation=' '.join(dict.fromkeys(link.explanation for link in connections if link.explanation)),
                           resolved_at=max(dates).isoformat(), resolutions=resolutions))
    for case in cases:
        if case.id in included or case.kind in ('bank_match', 'wallet_match', 'delayed_booking'):
            continue
        event = context['by_id'].get(str(case.event_id))
        result.append(dict(id=f'case:{case.id}', kind=case.kind, event_id=str(case.event_id) if case.event_id else None,
                           amount=cash(event.amount) if event else None, institution_id=str(case.institution_id), status='resolved',
                           connection_count=0, provenance='manual' if case.resolutions else 'automatic',
                           explanation=case.details.get('message', ''),
                           resolved_at=case.resolved_at.isoformat() if case.resolved_at else None, resolutions=case.resolutions))
    return sorted(result, key=lambda row: (row['resolved_at'] or '', row['id']), reverse=True)


def public_snapshot(context):
    stored = {(str(c.institution_id), c.key): c for c in context['cases']}
    issues = []
    for i in context['issues']:
        saved = stored.get((i['institution_id'], i['key']))
        issues.append(dict(i, id=str(saved.id) if saved else None, resolutions=saved.resolutions if saved else []))
    existing = {(str(a.source_id), str(a.target_id)) for a in context['allocations']}
    pending = [p for p in context['proposals'] if (p['source_id'], p['target_id']) not in existing]
    return dict(revision=context['token'], institutions=[dict(id=str(o.id), name=o.name,
        **{k: context['wallets'][str(o.id)][k] for k in ('wallet_status', 'opening', 'closing', 'credits', 'ordered_ids')},
        bank_status='needs_review' if any(i['institution_id'] == str(o.id) and i['kind'].startswith('bank_') for i in issues) else 'matched') for o in context['owners']],
        differences=list(context['differences'].values()),
        difference_total=cash(sum((abs(Decimal(d['amount'])) for d in context['differences'].values()), ZERO)),
        issues=issues, resolved=resolved_matches(context), pending_allocations=pending, pending_bank_links=context['bank_proposals'],
        history=[dict(id=str(c.id), kind=c.kind, event_id=str(c.event_id) if c.event_id else None, status=c.status,
                      resolutions=c.resolutions) for c in context['cases'] if c.resolutions])


def reconcile(db):
    context = analyze(db)
    changed, counts = set(), dict(allocations=0, bank_links=0, associations=0)
    existing = {(str(a.source_id), str(a.target_id)) for a in context['allocations']}
    for p in context['proposals']:
        if (p['source_id'], p['target_id']) in existing: continue
        db.add(ReturnAllocation(source_id=uuid.UUID(p['source_id']), target_id=uuid.UUID(p['target_id']), amount=Decimal(p['amount']), provenance='automatic', explanation=p['explanation']))
        changed.add(p['institution_id']); counts['allocations'] += 1
    existing_groups = {(str(a.funding_id), str(a.event_id)) for a in context['associations']}
    for g in context['groups']:
        for key in g['event_ids']:
            if (g['funding_id'], key) in existing_groups: continue
            db.add(ReinvestmentAssociation(funding_id=uuid.UUID(g['funding_id']), event_id=uuid.UUID(key), provenance='automatic', explanation='Unique destination funding and settlement group.'))
            changed.add(g['institution_id']); counts['associations'] += 1
    for p in context['bank_proposals']:
        db.add(InvestmentBankLink(event_id=uuid.UUID(p['event_id']), transaction_id=uuid.UUID(p['transaction_id']), amount=Decimal(p['amount']), provenance='automatic', explanation=p['explanation']))
        changed.add(p['institution_id']); counts['bank_links'] += 1
    db.flush()
    # Store issues from the resulting state, not those just resolved by proposals.
    after = analyze(db)
    saved = {(str(c.institution_id), c.key): c for c in after['cases']}
    active = set()
    for issue in after['issues']:
        key = (issue['institution_id'], issue['key']); active.add(key)
        case = saved.get(key)
        if not case:
            db.add(ReconciliationCase(institution_id=uuid.UUID(key[0]), key=key[1], event_id=uuid.UUID(issue['event_id']) if issue['event_id'] else None,
                                      kind=issue['kind'], status='open', details=issue, resolutions=[]))
            changed.add(key[0])
        elif case.status != 'open' or case.details != issue:
            case.status, case.details, case.resolved_at = 'open', issue, None
            changed.add(key[0])
    for key, case in saved.items():
        if key not in active and case.status == 'open':
            case.status, case.resolved_at = 'resolved', datetime.now(timezone.utc); changed.add(key[0])
    for owner in after['owners']:
        if str(owner.id) in changed: owner.revision += 1
    db.flush()
    return dict(public_snapshot(analyze(db)), saved=counts)


@router.get('/reconciliation')
def status(db: DB):
    db.connection(execution_options={'isolation_level': 'REPEATABLE READ'})
    return public_snapshot(analyze(db))


@router.post('/reconciliation/run')
def run(db: DB):
    lock(db)
    result = reconcile(db)
    db.commit()
    return result


class CreditDecision(BaseModel):
    source_id: uuid.UUID
    amount: PositiveMoney


class TransferDecision(BaseModel):
    transaction_id: uuid.UUID
    amount: PositiveMoney


class Resolution(BaseModel):
    expected_revision: str
    explanation: str = Field(min_length=3, max_length=2000)
    accept_difference: bool = False
    delayed_booking: bool = False
    instruction_date: date | None = None
    allocations: list[CreditDecision] = Field(default_factory=list, max_length=1000)
    association_ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)
    bank_links: list[TransferDecision] = Field(default_factory=list, max_length=1000)


def apply_resolution(identity: uuid.UUID, body: Resolution, db):
    lock(db)
    before = analyze(db)
    if before['token'] != body.expected_revision:
        raise HTTPException(409, 'Investment or bank data changed. Refresh reconciliation before saving this review.')
    case = find(db, ReconciliationCase, identity)
    active = next((i for i in before['issues'] if i['institution_id'] == str(case.institution_id) and i['key'] == case.key), None)
    if not active or not case.event_id:
        raise HTTPException(409, 'This case is no longer actionable. Refresh reconciliation.')
    if case.kind not in ('delayed_booking', 'wallet_match', 'reinvestment_group', 'allocation_conflict', 'bank_match'):
        raise HTTPException(422, 'Correct the source history or conflicting links, then reconcile again. This issue cannot be dismissed.')
    event = find(db, InvestmentEvent, case.event_id)
    difference = ZERO
    if body.accept_difference:
        if case.kind not in ('delayed_booking', 'wallet_match') or not body.allocations:
            raise HTTPException(422, 'Small differences can only be accepted for reviewed wallet source matches.')
        selected = {a.source_id: a.amount for a in before['allocations'] if a.target_id == event.id}
        selected.update({a.source_id: a.amount for a in body.allocations})
        if set(selected) != {a.source_id for a in body.allocations}:
            raise HTTPException(422, 'Include every existing source connection when accepting a difference.')
        difference = sum(selected.values(), ZERO) - event.amount
        if not ZERO < abs(difference) <= DIFFERENCE_LIMIT:
            raise HTTPException(422, 'The accepted difference must be greater than zero and at most BDT 5.00.')
    if case.kind == 'bank_match':
        if not body.bank_links: raise HTTPException(422, 'Select a bank transfer.')
        if len({b.transaction_id for b in body.bank_links}) != len(body.bank_links):
            raise HTTPException(422, 'Select each bank transfer only once.')
        for choice in body.bank_links:
            transaction = find(db, Transaction, choice.transaction_id)
            if transaction.direction != ('debit' if event.kind == 'investment' else 'credit') or before['accounts'][transaction.account_id].currency != 'BDT':
                raise HTTPException(422, 'Select a BDT bank transfer with the correct direction.')
            if any(l.event_id == event.id and l.transaction_id == transaction.id for l in before['links']):
                raise HTTPException(422, 'This pair is already linked; remove it before replacing it.')
            db.add(InvestmentBankLink(event_id=event.id, transaction_id=transaction.id, amount=choice.amount, provenance='manual', explanation=body.explanation))
    else:
        if body.bank_links: raise HTTPException(422, 'Resolve bank matches separately.')
        existing = {a.source_id: a for a in before['allocations'] if a.target_id == event.id}
        if len({a.source_id for a in body.allocations}) != len(body.allocations): raise HTTPException(422, 'Select each source only once.')
        for choice in body.allocations:
            source = find(db, InvestmentEvent, choice.source_id)
            if source.institution_id != event.institution_id or source.kind not in ('repayment', 'refund') or source.posting_date > event.posting_date:
                raise HTTPException(422, 'Select an earlier credit from the same institution.')
            committed_elsewhere = sum((a.amount for a in before['allocations'] if a.source_id == source.id and a.target_id != event.id), ZERO)
            if committed_elsewhere + choice.amount > source.amount:
                raise HTTPException(422, 'The selected credit is already committed to another payment.')
            if choice.source_id in existing:
                if existing[choice.source_id].amount != choice.amount: raise HTTPException(422, 'Existing links were preserved. Remove an incorrect link before replacing it.')
            else: db.add(ReturnAllocation(source_id=source.id, target_id=event.id, amount=choice.amount, provenance='manual', explanation=body.explanation))
        grouped = {a.event_id: a.funding_id for a in before['associations']}
        for key in body.association_ids:
            child = find(db, InvestmentEvent, key)
            if event.kind != 'reinvestment' or child.institution_id != event.institution_id or child.kind not in ('investment', 'reinvestment_settlement'):
                raise HTTPException(422, 'Select settlement rows and a top-up for this reinvestment.')
            if key in grouped and grouped[key] != event.id: raise HTTPException(422, 'This row already belongs to another reinvestment.')
            if key not in grouped:
                db.add(ReinvestmentAssociation(funding_id=event.id, event_id=key, provenance='manual', explanation=body.explanation)); grouped[key] = event.id
    if body.delayed_booking and (event.kind != 'reinvestment' or not body.allocations):
        raise HTTPException(422, 'Delayed booking requires a reinvestment and the actual source credits.')
    case.resolutions = case.resolutions + [dict(body.model_dump(mode='json', exclude={'expected_revision'}), difference=cash(difference), reviewed_at=datetime.now(timezone.utc).isoformat(), evidence=case.details)]
    db.flush()
    checked = analyze(db)
    bad = [i for i in checked['issues'] if i['event_id'] == str(event.id) and (i['kind'].startswith('bank_') if case.kind == 'bank_match' else not i['kind'].startswith('bank_'))]
    if case.kind == 'reinvestment_group':
        # Resolve grouping first; attribution/delayed booking may then need a
        # separate decision. Do not require inventing a source to save a group.
        bad = [i for i in bad if i['kind'] in ('reinvestment_group', 'allocation_conflict', 'source_balance')]
    # A partial manual bank link is useful, but over-capacity is never allowed.
    if case.kind == 'bank_match': bad = [i for i in checked['issues'] if i['kind'] == 'bank_conflict']
    bad = [i for i in bad if i['kind'] != 'discrepancy_balance']
    if bad: raise HTTPException(422, bad[0]['message'])
    case.status, case.resolved_at = 'resolved', datetime.now(timezone.utc)
    find(db, Institution, event.institution_id).revision += 1
    result = reconcile(db)
    return result


@router.post('/reconciliation/cases/{identity}/preview')
def preview_resolution(identity: uuid.UUID, body: Resolution, db: DB):
    # Exercise the same validation and cascading matching as the final save.
    # Always roll back, including on validation failures; release the write lock.
    try:
        return apply_resolution(identity, body, db)
    finally:
        db.rollback()


@router.patch('/reconciliation/cases/{identity}')
def resolve(identity: uuid.UUID, body: Resolution, db: DB):
    result = apply_resolution(identity, body, db)
    db.commit()
    return result


@router.delete('/reconciliation/associations/{identity}', status_code=204)
def remove_association(identity: uuid.UUID, db: DB):
    lock(db)
    association = find(db, ReinvestmentAssociation, identity)
    event = find(db, InvestmentEvent, association.funding_id)
    find(db, Institution, event.institution_id).revision += 1
    db.delete(association); db.commit()


@router.get('/projects/{identity}/trail')
def trail(identity: uuid.UUID, db: DB):
    db.connection(execution_options={'isolation_level': 'REPEATABLE READ'})
    project = find(db, InvestmentProject, identity)
    context = analyze(db)
    own = {str(e.id) for e in context['events'] if e.project_id == project.id}
    allocations = [a for a in context['allocations'] if str(a.source_id) in own or str(a.target_id) in own]
    destinations = {a.target_id for a in allocations}
    # Show every contributor to these direct destinations. Do not follow those
    # contributors to their other destinations or the destination's later returns.
    allocations = [a for a in context['allocations'] if a.target_id in destinations]
    related = own | {str(a.target_id) for a in allocations} | {str(a.source_id) for a in allocations}
    # Include group members, but do not traverse the other business's later returns.
    associations = [a for a in context['associations'] if str(a.funding_id) in related]
    related.update(str(a.event_id) for a in associations)
    names = {str(p.id): p.name for p in db.scalars(select(InvestmentProject))}
    wallet = context['wallets'][str(project.institution_id)]
    rows = []
    for key in wallet['ordered_ids'] or sorted(related, key=lambda k: (context['by_id'][k].posting_date, k)):
        if key not in related: continue
        e = context['by_id'][key]
        source_links = [dict(import_id=str(s.import_id), filename=r.filename, ordinal=s.ordinal) for s, r in context['sources'] if str(s.event_id) == key]
        rows.append(dict(event_view(e), difference=context['differences'].get(key), project_name=names.get(str(e.project_id)), wallet=wallet['credits'].get(key), sources=source_links))
    bank_rows = []
    for link in context['links']:
        key = str(link.event_id)
        if key not in related: continue
        e, t = context['by_id'][key], context['bank'][str(link.transaction_id)]
        contribution = e.amount if key in own else sum((a.amount for a in allocations if str(a.target_id) == key and str(a.source_id) in own), ZERO)
        # Shared payouts cannot be apportioned among multiple bank receipts without
        # additional evidence. Expose bounds, never invent a proportional share.
        share = link.amount if contribution == e.amount or key in own else contribution if link.amount == e.amount else None
        if key in context['differences'] and key not in own:
            share = None  # Gross source contributions are not exact net bank shares.
        bank_rows.append(dict(id=str(link.id), event_id=key, transaction_id=str(t.id), amount=cash(t.amount), linked_amount=cash(link.amount),
            project_share=cash(share) if share is not None else None, payout_project_share=cash(contribution),
            posting_date=t.posting_date.isoformat(), business_date=bank_evidence(e, t)['business_date'], description=t.description,
            account=context['accounts'][t.account_id].label, provenance=link.provenance, explanation=link.explanation))
    return dict(project_id=str(project.id), project_name=project.name, revision=context['token'], events=rows,
        wallet_status=wallet['wallet_status'], bank_status='needs_review' if any(i['kind'].startswith('bank_') and i['event_id'] in related for i in context['issues']) else 'matched',
        allocations=[dict(id=str(a.id), source_id=str(a.source_id), target_id=str(a.target_id), amount=cash(a.amount), provenance=a.provenance, explanation=a.explanation) for a in allocations],
        associations=[dict(id=str(a.id), funding_id=str(a.funding_id), event_id=str(a.event_id), provenance=a.provenance) for a in associations], bank_links=bank_rows,
        differences=[d for key, d in context['differences'].items() if key in related],
        issues=[i for i in public_snapshot(context)['issues'] if i['institution_id'] == str(project.institution_id) and (not i['event_id'] or i['event_id'] in related)])
