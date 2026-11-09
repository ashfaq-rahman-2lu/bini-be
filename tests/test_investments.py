import csv
import io
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.investment_csv import COLUMNS, CSVError, amount, parse_investment_csv
from app.investment_models import InvestmentBankLink, InvestmentEvent, InvestmentImport, InvestmentSource, ReturnAllocation
from app.main import app
from app.models import Account, Transaction


def csv_fixture(*, prefix='Synthetic investor', entries=None):
    entries = entries or [
        ('02-Jul-2025', 'Investment\nBANK_TRANSFER for Alpha [1]', '', '1000', '', '', '0', '1000'),
        ('05-Jul-2025', 'Repayment Received\npayment made by Alpha [1] to biniyog', '', '', '', '600', '600', '400'),
        ('06-Jul-2025', 'Reinvestment\nREINVESTMENT for Beta [2]', '', '400', '', '', '600', '800'),
        ('06-Jul-2025', 'Disbursed\nreinvestment', '400', '', '', '', '200', '800'),
        ('07-Jul-2025', 'Disbursed\ncashout', '200', '', '', '', '0', '800'),
        ('10-Jul-2025', 'Repayment Received\nwith 50 profit, payment made by Alpha [1] to biniyog', '', '', '', '500', '500', '300'),
        ('11-Jul-2025', 'Disbursed\ncashout', '500', '', '', '', '0', '300'),
    ]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([prefix]); writer.writerow(['Transactions']); writer.writerow(COLUMNS)
    rows = [[d, t, w, i, f or '0', r, b, '0', '0', c] for d, t, w, i, f, r, b, c in entries]
    writer.writerows(rows)
    totals = [str(sum(Decimal(row[n].replace(',', '') or '0') for row in rows)) for n in range(2, 10)]
    totals[4], totals[7] = rows[-1][6], rows[-1][9]
    writer.writerow(['', 'TOTAL', *totals])
    return output.getvalue().encode()


def upload(client, data=None, name='Biniyog', mode='extend'):
    response = client.post('/api/investments/imports', files={'file': ('synthetic.csv', data or csv_fixture(), 'text/csv')}, data={'institution': name, 'mode': mode})
    assert response.status_code == 200, response.text
    return response.json()


def confirm(client, draft):
    response = client.post(f'/api/investments/imports/{draft["id"]}/confirm', json={'expected_revision': draft['revision'], 'acknowledge_warnings': True, 'acknowledge_replace': draft.get('mode') == 'replace'})
    assert response.status_code == 200, response.text
    return response.json()


def workspace(client):
    response = client.get('/api/investments')
    assert response.status_code == 200, response.text
    return response.json()


def seed_bank():
    with SessionLocal.begin() as db:
        account = Account(bank='EBL', identifier='test-investment-bank', currency='BDT', label='Synthetic bank')
        db.add(account); db.flush()
        rows = [Transaction(account_id=account.id, posting_date=date(2025, 6, 30), direction='debit', amount='1000', balance='0', description='Transfer to institution', normalized_description='transfer', reference='A'),
                Transaction(account_id=account.id, posting_date=date(2025, 7, 9), direction='credit', amount='200', balance='200', description='Institution payout', normalized_description='payout', reference='B')]
        db.add_all(rows); db.flush()
        return [str(r.id) for r in rows]


def test_review_is_required_idempotent_overlap_and_sources(client):
    draft = upload(client)
    assert draft['new_count'] == 7
    assert workspace(client)['events'] == []
    assert client.post(f'/api/investments/imports/{draft["id"]}/confirm', json={'expected_revision': 0}).status_code == 422
    confirm(client, draft); confirm(client, draft)
    assert upload(client)['id'] == draft['id']
    overlap = upload(client, csv_fixture(prefix='Regenerated report'))
    assert overlap['new_count'] == 0 and overlap['linked_count'] == 7
    confirm(client, overlap)
    data = workspace(client)
    assert len(data['events']) == 7 and len(data['projects']) == 2
    assert all(len(e['sources']) == 2 for e in data['events'])
    alpha = next(p for p in data['projects'] if p['name'] == 'Alpha [1]')
    assert alpha['invested'] == '1000.00' and alpha['returned'] == '1100.00'
    assert alpha['unclassified_returns'] == '1100.00' and alpha['classified_profit'] == '0.00'
    assert alpha['principal_outstanding'] is None and alpha['final_gain'] is None
    assert data['institutions'][0]['wallet_balance'] == '0.00'


def replacement_csv():
    return csv_fixture(entries=[
        ('02-Jul-2025', 'Investment\nBANK_TRANSFER for Alpha [1]', '', '1000', '', '', '0', '1000'),
        ('12-Jul-2025', 'Repayment Received\npayment made by Alpha [1] to biniyog', '', '', '', '250', '250', '750'),
    ])


