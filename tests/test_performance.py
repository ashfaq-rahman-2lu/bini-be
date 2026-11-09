import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import SessionLocal
from app.performance import current_timings, phase
from app.request_logging import RequestJSONFormatter, RequestTimingMiddleware


def test_sql_counts_phases_and_context_isolation_exclude_parameters():
    output = io.StringIO()
    logger = logging.Logger('test.timings')
    handler = logging.StreamHandler(output); handler.setFormatter(RequestJSONFormatter()); logger.addHandler(handler)
    app = FastAPI()
    @app.get('/timed/{identity}')
    def timed(identity: str):
        with SessionLocal() as db:
            with phase('test_load'):
                db.execute(text('SELECT :private'), {'private': 'sensitive-financial-value'})
                db.execute(text('SELECT 1'))
            db.commit()
        return {'ok': True}
    app.add_middleware(RequestTimingMiddleware, logger=logger)
    with TestClient(app) as client:
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda i: client.get('/timed/' + str(i)), range(8)))
    assert all(r.status_code == 200 for r in responses)
    entries = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(entries) == 8
    for entry in entries:
        stats = entry['performance']
        assert stats['query_count'] == 2 and stats['connection_count'] == 1
        assert stats['query_ms'] > 0 and stats['connection_ms'] >= 0 and stats['commit_ms'] > 0
        assert stats['test_load_ms'] >= stats['query_ms']
    assert current_timings.get() is None
    assert 'sensitive-financial-value' not in output.getvalue()
    assert 'SELECT' not in output.getvalue()
