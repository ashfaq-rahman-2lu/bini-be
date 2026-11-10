"""Financial and round-trip regressions for the latency refactor."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import event

from app.db import engine
from test_reconciliation import difference_review
from test_investments import upload, confirm


@contextmanager
def sql_calls():
    calls = []
    def record(conn, cursor, statement, parameters, context, executemany):
        calls.append(statement)
    event.listen(engine, 'before_cursor_execute', record)
    try:
        yield calls
    finally:
        event.remove(engine, 'before_cursor_execute', record)


def test_preview_is_read_only_and_save_loads_snapshot_once(client):
    url, body = difference_review(client)
    before = client.get('/api/investments/reconciliation').json()
    for payload, expected in [(body, 200), ({**body, 'expected_revision': 'stale'}, 409),
                              ({**body, 'accept_difference': False}, 422)]:
        with sql_calls() as calls:
            preview = client.post(url + '/preview', json=payload)
        assert preview.status_code == expected, preview.text
        assert len(calls) == 7
        assert all(sql.startswith('SELECT') and 'pg_advisory' not in sql for sql in calls)
        assert client.get('/api/investments/reconciliation').json() == before
    with sql_calls() as calls:
        saved = client.patch(url, json=body)
    assert saved.status_code == 200, saved.text
    assert sum('FROM investment_institutions LEFT OUTER JOIN investment_events' in sql for sql in calls) == 1
    assert len(calls) <= 16


def test_read_query_budgets_keep_empty_and_populated_workspaces(client):
    for populated in (False, True):
        if populated:
            confirm(client, upload(client))
        for path, expected in [('/api/investments', 6), ('/api/investments/reconciliation', 7)]:
            with sql_calls() as calls:
                response = client.get(path)
            assert response.status_code == 200, response.text
            assert len(calls) == expected
        projects = client.get('/api/investments').json()['projects']
        if projects:
            with sql_calls() as calls:
                response = client.get('/api/investments/projects/' + projects[0]['id'] + '/trail')
            assert response.status_code == 200
            assert len(calls) == 8


def test_simultaneous_review_saves_reject_stale_second_decision(client):
    url, body = difference_review(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.patch(url, json=body), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]


def test_job_submit_and_status_avoid_extra_reads(client, monkeypatch):
    from test_operations import submit, logger
    from app import jobs
    from app.config import settings
    monkeypatch.setattr(settings, 'jobs_enabled', True)
    draft = upload(client)
    checkouts = []
    def checkout(*args):
        checkouts.append(1)
    event.listen(engine, 'checkout', checkout)
    try:
        with sql_calls() as calls:
            job = submit(client, draft)
    finally:
        event.remove(engine, 'checkout', checkout)
    assert len(calls) == 3
    assert len(checkouts) == 1
    with sql_calls() as calls:
        response = client.get('/api/jobs/' + job['id'])
    assert response.status_code == 200 and response.json()['result'] is None
    assert len(calls) == 1 and 'operation_jobs.payload' not in calls[0]
    assert jobs.process_one(logger)
    with sql_calls() as calls:
        response = client.get('/api/jobs/' + job['id'])
    assert len(calls) == 1
    assert response.json()['result']['status'] == 'confirmed'


def test_joined_snapshot_preserves_institutions_and_accounts_without_rows(client):
    from app.db import SessionLocal
    from app.investment_models import Institution
    from app.models import Account
    from app.reconciliation_api import load_analysis
    with SessionLocal.begin() as db:
        db.add(Institution(name='empty institution', revision=0))
        db.add(Account(bank='EBL', identifier='empty-account', currency='BDT', label='Empty account', revision=0))
    with SessionLocal() as db:
        data = load_analysis(db)
        assert len(data['owners']) == len(data['accounts']) == 1
        assert data['events'] == data['bank'] == []
    assert client.get('/api/investments/reconciliation').json()['institutions'][0]['name'] == 'empty institution'
    assert client.get('/api/investments').json()['institutions'][0]['wallet_balance'] is None