def extended_csv():
    records = list(csv.reader(io.StringIO(csv_fixture(prefix='Extended report').decode())))
    records.insert(-1, ['12-Jul-2025', 'Investment\nBANK_TRANSFER for Gamma [3]', '', '100', '0', '', '0', '0', '0', '400'])
    records[-1][3] = '1500'
    records[-1][9] = '400'
    output = io.StringIO(); csv.writer(output).writerows(records)
    return output.getvalue().encode()


def test_replace_is_previewed_acknowledged_and_scoped_to_one_institution(client):
    bank_ids = seed_bank(); original = upload(client); confirm(client, original)
    data = workspace(client)
    funding = next(e for e in data['events'] if e['kind'] == 'investment')
    returned = next(e for e in data['events'] if e['kind'] == 'repayment')
    reinvested = next(e for e in data['events'] if e['kind'] == 'reinvestment')
    assert client.patch(f'/api/investments/events/{returned["id"]}/split', json={'principal': '550'}).status_code == 200
    assert client.post('/api/investments/allocations', json={'source_id': returned['id'], 'target_id': reinvested['id'], 'amount': '400'}).status_code == 200
    assert client.post('/api/investments/bank-links', json={'event_id': funding['id'], 'transaction_id': bank_ids[0], 'amount': '1000'}).status_code == 200
    assert client.patch(f'/api/investments/projects/{funding["project_id"]}', json={'status': 'closed', 'expected_total': '1200', 'complete_history': True}).status_code == 200
    other = upload(client, name='Other'); confirm(client, other)
    pending = upload(client, csv_fixture(prefix='Pending old report'))
    before = workspace(client)
    owner = next(i for i in before['institutions'] if i['name'] == 'biniyog')
    other_events = [e for e in before['events'] if e['institution_id'] != owner['id']]
    draft = upload(client, replacement_csv(), mode='replace')
    assert draft['mode'] == 'replace' and draft['new_count'] == 2 and draft['linked_count'] == 0
    assert draft['replacement'] == dict(events=7, projects=2, imports=2, principal_splits=1, allocations=1, bank_links=1, reconciliation_cases=0, reinvestment_associations=0)
    assert workspace(client) == before
    denied = client.post(f'/api/investments/imports/{draft["id"]}/confirm', json={'expected_revision': draft['revision'], 'acknowledge_warnings': True})
    assert denied.status_code == 422 and 'Acknowledge replacement' in denied.json()['detail']
    assert workspace(client) == before
    confirm(client, draft)
    after = workspace(client)
    own_events = [e for e in after['events'] if e['institution_id'] == owner['id']]
    assert len(own_events) == 2 and all(e['principal'] is None for e in own_events)
    assert all(e['id'] not in {old['id'] for old in before['events']} for e in own_events)
    assert [e for e in after['events'] if e['institution_id'] != owner['id']] == other_events
    own_projects = [p for p in after['projects'] if p['institution_id'] == owner['id']]
    assert len(own_projects) == 1 and own_projects[0]['status'] == 'active' and own_projects[0]['expected_total'] is None
    assert after['allocations'] == [] and after['bank_links'] == []
    assert next(i for i in after['institutions'] if i['id'] == owner['id'])['wallet_balance'] == '250.00'
    assert client.get(f'/api/investments/imports/{original["id"]}').status_code == 404
    assert client.get(f'/api/investments/imports/{pending["id"]}').status_code == 404
    with SessionLocal() as db:
        assert {str(t.id) for t in db.scalars(select(Transaction))} == set(bank_ids)
    confirm(client, draft)
    assert workspace(client) == after


def test_extend_preserves_existing_edits_and_reuses_overlap(client):
    bank_ids = seed_bank(); confirm(client, upload(client)); data = workspace(client)
    returned = next(e for e in data['events'] if e['kind'] == 'repayment')
    funding = next(e for e in data['events'] if e['kind'] == 'investment')
    target = next(e for e in data['events'] if e['kind'] == 'reinvestment')
    assert client.patch(f'/api/investments/events/{returned["id"]}/split', json={'principal': '550'}).status_code == 200
    assert client.post('/api/investments/allocations', json={'source_id': returned['id'], 'target_id': target['id'], 'amount': '400'}).status_code == 200
    assert client.post('/api/investments/bank-links', json={'event_id': funding['id'], 'transaction_id': bank_ids[0], 'amount': '1000'}).status_code == 200
    before = workspace(client)
    draft = upload(client, extended_csv(), mode='extend')
    assert draft['mode'] == 'extend' and draft['replacement'] is None and draft['linked_count'] == 7
    assert draft['new_count'] == 1
    confirm(client, draft); after = workspace(client)
    assert len(after['events']) == 8
    assert {e['id'] for e in before['events']}.issubset(e['id'] for e in after['events'])
    assert next(e for e in after['events'] if e['id'] == returned['id'])['principal'] == '550.00'
    assert after['allocations'] == before['allocations'] and after['bank_links'] == before['bank_links']


