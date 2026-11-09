"""Small PostgreSQL-backed queue, processed inside the existing API service.

A worker holds the queue row lock until the operation and its result commit in
one transaction. A killed worker rolls back both; another worker can retry the
still-queued row. No lease expiry can start a second copy of a live operation.
"""
import asyncio
import hashlib
import json
import logging
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from .config import settings
from .db import SessionLocal
from .investment_models import InvestmentImport
from .investments import Confirm, DB, find
from .job_models import Job
from .models import Statement
from .performance import Timings, current_timings
from .request_logging import create_request_logger
from .schemas import Confirmation, ImportView

router = APIRouter(prefix='/api/jobs', tags=['operations'])
wakeup = threading.Event()


class Submission(BaseModel):
    kind: Literal['investment_confirm', 'bank_confirm', 'reconcile']
    target_id: uuid.UUID | None = None
    parameters: dict = Field(default_factory=dict)


def job_view(job, *, include_result=False):
    return dict(id=str(job.id), kind=job.kind, target_id=str(job.target_id) if job.target_id else None,
        status=job.status, attempts=job.attempts, created_at=job.created_at.isoformat(),
        result=job.result if include_result and job.status == 'succeeded' else None,
        error=dict(status=job.error_status, detail=job.error_detail) if job.status == 'failed' else None)


@router.post('', status_code=202)
def submit(body: Submission, db: DB):
    if not settings.jobs_enabled:
        raise HTTPException(503, 'Background operations are disabled on this service.')
    if body.kind == 'reconcile':
        if body.target_id is not None or body.parameters:
            raise HTTPException(422, 'Reconciliation does not accept an import or parameters.')
        payload = {}
    else:
        if body.target_id is None:
            raise HTTPException(422, 'Select an import to confirm.')
        model, schema = (InvestmentImport, Confirm) if body.kind == 'investment_confirm' else (Statement, Confirmation)
        find(db, model, body.target_id)
        try:
            payload = schema.model_validate(body.parameters).model_dump(mode='json')
        except ValueError:
            raise HTTPException(422, 'Supply the current review revision and confirmation acknowledgments.') from None
    active_key = f'{body.kind}:{body.target_id or "workspace"}'
    digest = hashlib.sha256(json.dumps([body.kind, str(body.target_id), payload], sort_keys=True).encode()).hexdigest()
    # Reconcile is repeatable over new data, so only concurrent runs coalesce.
    dedupe_key = digest if body.kind != 'reconcile' else uuid.uuid4().hex
    for _ in range(3):
        job = db.scalar(select(Job).where(or_(Job.dedupe_key == dedupe_key, Job.active_key == active_key)))
        if job:
            return job_view(job)
        identity = db.scalar(insert(Job).values(id=uuid.uuid4(), kind=body.kind, target_id=body.target_id,
            payload=payload, status='queued', attempts=0, active_key=active_key, dedupe_key=dedupe_key)
            .on_conflict_do_nothing().returning(Job.id))
        if identity:
            db.commit()
            wakeup.set()
            return job_view(db.get(Job, identity))
        job = db.scalar(select(Job).where(or_(Job.dedupe_key == dedupe_key, Job.active_key == active_key)))
        if job:
            return job_view(job)
        # A finishing worker may have cleared active_key between the statements.
    raise HTTPException(409, 'Another operation just finished. Refresh and try again.')


@router.get('')
def active(db: DB, kind: Literal['investment_confirm', 'bank_confirm', 'reconcile'], target_id: uuid.UUID | None = None):
    # Also return the latest completed job so a refresh can recover its outcome.
    job = db.scalar(select(Job).where(Job.kind == kind, Job.target_id == target_id)
        .order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
    wakeup.set()
    return job_view(job) if job else None


@router.get('/{identity}')
def status(identity: uuid.UUID, db: DB):
    wakeup.set()
    return job_view(find(db, Job, identity), include_result=True)


@router.post('/{identity}/retry', status_code=202)
def retry(identity: uuid.UUID, db: DB):
    # Do not wait on a live job's lock. Only terminal unexpected failures can be
    # retried; stale revisions/invalid consent require a refreshed submission.
    job = db.scalar(select(Job).where(Job.id == identity).with_for_update(skip_locked=True))
    if job is None:
        raise HTTPException(409, 'Operation is still being processed or no longer exists.')
    if job.status != 'failed' or job.error_status < 500:
        raise HTTPException(409, 'Refresh the import review before submitting a new confirmation.')
    active_key = f'{job.kind}:{job.target_id or "workspace"}'
    if db.scalar(select(Job.id).where(Job.active_key == active_key)):
        raise HTTPException(409, 'Another operation is already queued.')
    job.status, job.active_key, job.attempts = 'queued', active_key, 0
    job.error_status = job.error_detail = job.completed_at = None
    job.available_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'Another operation is already queued.') from None
    wakeup.set()
    return job_view(job)


