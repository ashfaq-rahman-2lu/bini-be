"""Synthetic financial acceptance tests; no private CSVs or bank documents."""
import pytest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from types import SimpleNamespace as Row
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.db import SessionLocal
from app.main import app
from app.models import Account, Transaction
from app.investment_models import InvestmentImport, InvestmentSource, ReturnAllocation
from app.investment_reconciliation import combinations, ordered_events, wallet_replay
from test_investments import csv_fixture, upload, confirm, workspace


def run(client):
    response = client.post('/api/investments/reconciliation/run')
    assert response.status_code == 200, response.text
    return response.json()


def seed_transfers(entries):
    with SessionLocal.begin() as db:
        account = Account(bank='EBL', identifier='reconciliation-test', currency='BDT', label='Synthetic bank')
        db.add(account); db.flush()
        rows = []
        for when, direction, amount, description in entries:
            row = Transaction(account_id=account.id, posting_date=date.fromisoformat(when), direction=direction, amount=amount, balance='1000', description=description, normalized_description=description.casefold(), reference='')
            db.add(row); rows.append(row)
        db.flush()
        return [str(r.id) for r in rows]


def apon_csv():
    return csv_fixture(entries=[
        ('03-Sep-2025', 'Investment\nBANK_TRANSFER for Apon Bazaar [5]', '', '40000', '', '', '0', '40000'),
        ('16-Nov-2025', 'Repayment Received\npayment made by Apon Bazaar [5] to biniyog', '', '', '', '1080', '1080', '38920'),
        ('17-Nov-2025', 'Disbursed\ncashout', '1080', '', '', '', '0', '38920'),
        ('19-Jan-2026', 'Repayment Received\npayment made by Apon Bazaar [5] to biniyog', '', '', '', '1023', '1023', '37897'),
        ('21-Jan-2026', 'Disbursed\ncashout', '1023', '', '', '', '0', '37897'),
        ('01-Mar-2026', 'Repayment Received\npayment made by Other [1] to biniyog', '', '', '', '20187', '20187', '17710'),
        ('06-Mar-2026', 'Repayment Received\npayment made by Apon Bazaar [5] to biniyog', '', '', '', '8459', '28646', '9251'),
        ('07-Mar-2026', 'Repayment Received\npayment made by Apon Bazaar [5] to biniyog', '', '', '', '17051', '45697', '-7800'),
        ('11-Mar-2026', 'Repayment Received\npayment made by Apon Bazaar [5] to biniyog', '', '', '', '15366', '61063', '-23166'),
        ('16-Mar-2026', 'Disbursed\ncashout', '40876', '', '', '', '20187', '-23166'),
    ])


def seed_apon_bank():
    prefix = 'EFT INFLOW - THE CITY BANK LTD., CBL, Payment, BUSINESS DATE: '
    return seed_transfers([
        ('2025-09-02', 'debit', '40000', 'Other Bank Transfer SKB-BEFTN'),
        ('2025-11-17', 'credit', '1080', prefix + '17-11-25 //a'),
        ('2026-01-22', 'credit', '1023', prefix + '21-01-26 //b'),
        ('2026-03-16', 'credit', '40876', prefix + '16-03-26 //c'),
    ])


def test_apon_bank_dates_overlap_idempotence_and_direct_trail(client):
    seed_apon_bank(); confirm(client, upload(client, apon_csv()))
    first = run(client)
    assert first['saved']['allocations'] == 5 and first['saved']['bank_links'] == 4
    assert first['institutions'][0]['wallet_status'] == 'reconciled' and first['issues'] == []
    project = next(p for p in workspace(client)['projects'] if p['name'] == 'Apon Bazaar [5]')
    trail = client.get(f'/api/investments/projects/{project["id"]}/trail').json()
    assert len(trail['bank_links']) == 4
    jan = next(b for b in trail['bank_links'] if b['amount'] == '1023.00')
    assert jan['posting_date'] == '2026-01-22' and jan['business_date'] == '2026-01-21'
    march = next(e for e in trail['events'] if e['kind'] == 'cashout' and e['amount'] == '40876.00')
    assert sum(Decimal(a['amount']) for a in trail['allocations'] if a['target_id'] == march['id']) == Decimal('40876')
    assert all(e['project_name'] != 'Other [1]' for e in trail['events'])
    assert run(client)['saved'] == dict(allocations=0, bank_links=0, associations=0)
    assert run(client)['pending_allocations'] == []
    assert run(client)['revision'] == first['revision']
    confirm(client, upload(client, apon_csv().replace(b'Synthetic investor', b'Regenerated investor')))
    assert run(client)['saved']['allocations'] == 0 and len(workspace(client)['allocations']) == 5