def test_same_file_can_be_replaced_again_later_and_cancelled_without_changes(client):
    first = upload(client); confirm(client, first)
    replace = upload(client, mode='replace')
    assert replace['id'] != first['id'] and replace['status'] == 'draft'
    assert upload(client, mode='replace')['id'] == replace['id']
    confirm(client, replace)
    confirm(client, upload(client, csv_fixture(prefix='Later extension')))
    before = workspace(client)
    repeated = upload(client, mode='replace')
    assert repeated['id'] != replace['id'] and repeated['status'] == 'draft'
    assert client.delete(f'/api/investments/imports/{repeated["id"]}').status_code == 204
    assert workspace(client) == before
    fresh = upload(client, mode='replace'); confirm(client, fresh)
    assert {e['id'] for e in workspace(client)['events']}.isdisjoint(e['id'] for e in before['events'])
    assert len(client.get('/api/investments/imports').json()) == 1


def test_stale_and_invalid_replacement_cannot_change_the_ledger(client):
    confirm(client, upload(client)); before = workspace(client)
    invalid = client.post('/api/investments/imports', files={'file': ('bad.csv', b'wrong headers')}, data={'mode': 'replace'})
    assert invalid.status_code == 422 and workspace(client) == before
    invalid_mode = client.post('/api/investments/imports', files={'file': ('valid.csv', csv_fixture())}, data={'mode': 'erase'})
    assert invalid_mode.status_code == 422 and workspace(client) == before
    replacement = upload(client, replacement_csv(), mode='replace')
    confirm(client, upload(client, csv_fixture(prefix='Another update')))
    before = workspace(client)
    stale = client.post(f'/api/investments/imports/{replacement["id"]}/confirm', json={'expected_revision': replacement['revision'], 'acknowledge_warnings': True, 'acknowledge_replace': True})
    assert stale.status_code == 409 and workspace(client) == before
    refreshed = client.get(f'/api/investments/imports/{replacement["id"]}').json()
    confirm(client, refreshed)
    assert len(workspace(client)['events']) == 2


def test_replacement_rolls_back_removed_data_if_later_work_fails(client, monkeypatch):
    import app.investments as service
    confirm(client, upload(client)); replacement = upload(client, replacement_csv(), mode='replace')
    before = workspace(client)
    def fail_preview(*args): raise RuntimeError('Synthetic failure after rebuilding the ledger')
    with monkeypatch.context() as patch:
        patch.setattr(service, 'preview', fail_preview)
        with TestClient(app, raise_server_exceptions=False) as failing:
            response = failing.post(f'/api/investments/imports/{replacement["id"]}/confirm', json={'expected_revision': replacement['revision'], 'acknowledge_warnings': True, 'acknowledge_replace': True})
    assert response.status_code == 500 and workspace(client) == before
    assert client.get(f'/api/investments/imports/{replacement["id"]}').json()['status'] == 'draft'


def test_concurrent_repeated_replacement_confirmation_is_idempotent(client):
    confirm(client, upload(client)); replacement = upload(client, replacement_csv(), mode='replace')
    def commit(_):
        with TestClient(app) as instance:
            return instance.post(f'/api/investments/imports/{replacement["id"]}/confirm', json={'expected_revision': replacement['revision'], 'acknowledge_warnings': True, 'acknowledge_replace': True}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(commit, range(2))) == [200, 200]
    assert len(workspace(client)['events']) == 2


def test_extend_and_replace_confirmations_cannot_commit_from_the_same_stale_review(client):
    confirm(client, upload(client))
    extended = upload(client, csv_fixture(prefix='Competing extension'))
    replacement = upload(client, replacement_csv(), mode='replace')
    def commit(draft):
        with TestClient(app) as instance:
            return instance.post(f'/api/investments/imports/{draft["id"]}/confirm', json={'expected_revision': draft['revision'], 'acknowledge_warnings': True, 'acknowledge_replace': True}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(commit, [extended, replacement]))
    assert statuses.count(200) == 1 and any(s in (404, 409) for s in statuses)
    assert len(workspace(client)['events']) in (2, 7)


