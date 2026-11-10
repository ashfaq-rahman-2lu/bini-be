"""Performance and queue regressions against the dedicated synthetic database."""
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select

from app import jobs
from app.config import settings
from app.db import SessionLocal, engine
from app.investment_models import Institution, InvestmentEvent, InvestmentImport, InvestmentProject, InvestmentSource
from app.job_models import Job
from app.models import SourceLink, Statement, Transaction
from test_investments import csv_fixture, upload, confirm

logger = logging.Logger('test.operations')


@pytest.fixture
def queue(client, monkeypatch):
    # Existing API client was started with the worker disabled. Process jobs
    # deterministically unless a test explicitly starts worker threads.
    monkeypatch.setattr(settings, 'jobs_enabled', True)
    return client


def submit(client, draft, **overrides):
    data = dict(kind='investment_confirm', target_id=draft['id'], parameters=dict(
        expected_revision=draft['revision'], acknowledge_warnings=True, acknowledge_replace=draft['mode'] == 'replace'))
    data.update(overrides)
    result = client.post('/api/jobs', json=data)
    assert result.status_code == 202, result.text
    return result.json()


@pytest.mark.parametrize('row_count', [2, 250])
def test_investment_confirmation_round_trips_do_not_grow_per_row(client, row_count):
    entries = [('02-Jul-2025', 'Investment\nBANK_TRANSFER for Alpha [1]', '', '1', '', '', '0', str(i + 1)) for i in range(row_count)]
    draft = upload(client, csv_fixture(entries=entries))
    calls = []
    def record(conn, cursor, statement, parameters, context, executemany): calls.append(statement)
    event.listen(engine, 'before_cursor_execute', record)
    try:
        confirm(client, draft)
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    assert len(calls) <= 14, f'{row_count} rows used {len(calls)} calls'
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(InvestmentEvent)) == row_count
        assert db.scalar(select(func.count()).select_from(InvestmentSource)) == row_count


def test_reconcile_loads_once_and_returns_the_same_fresh_snapshot(client):
    draft = upload(client); confirm(client, draft)
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('SELECT'): statements.append(statement)
    event.listen(engine, 'before_cursor_execute', record)
    try:
        response = client.post('/api/investments/reconciliation/run')
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    assert response.status_code == 200
    # Seven joined datasets plus the advisory lock; no reload after writes.
    assert len(statements) == 8
    assert all('investment_imports.parsed' not in sql and 'original_csv' not in sql for sql in statements)
    result = response.json(); result.pop('saved')
    assert result == client.get('/api/investments/reconciliation').json()


def test_queue_is_durable_idempotent_and_protected(queue):
    draft = upload(queue)
    first = submit(queue, draft)
    second = submit(queue, draft)
    assert first['id'] == second['id'] and first['status'] == 'queued'
    with SessionLocal() as db:
        assert db.get(InvestmentImport, uuid.UUID(draft['id'])).status == 'draft'
    # A new worker/session can claim persisted work after the submitter is gone.
    assert jobs.process_one(logger)
    result = queue.get('/api/jobs/' + first['id']).json()
    assert result['status'] == 'succeeded' and result['result']['status'] == 'confirmed'
    assert submit(queue, draft)['id'] == first['id']
    assert jobs.process_one(logger)
    assert queue.get('/api/jobs/' + result['result']['reconciliation_job_id']).json()['status'] == 'succeeded'
    assert not jobs.process_one(logger)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(InvestmentEvent)) == 7
        assert db.scalar(select(func.count()).select_from(InvestmentSource)) == 7
    queue.post('/api/auth/logout')
    assert queue.get('/api/jobs/' + first['id']).status_code == 401
    assert queue.post('/api/jobs', json={'kind': 'reconcile'}).status_code == 401