def test_resolved_matches_group_saved_connections_and_include_automatic_cases(client):
    confirm(client, upload(client, apon_csv()))
    initial = run(client)
    assert len(initial['resolved']) == 3  # Three payouts, not five source allocations.
    assert len(initial['issues']) == 4
    seed_apon_bank()
    matched = run(client)
    assert len(matched['resolved']) == 7
    assert sum(row['kind'] == 'bank_match' for row in matched['resolved']) == 4
    payout = next(row for row in matched['resolved'] if row['kind'] == 'wallet_match' and row['amount'] == '40876.00')
    assert payout['connection_count'] == 3 and payout['provenance'] == 'automatic'
    assert all(row['status'] == 'resolved' for row in matched['resolved'])
    assert client.get('/api/investments/reconciliation').json()['resolved'] == matched['resolved']
    assert run(client)['resolved'] == matched['resolved']

    # Removing a saved match must not leave its old resolved case in this list.
    link = workspace(client)['bank_links'][0]
    assert client.delete(f'/api/investments/bank-links/{link["id"]}').status_code == 204
    after = client.get('/api/investments/reconciliation').json()
    assert not any(row['id'] == f'bank:{link["event_id"]}' for row in after['resolved'])
    assert len(after['resolved']) == 6
    assert client.get('/api/investments/reconciliation').json()['revision'] == after['revision']


def reinvestment_csv(delayed=False):
    entries = [
        ('01-Jul-2025', 'Investment\nBANK_TRANSFER for Alpha [1]', '', '1000', '', '', '0', '1000'),
        ('02-Jul-2025', 'Repayment Received\npayment made by Alpha [1] to biniyog', '', '', '', '400', '400', '600'),
    ]
    if delayed: entries += [('03-Jul-2025', 'Repayment Received\npayment made by Other [2] to biniyog', '', '', '', '200', '600', '400')]
    wallet, capital = (600, 400) if delayed else (400, 600)
    entries += [
        ('04-Jul-2025', 'Reinvestment\nREINVESTMENT for Beta [3]', '', '400', '', '', str(wallet), str(capital + 400)),
        ('04-Jul-2025', 'Investment\nBANK_TRANSFER for Beta [3]', '', '600', '', '', str(wallet), str(capital + 1000)),
        ('04-Jul-2025', 'Disbursed\nreinvestment', '150', '', '', '', str(wallet - 150), str(capital + 1000)),
        ('04-Jul-2025', 'Disbursed\nreinvestment', '250', '', '', '', str(wallet - 400), str(capital + 1000)),
    ]
    return csv_fixture(entries=entries)


def test_full_wallet_reinvestment_split_settlement_no_double_count(client):
    confirm(client, upload(client, reinvestment_csv()))
    result = run(client)
    assert result['saved']['allocations'] == 1 and result['saved']['associations'] == 3
    assert result['institutions'][0]['wallet_status'] == 'reconciled'
    assert result['institutions'][0]['closing'] == '0.00'
    assert next(iter(result['institutions'][0]['credits'].values())) == dict(available='0.00', reserved='0.00', consumed='400.00')
    beta = next(p for p in workspace(client)['projects'] if p['name'] == 'Beta [3]')
    assert beta['invested'] == '1000.00' and beta['reinvestment_funding'] == '400.00'
    assert run(client)['saved'] == dict(allocations=0, bank_links=0, associations=0)