def test_stale_preview_cancel_and_invalid_file_do_not_touch_ledger(client):
    first, second = upload(client), upload(client, csv_fixture(prefix='Another report'))
    confirm(client, first)
    response = client.post(f'/api/investments/imports/{second["id"]}/confirm', json={'expected_revision': 0, 'acknowledge_warnings': True})
    assert response.status_code == 409
    assert len(workspace(client)['events']) == 7
    assert client.delete(f'/api/investments/imports/{first["id"]}').status_code == 409
    assert client.delete(f'/api/investments/imports/{second["id"]}').status_code == 204
    assert client.get(f'/api/investments/imports/{second["id"]}').status_code == 404
    assert client.post('/api/investments/imports', files={'file': ('bad.csv', b'wrong header')}).status_code == 422
    assert len(workspace(client)['events']) == 7


def test_profit_splits_closed_gain_and_loss(client):
    confirm(client, upload(client))
    data = workspace(client); alpha = next(p for p in data['projects'] if p['name'] == 'Alpha [1]')
    repayments = [e for e in data['events'] if e['kind'] == 'repayment']
    for e, principal in zip(repayments, ['550.00', '450.00']):
        assert client.patch(f'/api/investments/events/{e["id"]}/split', json={'principal': principal}).status_code == 200
    p = next(p for p in workspace(client)['projects'] if p['id'] == alpha['id'])
    assert (p['classified_profit'], p['principal_outstanding'], p['unclassified_returns']) == ('100.00', '0.00', '0.00')
    assert client.patch(f'/api/investments/projects/{p["id"]}', json={'status': 'closed'}).status_code == 422
    assert client.patch(f'/api/investments/projects/{p["id"]}', json={'status': 'closed', 'expected_total': '1100.00', 'complete_history': True}).status_code == 200
    p = next(p for p in workspace(client)['projects'] if p['id'] == alpha['id'])
    assert p['final_gain'] == '100.00' and p['roi_percent'] == '10.00' and p['expected_remaining'] == '0.00'
    assert client.patch(f'/api/investments/events/{repayments[0]["id"]}/split', json={'principal': '0'}).status_code == 409
    beta = next(p for p in data['projects'] if p['name'] == 'Beta [2]')
    assert client.patch(f'/api/investments/projects/{beta["id"]}', json={'status': 'closed', 'complete_history': True}).status_code == 200
    closed = next(p for p in workspace(client)['projects'] if p['id'] == beta['id'])
    assert closed['final_gain'] == '-400.00' and closed['principal_outstanding'] == '0.00' and closed['capital_written_off'] == '400.00'
    assert client.patch(f'/api/investments/projects/{alpha["id"]}', json={'status': 'active'}).status_code == 200
    assert client.patch(f'/api/investments/events/{repayments[1]["id"]}/split', json={'principal': '500'}).status_code == 422
    for invalid in ['-1', '600.001', 'NaN', '100000000000000000']:
        assert client.patch(f'/api/investments/events/{repayments[0]["id"]}/split', json={'principal': invalid}).status_code == 422


def test_trace_return_to_reinvestment_and_bank_payout_without_double_counting(client):
    confirm(client, upload(client)); data = workspace(client)
    returned = next(e for e in data['events'] if e['kind'] == 'repayment')
    reinvested = next(e for e in data['events'] if e['kind'] == 'reinvestment')
    cashout = next(e for e in data['events'] if e['kind'] == 'cashout')
    ids = []
    for target, value in [(reinvested, '400'), (cashout, '200')]:
        response = client.post('/api/investments/allocations', json={'source_id': returned['id'], 'target_id': target['id'], 'amount': value})
        assert response.status_code == 200, response.text
        ids.append(response.json()['id'])
    alpha = next(p for p in workspace(client)['projects'] if p['name'] == 'Alpha [1]')
    assert alpha['traced_to_reinvestment'] == '400.00' and alpha['traced_to_bank_payout'] == '200.00'
    assert alpha['untraced_returns'] == '500.00'
    assert client.post('/api/investments/allocations', json={'source_id': returned['id'], 'target_id': cashout['id'], 'amount': '1'}).status_code == 409
    later = [e for e in data['events'] if e['kind'] == 'repayment'][-1]
    assert client.post('/api/investments/allocations', json={'source_id': later['id'], 'target_id': cashout['id'], 'amount': '1'}).status_code == 422
    settlement = next(e for e in data['events'] if e['kind'] == 'reinvestment_settlement')
    assert client.post('/api/investments/allocations', json={'source_id': returned['id'], 'target_id': settlement['id'], 'amount': '1'}).status_code == 422
    assert client.delete('/api/investments/allocations/' + ids[0]).status_code == 204
    assert next(e for e in workspace(client)['events'] if e['id'] == returned['id'])['allocated_out'] == '200.00'