def execute_job(job, db):
    if job.kind == 'investment_confirm':
        from .investments import confirm_import
        return confirm_import(job.target_id, Confirm.model_validate(job.payload), db, commit=False)
    if job.kind == 'bank_confirm':
        from .main import confirm_import
        result = confirm_import(job.target_id, Confirmation.model_validate(job.payload), db, commit=False)
        # Match the synchronous endpoint's response model, including exact money
        # strings. FastAPI's generic JSON encoder converts Decimal to floats.
        return ImportView.model_validate(result).model_dump(mode='json')
    from .investments import lock
    from .reconciliation_api import reconcile
    lock(db)
    return reconcile(db)


def process_one(logger):
    stats = Timings()
    token = current_timings.set(stats)
    started = perf_counter()
    job = None
    try:
        with SessionLocal() as db:
            job = db.scalar(select(Job).where(Job.status == 'queued', Job.available_at <= datetime.now(timezone.utc))
                .order_by(Job.created_at, Job.id).with_for_update(skip_locked=True).limit(1))
            if job is None:
                return False
            queue_ms = max(0, (datetime.now(timezone.utc) - job.created_at).total_seconds() * 1000)
            job.attempts += 1
            error_type = None
            try:
                # Roll back every domain write on failure while retaining the
                # outer queue-row lock and retry counter.
                with db.begin_nested():
                    job.result = jsonable_encoder(execute_job(job, db))
                    if job.kind in {'investment_confirm', 'bank_confirm'}:
                        # Persist the follow-up with the confirmation itself.
                        # It runs in its own transaction even if the browser
                        # closes before receiving the confirmation result.
                        followup = Job(id=uuid.uuid4(), kind='reconcile', payload={},
                            dedupe_key=hashlib.sha256(f'{job.id}:reconcile'.encode()).hexdigest(),
                            active_key=None, status='queued', attempts=0,
                            created_at=datetime.now(timezone.utc), available_at=datetime.now(timezone.utc))
                        db.add(followup)
                        job.result = dict(job.result, reconciliation_job_id=str(followup.id))
                    job.status = 'succeeded'
                    job.active_key = None
                    job.completed_at = datetime.now(timezone.utc)
                    db.flush()
            except HTTPException as error:
                error_type = type(error).__name__
                job.status, job.error_status = 'failed', error.status_code
                job.error_detail = str(error.detail)[:1000]
                job.active_key = None
                job.completed_at = datetime.now(timezone.utc)
            except Exception as error:
                error_type = type(error).__name__
                # Never log DB parameters, exception messages, or import data.
                if job.attempts >= 3:
                    job.status, job.error_status = 'failed', 500
                    job.error_detail = 'Operation could not finish. Retry the saved operation.'
                    job.active_key = None
                    job.completed_at = datetime.now(timezone.utc)
                else:
                    job.available_at = datetime.now(timezone.utc) + timedelta(seconds=job.attempts * 5)
            db.commit()
            try:
                logger.log(logging.ERROR if job.status == 'failed' else logging.WARNING if error_type else logging.INFO,
                    'Background operation completed', extra={
                        'service': 'bini-api', 'environment': settings.log_environment,
                        'job': dict(id=str(job.id), kind=job.kind, status=job.status, attempts=job.attempts,
                                    duration_ms=round((perf_counter() - started) * 1000, 3),
                                    queue_ms=round(queue_ms, 3), error_type=error_type),
                        'performance': stats.snapshot(),
                    })
            except Exception:
                pass
            return True
    finally:
        current_timings.reset(token)


def worker(stop, logger):
    # Startup and authenticated status requests recover abandoned queued work.
    # When no jobs remain, sleep without polling/holding a DB connection.
    last_cleanup = None
    while not stop.is_set():
        wakeup.clear()
        delay = 5
        try:
            while not stop.is_set() and process_one(logger):
                pass
            with SessionLocal() as db:
                now = datetime.now(timezone.utc)
                if last_cleanup != now.date():
                    db.execute(delete(Job).where(Job.status != 'queued', Job.completed_at < now - timedelta(days=7)))
                    db.commit()
                    last_cleanup = now.date()
                next_due = db.scalar(select(func.min(Job.available_at)).where(Job.status == 'queued'))
            delay = max(5, (next_due - now).total_seconds()) if next_due else None
        except Exception:
            logger.warning('Background queue temporarily unavailable')
        wakeup.wait(delay)


@asynccontextmanager
async def lifespan(app):
    stop = threading.Event()
    thread = None
    if settings.jobs_enabled:
        thread = threading.Thread(target=worker, args=(stop, create_request_logger()), daemon=True, name='bini-operations')
        thread.start()
    try:
        yield
    finally:
        stop.set(); wakeup.set()
        if thread:
            # A killed process rolls back the open transaction for recovery.
            await asyncio.to_thread(thread.join, 5)