def test_delayed_booking_review_stale_and_persisted_evidence(client):
    confirm(client, upload(client, reinvestment_csv(True)))
    result = run(client); issue = next(i for i in result['issues'] if i['kind'] == 'delayed_booking')
    assert result['saved']['allocations'] == 0
    data = workspace(client); source = next(e for e in data['events'] if e['kind'] == 'repayment' and e['amount'] == '400.00')
    body = dict(expected_revision=result['revision'], explanation='Instructed before the later installment arrived.', delayed_booking=True, instruction_date='2025-07-02', allocations=[dict(source_id=source['id'], amount='400')], association_ids=issue['association_ids'])
    assert client.patch(f'/api/investments/reconciliation/cases/{issue["id"]}', json=dict(body, expected_revision='old')).status_code == 409
    assert client.patch(f'/api/investments/reconciliation/cases/{issue["id"]}', json=dict(body, instruction_date='2025-07-01')).status_code == 422
    assert workspace(client)['allocations'] == []
    response = client.patch(f'/api/investments/reconciliation/cases/{issue["id"]}', json=body)
    assert response.status_code == 200, response.text
    after = response.json(); assert after['institutions'][0]['wallet_status'] == 'reconciled'
    later = next(e for e in data['events'] if e['kind'] == 'repayment' and e['amount'] == '200.00')
    assert after['institutions'][0]['credits'][later['id']]['available'] == '200.00'
    assert after['history'][0]['resolutions'][0]['evidence']['kind'] == 'delayed_booking'
    reviewed = [row for row in after['resolved'] if row['event_id'] == issue['event_id'] and row['kind'] == 'wallet_match']
    assert len(reviewed) == 1 and reviewed[0]['provenance'] == 'manual'
    assert reviewed[0]['resolutions'][0]['explanation'] == body['explanation']
    assert run(client)['saved']['allocations'] == 0


def competing_csv(full=False):
    return csv_fixture(entries=[
        ('01-Jul-2025', 'Investment\nBANK_TRANSFER for Alpha [1]', '', '1000', '', '', '0', '1000'),
        ('02-Jul-2025', 'Repayment Received\npayment made by Alpha [1] to biniyog', '', '', '', '100', '100', '900'),
        ('02-Jul-2025', 'Repayment Received\npayment made by Beta [2] to biniyog', '', '', '', '100', '200', '800'),
        ('03-Jul-2025', 'Disbursed\ncashout', '200' if full else '100', '', '', '', '0' if full else '100', '800'),
    ])


def test_competing_review_full_wallet_shared_bank_and_replace(client):
    confirm(client, upload(client, competing_csv()))
    result = run(client); case = next(i for i in result['issues'] if i['kind'] == 'wallet_match')
    assert result['saved']['allocations'] == 0 and len(case['candidates']) == 2
    response = client.patch(f'/api/investments/reconciliation/cases/{case["id"]}', json=dict(expected_revision=result['revision'], explanation='Provider identifies Alpha.', allocations=case['candidates'][0]['allocations']))
    assert response.status_code == 200, response.text
    assert response.json()['institutions'][0]['wallet_status'] == 'reconciled'
    confirm(client, upload(client, competing_csv(True), mode='replace'))
    assert client.get('/api/investments/reconciliation').json()['history'] == []
    seed_transfers([('2025-07-03', 'credit', '200', 'EFT INFLOW - THE CITY BANK LTD., CBL, Payment,')])
    assert run(client)['saved']['allocations'] == 2
    alpha = next(p for p in workspace(client)['projects'] if p['name'] == 'Alpha [1]')
    receipt = client.get(f'/api/investments/projects/{alpha["id"]}/trail').json()['bank_links'][0]
    assert receipt['amount'] == '200.00' and receipt['project_share'] == '100.00'
    shared = client.get(f'/api/investments/projects/{alpha["id"]}/trail').json()
    assert len(shared['allocations']) == 2