def test_bank_matching_date_lag_partial_and_combined_links(client):
    bank_ids = seed_bank(); confirm(client, upload(client)); data = workspace(client)
    external = next(e for e in data['events'] if e['kind'] == 'investment')
    cashout = next(e for e in data['events'] if e['kind'] == 'cashout')
    candidates = client.get(f'/api/investments/events/{external["id"]}/bank-candidates').json()
    assert candidates['items'][0]['id'] == bank_ids[0] and candidates['items'][0]['days_difference'] == 2
    assert candidates['items'][0]['exact_amount'] is True
    assert client.get(f'/api/investments/events/{external["id"]}/bank-candidates?days=0').json()['items'] == []
    assert client.get(f'/api/investments/events/{cashout["id"]}/bank-candidates').json()['items'][0]['id'] == bank_ids[1]
    response = client.post('/api/investments/bank-links', json={'event_id': external['id'], 'transaction_id': bank_ids[0], 'amount': '600'})
    assert response.status_code == 200, response.text
    assert workspace(client)['bank_links'][0]['amount'] == '600.00'
    assert client.post('/api/investments/bank-links', json={'event_id': external['id'], 'transaction_id': bank_ids[1], 'amount': '1'}).status_code == 422
    assert client.post('/api/investments/bank-links', json={'event_id': cashout['id'], 'transaction_id': bank_ids[1], 'amount': '201'}).status_code == 422
    assert client.delete('/api/investments/bank-links/' + response.json()['id']).status_code == 204
    assert workspace(client)['bank_links'] == []
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Transaction)) == 2


def test_concurrent_confirmation_and_bank_capacity(client):
    bank_ids = seed_bank(); draft = upload(client)
    def commit(_):
        with TestClient(app) as c:
            return c.post(f'/api/investments/imports/{draft["id"]}/confirm', json={'expected_revision': 0, 'acknowledge_warnings': True}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(commit, range(2))) == [200, 200]
    assert len(workspace(client)['events']) == 7
    # Two institutions can share a combined bank transfer, but cannot over-allocate it.
    confirm(client, upload(client, name='Other institution'))
    events = [e for e in workspace(client)['events'] if e['kind'] == 'investment']
    def link(e):
        with TestClient(app) as c:
            return c.post('/api/investments/bank-links', json={'event_id': e['id'], 'transaction_id': bank_ids[0], 'amount': '700'}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(link, events)) == [200, 422]
    assert len(workspace(client)['bank_links']) == 1


def test_different_campaigns_and_institutions_stay_separate(client):
    confirm(client, upload(client))
    confirm(client, upload(client, name='Other'))
    assert len(workspace(client)['projects']) == 4 and len(workspace(client)['events']) == 14
    data = workspace(client)
    source = next(e for e in data['events'] if e['kind'] == 'repayment')
    target = next(e for e in data['events'] if e['institution_id'] != source['institution_id'] and e['kind'] == 'cashout')
    assert client.post('/api/investments/allocations', json={'source_id': source['id'], 'target_id': target['id'], 'amount': '1'}).status_code == 422


def test_repeated_same_day_installments_keep_their_distinct_balances(client):
    entries = [('02-Jul-2025', 'Investment\nBANK_TRANSFER for Alpha [1]', '', '1000', '', '', '0', '1000')]
    entries += [('05-Jul-2025', 'Repayment Received\npayment made by Alpha [1] to biniyog', '', '', '', '1', str(n), str(1000 - n)) for n in range(1, 70)]
    data = csv_fixture(entries=entries)
    confirm(client, upload(client, data))
    assert len(workspace(client)['events']) == 70
    regenerated = data.replace(b'Synthetic investor', b'Regenerated export')
    draft = upload(client, regenerated)
    assert draft['new_count'] == 0 and draft['linked_count'] == 70
    confirm(client, draft)
    assert len(workspace(client)['events']) == 70
    assert all(len(e['sources']) == 2 for e in workspace(client)['events'])


def test_concurrent_money_traces_cannot_overspend_an_installment(client):
    confirm(client, upload(client))
    data = workspace(client)
    source = next(e for e in data['events'] if e['kind'] == 'repayment')
    targets = [next(e for e in data['events'] if e['kind'] == 'reinvestment'), next(e for e in reversed(data['events']) if e['kind'] == 'cashout')]
    def allocate(target):
        with TestClient(app) as c:
            return c.post('/api/investments/allocations', json={'source_id': source['id'], 'target_id': target['id'], 'amount': target['amount']}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(allocate, targets)) == [200, 422]
    assert len(workspace(client)['allocations']) == 1


@pytest.mark.parametrize('edit', [
    lambda s: s.replace('1000', '1000.001'),
    lambda s: s.replace('BANK_TRANSFER for Alpha [1]', 'missing identity'),
    lambda s: s.replace('Disbursed\nreinvestment', 'Disbursed\ncashout'),
    lambda s: s.replace('Repayment Received', 'Unknown event'),
    lambda s: s.replace(',200,0,0,800', ',199,0,0,800'),
])
def test_parser_rejects_invalid_or_unreconciled_records(edit):
    with pytest.raises(CSVError): parse_investment_csv(edit(csv_fixture().decode()).encode())