def test_failed_source_insert_rolls_back_every_financial_write(queue, monkeypatch):
    draft = upload(queue); queued = submit(queue, draft)
    def fail_links(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO investment_sources'):
            raise RuntimeError('private SQL financial payload must not escape')
    event.listen(engine, 'before_cursor_execute', fail_links)
    try:
        for attempt in range(3):
            if attempt:
                with SessionLocal.begin() as db:
                    db.get(Job, uuid.UUID(queued['id'])).available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            assert jobs.process_one(logger)
            with SessionLocal() as db:
                assert db.scalar(select(func.count()).select_from(InvestmentEvent)) == 0
                assert db.scalar(select(func.count()).select_from(InvestmentProject)) == 0
                assert db.scalar(select(func.count()).select_from(InvestmentSource)) == 0
                assert db.get(InvestmentImport, uuid.UUID(draft['id'])).status == 'draft'
                assert db.scalar(select(Institution.revision)) == draft['revision']
    finally:
        event.remove(engine, 'before_cursor_execute', fail_links)
    failed = queue.get('/api/jobs/' + queued['id']).json()
    assert failed['status'] == 'failed' and failed['attempts'] == 3
    assert 'private' not in str(failed)
    assert queue.post('/api/jobs/' + queued['id'] + '/retry').status_code == 202
    assert jobs.process_one(logger)
    assert queue.get('/api/jobs/' + queued['id']).json()['result']['status'] == 'confirmed'


def test_stale_revision_fails_without_retrying_or_partial_writes(queue):
    draft = upload(queue)
    queued = submit(queue, draft, parameters={'expected_revision': 42, 'acknowledge_warnings': True})
    assert jobs.process_one(logger)
    result = queue.get('/api/jobs/' + queued['id']).json()
    assert result['status'] == 'failed' and result['error']['status'] == 409 and result['attempts'] == 1
    assert queue.post('/api/jobs/' + queued['id'] + '/retry').status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(InvestmentEvent)) == 0
    correct = submit(queue, draft)
    assert correct['id'] != queued['id']
    assert jobs.process_one(logger)


def test_two_workers_do_not_execute_the_same_job(queue, monkeypatch):
    draft = upload(queue); queued = submit(queue, draft)
    entered, release = threading.Event(), threading.Event()
    original = jobs.execute_job
    calls = []
    def slow(job, db):
        calls.append(job.id); entered.set()
        assert release.wait(5)
        return original(job, db)
    monkeypatch.setattr(jobs, 'execute_job', slow)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(jobs.process_one, logger)
        try:
            assert entered.wait(5)
            assert not jobs.process_one(logger)
            # A live operation's dedupe lookup must return without waiting for
            # the worker's row lock or exposing its uncommitted results.
            assert submit(queue, draft)['id'] == queued['id']
            state = queue.get('/api/jobs/' + queued['id']).json()
            assert state['status'] == 'queued' and state['result'] is None
        finally:
            release.set()
        assert future.result(5)
    assert len(calls) == 1


def test_crashed_worker_transaction_can_be_recovered(queue, monkeypatch):
    draft = upload(queue); queued = submit(queue, draft)
    original = jobs.execute_job
    class ProcessKilled(BaseException): pass
    def killed(job, db):
        original(job, db)
        raise ProcessKilled()
    monkeypatch.setattr(jobs, 'execute_job', killed)
    with pytest.raises(ProcessKilled): jobs.process_one(logger)
    with SessionLocal() as db:
        assert db.get(Job, uuid.UUID(queued['id'])).status == 'queued'
        assert db.scalar(select(func.count()).select_from(InvestmentEvent)) == 0
    monkeypatch.setattr(jobs, 'execute_job', original)
    assert jobs.process_one(logger)
    assert queue.get('/api/jobs/' + queued['id']).json()['status'] == 'succeeded'


def test_bank_confirmation_and_reconciliation_jobs(queue, pdf_factory):
    draft = queue.post('/api/imports', files={'file': ('synthetic.pdf', pdf_factory(), 'application/pdf')}, data={'password': 'test-only-password'}).json()
    job = queue.post('/api/jobs', json={'kind': 'bank_confirm', 'target_id': draft['id'], 'parameters': {'expected_revision': draft['revision']}}).json()
    assert jobs.process_one(logger)
    result = queue.get('/api/jobs/' + job['id']).json()['result']
    assert result['status'] == 'confirmed'
    assert isinstance(result['opening_balance'], str)
    assert all(isinstance(r['amount'], str) and isinstance(r['balance'], str) for r in result['rows'])
    assert jobs.process_one(logger)
    assert queue.get('/api/jobs/' + result['reconciliation_job_id']).json()['status'] == 'succeeded'
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Transaction)) == 2
        assert db.scalar(select(func.count()).select_from(SourceLink)) == 2
    first = queue.post('/api/jobs', json={'kind': 'reconcile'}).json()
    second = queue.post('/api/jobs', json={'kind': 'reconcile'}).json()
    assert first['id'] == second['id']
    assert jobs.process_one(logger)
    result = queue.get('/api/jobs/' + first['id']).json()
    assert result['status'] == 'succeeded' and result['result']['saved'] == dict(allocations=0, bank_links=0, associations=0)
    assert queue.post('/api/jobs', json={'kind': 'reconcile'}).json()['id'] != first['id']