def test_conflicting_source_order_blocks_without_uuid_guess(client):
    confirm(client, upload(client, competing_csv(True)))
    with SessionLocal.begin() as db:
        sources = list(db.scalars(select(InvestmentSource).order_by(InvestmentSource.ordinal)))
        original = db.get(InvestmentImport, sources[0].import_id)
        report = InvestmentImport(institution_id=original.institution_id, fingerprint='x'*64, filename='conflicting.csv', original_csv=b'', parsed=original.parsed, status='confirmed', mode='extend')
        db.add(report); db.flush()
        for n, source in enumerate([sources[0], sources[2], sources[1], sources[3]], 1): db.add(InvestmentSource(import_id=report.id, ordinal=n, event_id=source.event_id))
    result = run(client)
    assert any(i['kind'] == 'source_order' for i in result['issues']) and not result['saved']['allocations']


def test_bank_competition_and_concurrent_runs(client):
    seed_transfers([('2025-06-30', 'debit', '1000', 'Other Bank Transfer SKB-BEFTN')])
    confirm(client, upload(client, competing_csv(True)))
    confirm(client, upload(client, competing_csv(True), name='Second'))
    def work(_):
        with TestClient(app, headers=client.headers, cookies=client.cookies) as c: return run(c)
    with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(work, range(2)))
    assert sum(r['saved']['allocations'] for r in results) == 4
    assert sum(r['saved']['bank_links'] for r in results) == 0
    assert len(workspace(client)['allocations']) == 4


def test_manual_link_revision_stales_review(client):
    confirm(client, upload(client, competing_csv()))
    result = run(client); case = next(i for i in result['issues'] if i['kind'] == 'wallet_match')
    choice = case['candidates'][0]['allocations'][0]
    assert client.post('/api/investments/allocations', json=dict(choice, target_id=case['event_id'])).status_code == 200
    assert client.patch(f'/api/investments/reconciliation/cases/{case["id"]}', json=dict(expected_revision=result['revision'], explanation='Old review', allocations=[choice])).status_code == 409
    run(client)
    with SessionLocal() as db: assert db.scalar(select(ReturnAllocation)).provenance == 'manual'


def test_unknown_opening_negative_and_bounded_search():
    event = Row(id='credit', project_id='one', posting_date=date(2025, 1, 1), kind='repayment', amount=Decimal('100'), ready_balance=Decimal('120'))
    result = wallet_replay([event], [['credit']], [], [], {})
    assert result['credits']['credit']['available'] is None and result['issues'][0]['kind'] == 'opening_funds'
    event.ready_balance = Decimal('-10')
    assert any(i['kind'] == 'source_balance' for i in wallet_replay([event], [['credit']], [], [], {})['issues'])
    assert not combinations({'a': Decimal('10'), 'b': Decimal('20')}, Decimal('30'), limit=1)[1]
    assert ordered_events([event, Row(id='other', posting_date=event.posting_date)], [['credit'], ['other']])[1]


def test_later_import_revalidates_earlier_automatic_bank_match(client):
    seed_transfers([('2025-06-30', 'debit', '1000', 'Other Bank Transfer SKB-BEFTN')])
    confirm(client, upload(client, competing_csv(True)))
    assert run(client)['saved']['bank_links'] == 1
    confirm(client, upload(client, competing_csv(True), name='Second'))
    after = run(client)
    assert any(i['kind'] == 'bank_conflict' for i in after['issues'])
    assert after['saved']['bank_links'] == 0 and len(workspace(client)['bank_links']) == 1


def test_bank_match_independent_of_wallet_and_wrong_patterns_not_auto_linked(client):
    seed_transfers([('2025-06-30', 'debit', '1000', 'Other Bank Transfer SKB-BEFTN'),
                    ('2025-07-03', 'credit', '100', 'Unrelated bank credit')])
    confirm(client, upload(client, competing_csv()))
    result = run(client)
    assert result['saved']['bank_links'] == 1 and result['saved']['allocations'] == 0
    assert any(i['kind'] == 'wallet_match' for i in result['issues'])
    assert any(i['kind'] == 'bank_match' for i in result['issues'])