def formula_csv():
    records = list(csv.reader(io.StringIO(csv_fixture().decode())))
    records.insert(0, [''])  # Header offsets and multiline descriptions use CSV record numbers.
    first = records.index(COLUMNS) + 2
    for index in range(first - 1, len(records) - 1):
        row = index + 1
        records[index][6] = f'=SUM($F${first}:F{row})-SUM($C${first}:C{row})'
    records[-1][6] = f'=G{len(records) - 1}'
    output = io.StringIO(); csv.writer(output).writerows(records)
    return output.getvalue().encode()


def test_formula_import_matches_numeric_records_and_keeps_source_evidence(client):
    numeric = parse_investment_csv(csv_fixture())
    formulas = parse_investment_csv(formula_csv())
    assert [r['signature'] for r in formulas['rows']] == [r['signature'] for r in numeric['rows']]
    assert formulas['closing_wallet'] == '0.00'
    assert formulas['rows'][1]['raw'][COLUMNS[6]] == '=SUM($F$5:F6)-SUM($C$5:C6)'
    confirm(client, upload(client, formula_csv()))
    overlap = upload(client)
    assert overlap['new_count'] == 0 and overlap['linked_count'] == 7
    confirm(client, overlap)
    assert len(workspace(client)['events']) == 7


@pytest.mark.parametrize('value,expected', [('1,38,920', '138920.00'), ('12,34,567.89', '1234567.89'),
                                          ('1,234,567.89', '1234567.89'), ('-1,38,920.50', '-138920.50')])
def test_indian_and_western_money_grouping(value, expected):
    assert str(amount(value, signed=True)) == expected


@pytest.mark.parametrize('value', ['1,234,56', '123,45,678', '1,38,920.001', '=1+2', 'NaN'])
def test_invalid_money_grouping_precision_and_formulas(value):
    with pytest.raises(CSVError): amount(value)


def test_indian_grouping_and_padded_summary_controls(client):
    records = list(csv.reader(io.StringIO(csv_fixture(entries=[
        ('02-Jul-2025', 'Investment\nBANK_TRANSFER for Alpha [1]', '', '138920', '', '', '0', '138920'),
    ]).decode())))
    records.insert(2, ['Total Deposited:', '1,38,920', *([''] * 8)])
    records[-2][3] = records[-2][9] = records[-1][3] = records[-1][9] = '1,38,920'
    output = io.StringIO(); csv.writer(output).writerows(records)
    data = output.getvalue().encode()
    parsed = parse_investment_csv(data)
    assert parsed['summary']['Total Deposited'] == '138920.00'
    assert parsed['rows'][0]['amount'] == '138920.00'
    confirm(client, upload(client, data))
    broken = data.replace(b'Total Deposited:,"1,38,920"', b'Total Deposited:,"1,38,919"')
    response = client.post('/api/investments/imports', files={'file': ('bad.csv', broken)})
    assert response.status_code == 422 and 'summary does not reconcile' in response.json()['detail']
    assert len(workspace(client)['events']) == 1


@pytest.mark.parametrize('bad', ['=SUM($F$4:F5)-SUM($C$4:C5)', '=SUM($F$5:F6)-SUM($C$5:C6)',
                                 '=G5', '=1+1', '=HYPERLINK("https://example.com")'])
def test_balance_formula_references_are_validated(bad):
    records = list(csv.reader(io.StringIO(formula_csv().decode())))
    records[4][6] = bad
    output = io.StringIO(); csv.writer(output).writerows(records)
    with pytest.raises(CSVError, match='G5: Unsupported ready-balance formula'):
        parse_investment_csv(output.getvalue().encode())


def test_formula_import_still_blocks_wrong_totals_and_capital():
    data = formula_csv()
    records = list(csv.reader(io.StringIO(data.decode())))
    records[-1][5] = '1099'
    output = io.StringIO(); csv.writer(output).writerows(records)
    with pytest.raises(CSVError, match='transaction rows sum to 1100.00, but TOTAL says 1099.00'):
        parse_investment_csv(output.getvalue().encode())
    records[-1][5] = '1100'; records[5][9] = '401'
    output = io.StringIO(); csv.writer(output).writerows(records)
    with pytest.raises(CSVError, match='expected 400.00, found 401.00'):
        parse_investment_csv(output.getvalue().encode())
    records[5][9] = '400'; records[-1][6] = '=G10'
    output = io.StringIO(); csv.writer(output).writerows(records)
    with pytest.raises(CSVError, match='G12: Unsupported ready-balance formula'):
        parse_investment_csv(output.getvalue().encode())


def repayment_formula_csv(*, gap=False):
    records = list(csv.reader(io.StringIO(csv_fixture().decode())))
    records.insert(2, ['Total Repaid (from campaigns):', '', *([''] * 8)])
    first = records.index(COLUMNS) + 2
    last = len(records) - 1
    if gap: records.insert(-1, [])
    records[2][1] = f'=F{len(records)}'
    records[-1][5] = f'=SUM(F{first}:F{last})'
    output = io.StringIO(); csv.writer(output).writerows(records)
    return output.getvalue().encode()


def test_repayment_total_and_linked_summary_formulas_import_and_preserve_duplicates(client):
    data = repayment_formula_csv(gap=True)
    parsed = parse_investment_csv(data)
    assert parsed['totals'][COLUMNS[5]] == parsed['summary']['Total Repaid (from campaigns)'] == '1100.00'
    confirm(client, upload(client, data))
    draft = upload(client)
    assert draft['new_count'] == 0 and draft['linked_count'] == 7
    confirm(client, draft)
    assert len(workspace(client)['events']) == 7


@pytest.mark.parametrize('old,new,cell', [
    ('=F12', '=F11', 'B3'),
    ('=F12', '=SUM(F5:F11)', 'B3'),
    ('=SUM(F5:F11)', '=SUM(F5:F12)', 'F12'),
    ('=SUM(F5:F11)', '=SUM(F6:F11)', 'F12'),
    ('=SUM(F5:F11)', '=1+1099', 'F12'),
])
def test_repayment_control_formulas_reject_wrong_references_and_arbitrary_expressions(old, new, cell):
    data = repayment_formula_csv().replace(old.encode(), new.encode())
    with pytest.raises(CSVError, match=f'{cell}: Unsupported repayment-total formula'):
        parse_investment_csv(data)


def capital_formula_csv(*, opening='0', gap=False, entries=None):
    records = list(csv.reader(io.StringIO(csv_fixture(entries=entries).decode())))
    first = records.index(COLUMNS) + 2
    last = len(records) - 1
    suffix = ('+' if Decimal(opening) > 0 else '') + opening if Decimal(opening) else ''
    for index in range(first - 1, last):
        row = index + 1
        records[index][9] = f'=SUM($D${first}:D{row})-SUM($E${first}:E{row})-SUM($F${first}:F{row}){suffix}'
    if gap: records.insert(-1, [])
    records[-1][9] = f'=J{last}'
    output = io.StringIO(); csv.writer(output).writerows(records)
    return output.getvalue().encode()


def test_capital_formula_import_preserves_numeric_duplicate_identity_and_source_text(client):
    data = capital_formula_csv(gap=True)
    parsed = parse_investment_csv(data)
    assert parsed['totals'][COLUMNS[9]] == '300.00'
    assert parsed['rows'][1]['raw'][COLUMNS[9]] == '=SUM($D$4:D5)-SUM($E$4:E5)-SUM($F$4:F5)'
    assert [r['signature'] for r in parsed['rows']] == [r['signature'] for r in parse_investment_csv(csv_fixture())['rows']]
    confirm(client, upload(client, data))
    draft = upload(client)
    assert draft['new_count'] == 0 and draft['linked_count'] == 7
    confirm(client, draft)
    assert len(workspace(client)['events']) == 7


@pytest.mark.parametrize('opening', ['250.25', '-500.50'])
def test_capital_formula_preserves_partial_export_opening_balances(opening):
    parsed = parse_investment_csv(capital_formula_csv(opening=opening))
    assert Decimal(parsed['rows'][0]['reported_capital']) == Decimal('1000') + Decimal(opening)
    assert Decimal(parsed['totals'][COLUMNS[9]]) == Decimal('300') + Decimal(opening)
    assert any('existing business capital' in warning for warning in parsed['warnings'])


def test_capital_formula_subtracts_refunds():
    parsed = parse_investment_csv(capital_formula_csv(entries=[
        ('02-Jul-2025', 'Investment\nBANK_TRANSFER for Alpha [1]', '', '100', '', '', '0', '100'),
        ('03-Jul-2025', 'Investment Refund\nRefund for Alpha [1]', '', '', '20', '', '20', '80'),
    ]))
    assert parsed['rows'][-1]['reported_capital'] == parsed['totals'][COLUMNS[9]] == '80.00'


@pytest.mark.parametrize('bad', ['=J4', '=1+999', '=SUM($D$4:D5)-SUM($E$4:E5)-SUM($F$4:F5)',
                                 '=SUM($D$4:D4)-SUM($E$4:E4)-SUM($F$4:F4)+1.001',
                                 '=SUM($D$4:D4)-SUM($E$4:E4)-SUM($F$4:F4)+10000000000000000'])