def test_bank_partial_manual_capacity_and_repeated_resolution(client):
    bank_id = seed_transfers([('2025-07-03', 'credit', '200', 'Manual split transfer')])[0]
    confirm(client, upload(client, competing_csv(True)))
    result = run(client)
    case = next(i for i in result['issues'] if i['kind'] == 'bank_match' and workspace(client)['events'][-1]['id'] == i['event_id'])
    body = dict(expected_revision=result['revision'], explanation='Reviewed split transfer', bank_links=[dict(transaction_id=bank_id, amount='201')])
    response = client.patch(f'/api/investments/reconciliation/cases/{case["id"]}', json=body)
    assert response.status_code == 422 and workspace(client)['bank_links'] == []
    body['bank_links'][0]['amount'] = '100'
    response = client.patch(f'/api/investments/reconciliation/cases/{case["id"]}', json=body)
    assert response.status_code == 200, response.text
    assert client.patch(f'/api/investments/reconciliation/cases/{case["id"]}', json=body).status_code == 409
    alpha = next(p for p in workspace(client)['projects'] if p['name'] == 'Alpha [1]')
    trail = client.get(f'/api/investments/projects/{alpha["id"]}/trail').json()
    assert trail['bank_links'][0]['project_share'] is None  # mixed payout, partial bank receipt: no invented ratio


def test_incomplete_disjoint_same_day_history_and_late_credit_block_dependents(client):
    confirm(client, upload(client, reinvestment_csv(True)))
    result = run(client)
    assert any(i['kind'] == 'delayed_booking' for i in result['issues'])
    assert all(c['available'] is None for c in result['institutions'][0]['credits'].values())
    # GET endpoints are pure: cases are not rewritten and revision is stable.
    first = client.get('/api/investments/reconciliation').json()
    second = client.get('/api/investments/reconciliation').json()
    assert first == second


def test_automatic_matching_does_not_inherit_destination_returns(client):
    confirm(client, upload(client, reinvestment_csv()))
    run(client)
    data = workspace(client)
    alpha = next(p for p in data['projects'] if p['name'] == 'Alpha [1]')
    beta = next(p for p in data['projects'] if p['name'] == 'Beta [3]')
    # Extend with a destination installment in a later non-overlapping report.
    extra = csv_fixture(entries=[('05-Jul-2025', 'Repayment Received\npayment made by Beta [3] to biniyog', '', '', '', '50', '50', '1550')])
    confirm(client, upload(client, extra)); run(client)
    origin = client.get(f'/api/investments/projects/{alpha["id"]}/trail').json()
    destination = client.get(f'/api/investments/projects/{beta["id"]}/trail').json()
    assert not any(e['kind'] == 'repayment' and e['project_id'] == beta['id'] for e in origin['events'])
    assert any(e['kind'] == 'repayment' and e['project_id'] == beta['id'] for e in destination['events'])


def test_ambiguous_reinvestment_group_resolves_before_delayed_booking(client):
    entries = [
        ('01-Jul-2025', 'Repayment Received\npayment made by Alpha [1] to biniyog', '', '', '', '400', '400', '600'),
        ('02-Jul-2025', 'Repayment Received\npayment made by Other [2] to biniyog', '', '', '', '200', '600', '400'),
        ('03-Jul-2025', 'Reinvestment\nREINVESTMENT for Beta [3]', '', '400', '', '', '600', '800'),
        ('03-Jul-2025', 'Disbursed\nreinvestment', '400', '', '', '', '200', '800'),
        ('03-Jul-2025', 'Reinvestment\nREINVESTMENT for Gamma [4]', '', '200', '', '', '200', '1000'),
        ('03-Jul-2025', 'Disbursed\nreinvestment', '200', '', '', '', '0', '1000'),
    ]
    confirm(client, upload(client, csv_fixture(entries=entries)))
    result = run(client); case = next(i for i in result['issues'] if i['kind'] == 'reinvestment_group')
    settlement = next(e for e in workspace(client)['events'] if e['kind'] == 'reinvestment_settlement' and e['amount'] == '400.00')
    response = client.patch(f'/api/investments/reconciliation/cases/{case["id"]}', json=dict(expected_revision=result['revision'], explanation='First settlement belongs to Beta.', association_ids=[settlement['id']]))
    assert response.status_code == 200, response.text
    assert any(i['kind'] == 'delayed_booking' and i['event_id'] == case['event_id'] for i in response.json()['issues'])