def test_capital_formula_rejects_unsafe_expressions_and_invalid_references(bad):
    records = list(csv.reader(io.StringIO(capital_formula_csv().decode())))
    records[3][9] = bad
    output = io.StringIO(); csv.writer(output).writerows(records)
    with pytest.raises(CSVError, match='J4:'):
        parse_investment_csv(output.getvalue().encode())


def test_capital_formula_still_rejects_changed_opening_and_wrong_total_reference():
    data = capital_formula_csv().replace(b'=SUM($D$4:D5)-SUM($E$4:E5)-SUM($F$4:F5)',
                                        b'=SUM($D$4:D5)-SUM($E$4:E5)-SUM($F$4:F5)+1')
    with pytest.raises(CSVError, match='business-capital running balance does not reconcile'):
        parse_investment_csv(data)
    with pytest.raises(CSVError, match='J11: Unsupported capital-total formula'):
        parse_investment_csv(capital_formula_csv().replace(b'=J10', b'=J9'))


def balance_summary_csv(*, gap=False):
    records = list(csv.reader(io.StringIO(csv_fixture(entries=[
        ('02-Jul-2025', 'Investment\nBANK_TRANSFER for Alpha [1]', '', '1000', '', '', '0', '1000'),
        ('03-Jul-2025', 'Repayment Received\npayment made by Alpha [1] to biniyog', '', '', '', '100', '100', '900'),
    ]).decode())))
    records[2:2] = [['Ready Balance:', ''], ['Total Currently Invested:', '', *([''] * 8)]]
    if gap: records.insert(-1, [])
    total = len(records)
    records[2][1] = f'=G{total}'
    records[3][1] = f'=J{total}+G{total}'
    output = io.StringIO(); csv.writer(output).writerows(records)
    return output.getvalue().encode()


def test_summary_balance_formulas_resolve_total_capital_plus_wallet_and_import_safely(client):
    data = balance_summary_csv(gap=True)
    parsed = parse_investment_csv(data)
    assert parsed['summary']['Ready Balance'] == '100.00'
    assert parsed['summary']['Total Currently Invested'] == '1000.00'
    assert not any('Total Currently Invested' in warning for warning in parsed['warnings'])
    confirm(client, upload(client, data))
    numeric = data.replace(b'=G9', b'100').replace(b'=J9+G9', b'1000')
    draft = upload(client, numeric)
    assert draft['new_count'] == 0 and draft['linked_count'] == 2
    confirm(client, draft)
    assert len(workspace(client)['events']) == 2


@pytest.mark.parametrize('old,new,cell', [
    ('=G8', '=G7', 'B3'),
    ('=G8', '=1+99', 'B3'),
    ('=J8+G8', '=J8', 'B4'),
    ('=J8+G8', '=J7+G8', 'B4'),
    ('=J8+G8', '=J8+G7', 'B4'),
])
def test_summary_balance_formulas_validate_both_references_and_reject_expressions(old, new, cell):
    with pytest.raises(CSVError, match=f'{cell}: Unsupported summary formula'):
        parse_investment_csv(balance_summary_csv().replace(old.encode(), new.encode()))


def test_numeric_currently_invested_warning_compares_capital_plus_wallet():
    data = balance_summary_csv().replace(b'=G8', b'100').replace(b'=J8+G8', b'1000')
    parsed = parse_investment_csv(data)
    assert not any('Total Currently Invested' in warning for warning in parsed['warnings'])
    mismatched = data.replace(b'Total Currently Invested:,1000', b'Total Currently Invested:,900')
    assert any('capital with business plus ready balance' in warning for warning in parse_investment_csv(mismatched)['warnings'])


def test_private_sample_controls_and_financial_meanings(client):
    sample = Path(os.environ.get('SAMPLE_DIR', Path(__file__).resolve().parents[2] / 'sample')) / 'transactions_2026-11-02.csv'
    if not os.environ.get('RUN_PRIVATE_SAMPLES') or not sample.is_file(): pytest.skip('Private CSV opt-in required.')
    parsed = parse_investment_csv(sample.read_bytes())
    assert len(parsed['rows']) == 70 and parsed['closing_wallet'] == '1718.00'
    assert len({r['project'] for r in parsed['rows'] if r['project']}) == 10
    confirm(client, upload(client, sample.read_bytes()))
    data = workspace(client)
    assert sum(Decimal(p['external_funding']) for p in data['projects']) == Decimal('450392')
    assert sum(Decimal(p['reinvestment_funding']) for p in data['projects']) == Decimal('34608')
    assert sum(Decimal(e['amount']) for e in data['events'] if e['kind'] == 'cashout') == Decimal('218035')
    assert sum(Decimal(p['returned']) for p in data['projects']) == Decimal('254361')
    assert all(p['final_gain'] is None and p['principal_outstanding'] is None for p in data['projects'] if p['returned'] != '0.00')