def test_partial_manual_bank_pair_not_duplicated(client):
    bank_id = seed_transfers([('2025-06-30', 'debit', '1000', 'Other Bank Transfer SKB-BEFTN')])[0]
    confirm(client, upload(client, competing_csv(True)))
    event = next(e for e in workspace(client)['events'] if e['kind'] == 'investment')
    assert client.post('/api/investments/bank-links', json=dict(event_id=event['id'], transaction_id=bank_id, amount='500')).status_code == 200
    assert run(client)['saved']['bank_links'] == 0
    assert len(workspace(client)['bank_links']) == 1


def test_complete_project_requires_empty_wallet(client):
    confirm(client, upload(client, apon_csv()))
    project = next(p for p in workspace(client)['projects'] if p['name'] == 'Apon Bazaar [5]')
    url = f'/api/investments/projects/{project["id"]}'
    body = dict(status='closed', complete_history=True, confirm_wallet_empty=True)
    assert client.patch(url, json=body).status_code == 409
    assert next(p for p in workspace(client)['projects'] if p['id'] == project['id'])['status'] == 'active'
    run(client)
    assert client.patch(url, json=body).status_code == 200
    assert client.patch(url, json=body).status_code == 200
    assert next(p for p in workspace(client)['projects'] if p['id'] == project['id'])['final_gain'] == '2979.00'
    assert client.patch(url, json={'status': 'active'}).status_code == 200


def test_complete_project_rejects_remaining_wallet_credit(client):
    confirm(client, upload(client, reinvestment_csv(True)))
    run(client)
    project = next(p for p in workspace(client)['projects'] if p['name'] == 'Alpha [1]')
    response = client.patch(f'/api/investments/projects/{project["id"]}', json=dict(status='closed', complete_history=True, confirm_wallet_empty=True))
    assert response.status_code == 409


def difference_csv(difference='1.00'):
    """Two source businesses, later credits, and a 400 BDT settlement."""
    gap = Decimal(difference)
    second, ready, capital = Decimal('200') + gap, Decimal('600') + gap, Decimal('400') - gap
    return csv_fixture(entries=[
        ('01-Jul-2025', 'Investment\nBANK_TRANSFER for Alpha [1]', '', '1000', '', '', '0', '1000'),
        ('02-Jul-2025', 'Repayment Received\npayment made by Alpha [1] to biniyog', '', '', '', '200', '200', '800'),
        ('02-Jul-2025', 'Repayment Received\npayment made by Other [2] to biniyog', '', '', '', str(second), str(400 + gap), str(600 - gap)),
        ('03-Jul-2025', 'Repayment Received\npayment made by Later [4] to biniyog', '', '', '', '200', str(ready), str(capital)),
        ('04-Jul-2025', 'Reinvestment\nREINVESTMENT for Beta [3]', '', '400', '', '', str(ready), str(capital + 400)),
        ('04-Jul-2025', 'Investment\nBANK_TRANSFER for Beta [3]', '', '600', '', '', str(ready), str(capital + 1000)),
        ('04-Jul-2025', 'Disbursed\nreinvestment', '150', '', '', '', str(ready - 150), str(capital + 1000)),
        ('04-Jul-2025', 'Disbursed\nreinvestment', '250', '', '', '', str(ready - 400), str(capital + 1000)),
    ])


def difference_review(client, difference='1.00'):
    confirm(client, upload(client, difference_csv(difference)))
    result = run(client)
    issue = next(i for i in result['issues'] if i['kind'] == 'delayed_booking')
    sources = [e for e in workspace(client)['events'] if e['kind'] == 'repayment' and e['posting_date'] == '2025-07-02']
    body = dict(expected_revision=result['revision'], explanation='Small platform discrepancy; cause unknown.',
                delayed_booking=True, instruction_date='2025-07-02', accept_difference=True,
                allocations=[dict(source_id=e['id'], amount=e['amount']) for e in sources], association_ids=issue['association_ids'])
    return f'/api/investments/reconciliation/cases/{issue["id"]}', body


def test_difference_requires_consent_persists_without_phantom_money(client):
    url, body = difference_review(client)
    assert client.patch(url, json=dict(body, accept_difference=False)).status_code == 422
    assert workspace(client)['allocations'] == []
    response = client.patch(url, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['difference_total'] == '1.00'
    assert result['differences'][0]['amount'] == '1.00'
    assert result['institutions'][0]['closing'] == '201.00'
    assert result['institutions'][0]['wallet_status'] == 'reconciled'
    credits = result['institutions'][0]['credits']
    assert sum(Decimal(c['available']) for c in credits.values()) == Decimal('200')
    assert sum(Decimal(c['consumed']) for c in credits.values()) == Decimal('401')
    assert all(c['reserved'] == '0.00' for c in credits.values())
    assert client.patch(url, json=body).status_code == 409
    rerun = run(client)
    assert rerun['revision'] == result['revision']
    assert rerun['saved'] == dict(allocations=0, bank_links=0, associations=0)
    data = workspace(client)
    destination = next(e for e in data['events'] if e['kind'] == 'reinvestment')
    assert destination['amount'] == '400.00' and destination['allocated_in'] == '401.00'
    assert destination['difference']['amount'] == '1.00'
    for name in ('Alpha [1]', 'Other [2]', 'Beta [3]'):
        project = next(p for p in data['projects'] if p['name'] == name)
        trail = client.get(f'/api/investments/projects/{project["id"]}/trail').json()
        assert trail['differences'][0]['amount'] == '1.00'
    assert client.delete(f'/api/investments/allocations/{data["allocations"][0]["id"]}').status_code == 204
    after = run(client)
    assert after['differences'] == [] and after['institutions'][0]['wallet_status'] == 'needs_review'


@pytest.mark.parametrize('gap,accepted', [('5.00', True), ('5.01', False), ('-5.00', True), ('-5.01', False)])
def test_difference_limit_and_shortfall_does_not_make_unbacked_cash_available(client, gap, accepted):
    url, body = difference_review(client, gap)
    response = client.patch(url, json=body)
    assert response.status_code == (200 if accepted else 422), response.text
    if not accepted:
        assert workspace(client)['allocations'] == []
        assert client.get('/api/investments/reconciliation').json()['differences'] == []
    elif gap.startswith('-'):
        result = response.json()
        assert result['differences'][0]['amount'] == gap
        assert all(c['available'] is None for c in result['institutions'][0]['credits'].values())
        assert any(i['kind'] == 'discrepancy_balance' for i in result['issues'])


def test_reviewed_cashout_difference_keeps_bank_amount_and_unassigned_project_share(client):
    data = csv_fixture(entries=[
        ('01-Jul-2025', 'Repayment Received\npayment made by Alpha [1] to biniyog', '', '', '', '200', '200', '800'),
        ('01-Jul-2025', 'Repayment Received\npayment made by Other [2] to biniyog', '', '', '', '201', '401', '599'),
        ('02-Jul-2025', 'Disbursed\ncashout', '400', '', '', '', '1', '599'),
    ])
    confirm(client, upload(client, data))
    seed_transfers([('2025-07-02', 'credit', '400', 'EFT INFLOW - THE CITY BANK LTD., CBL, Payment,')])
    result = run(client); issue = next(i for i in result['issues'] if i['kind'] == 'wallet_match')
    response = client.patch(f'/api/investments/reconciliation/cases/{issue["id"]}', json=dict(
        expected_revision=result['revision'], explanation='Small unexplained difference in full-wallet payout.', accept_difference=True,
        allocations=issue['credit_candidates']))
    assert response.status_code == 200, response.text
    assert response.json()['institutions'][0]['wallet_status'] == 'reconciled'
    project = workspace(client)['projects'][0]
    trail = client.get(f'/api/investments/projects/{project["id"]}/trail').json()
    assert trail['bank_links'][0]['amount'] == '400.00'
    assert trail['bank_links'][0]['project_share'] is None
    assert all(c['available'] == '0.00' for c in response.json()['institutions'][0]['credits'].values())


def test_full_wallet_difference_needs_no_delayed_claim_and_preserves_extend_replace(client):
    contents = csv_fixture(entries=[
        ('01-Jul-2025', 'Repayment Received\npayment made by Alpha [1] to biniyog', '', '', '', '401', '401', '599'),
        ('02-Jul-2025', 'Reinvestment\nREINVESTMENT for Beta [3]', '', '400', '', '', '401', '999'),
        ('02-Jul-2025', 'Disbursed\nreinvestment', '400', '', '', '', '1', '999'),
    ])
    confirm(client, upload(client, contents)); result = run(client)
    issue = next(i for i in result['issues'] if i['kind'] == 'delayed_booking')
    response = client.patch(f'/api/investments/reconciliation/cases/{issue["id"]}', json=dict(
        expected_revision=result['revision'], explanation='Small platform discrepancy, not delayed booking.',
        accept_difference=True, allocations=issue['credit_candidates'], association_ids=issue['association_ids']))
    assert response.status_code == 200, response.text
    assert response.json()['institutions'][0]['wallet_status'] == 'reconciled'
    assert next(iter(response.json()['institutions'][0]['credits'].values()))['available'] == '0.00'
    confirm(client, upload(client, contents.replace(b'Synthetic investor', b'Regenerated investor')))
    assert run(client)['difference_total'] == '1.00'
    confirm(client, upload(client, contents, mode='replace'))
    assert client.get('/api/investments/reconciliation').json()['differences'] == []


def test_review_preview_rolls_back_every_change_and_matches_final_resolution(client):
    url, body = difference_review(client)
    before = client.get('/api/investments/reconciliation').json()
    original_workspace = workspace(client)
    for payload, expected in [(dict(body, accept_difference=False), 422), (dict(body, expected_revision='stale'), 409), (body, 200), (body, 200)]:
        response = client.post(url + '/preview', json=payload)
        assert response.status_code == expected, response.text
        assert client.get('/api/investments/reconciliation').json() == before
        assert workspace(client) == original_workspace
    preview = response.json()
    assert preview['institutions'][0]['wallet_status'] == 'reconciled'
    assert preview['difference_total'] == '1.00'
    assert preview['history'][0]['resolutions']
    # The original revision remains valid: preview did not save a review or links.
    saved = client.patch(url, json=body)
    assert saved.status_code == 200, saved.text
    result = saved.json()
    assert result['issues'] == preview['issues']
    assert result['differences'] == [{**d, 'reviewed_at': result['differences'][i]['reviewed_at']} for i, d in enumerate(preview['differences'])]
    assert {(r['event_id'], r['kind']) for r in result['resolved']} == {(r['event_id'], r['kind']) for r in preview['resolved']}
    assert client.post(url + '/preview', json=body).status_code == 409


def test_bank_review_preview_does_not_save_bank_links(client):
    confirm(client, upload(client, reinvestment_csv()))
    seed_transfers([('2025-07-01', 'debit', '1000', 'Other Bank Transfer SKB-BEFTN'), ('2025-07-01', 'debit', '1000', 'Other Bank Transfer SKB-BEFTN')])
    before = run(client)
    issue = next(i for i in before['issues'] if i['kind'] == 'bank_match' and len(i.get('candidates', [])) == 2)
    body = dict(expected_revision=before['revision'], explanation='Confirmed bank statement reference.', bank_links=[dict(transaction_id=issue['candidates'][0]['transaction_id'], amount='1000')])
    original = workspace(client)
    response = client.post(f'/api/investments/reconciliation/cases/{issue["id"]}/preview', json=body)
    assert response.status_code == 200, response.text
    assert any(r['event_id'] == issue['event_id'] and r['kind'] == 'bank_match' for r in response.json()['resolved'])
    assert workspace(client) == original
    assert client.get('/api/investments/reconciliation').json()['revision'] == before['revision']
