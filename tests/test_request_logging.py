import asyncio
import io
import json
import logging
import re
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import request_logging
from app.config import settings
from app.request_logging import RequestJSONFormatter, RequestTimingMiddleware, create_request_logger


@pytest.fixture
def output():
    stream = io.StringIO()
    logger = logging.Logger("request-test", level=logging.INFO)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RequestJSONFormatter())
    logger.addHandler(handler)
    return logger, stream


def events(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_route_status_duration_and_id_exclude_sensitive_request_data(output):
    logger, stream = output
    app = FastAPI()

    @app.post("/api/items/{item_id}")
    def item(item_id: str, body: dict):
        return {"secret_response": "response-financial-data"}

    app.add_middleware(RequestTimingMiddleware, logger=logger)
    with TestClient(app) as client:
        response = client.post("/api/items/private-account-id?search=private-search", json={"password": "private-password"},
            headers={"Authorization": "Bearer private-token", "Cookie": "bini_session=private-cookie", "X-Request-ID": "private-caller-id"})
    assert response.status_code == 200
    event, = events(stream)
    request = event["request"]
    assert request["route"] == "/api/items/{item_id}"
    assert request["method"] == "POST" and request["status_code"] == 200
    assert request["duration_ms"] >= request["response_started_ms"] >= 0
    assert re.fullmatch(r"[0-9a-f]{32}", request["id"])
    assert response.headers["x-request-id"] == request["id"]
    for secret in ["private-account-id", "private-search", "private-password", "private-token", "private-cookie", "private-caller-id", "response-financial-data"]:
        assert secret not in stream.getvalue()


def test_handled_failures_unmatched_routes_and_preflight_are_logged(output):
    logger, stream = output
    app = FastAPI()

    @app.get("/api/protected")
    def protected():
        raise HTTPException(401, "private-detail")

    app.add_middleware(CORSMiddleware, allow_origins=["https://example.test"], allow_methods=["GET"])
    app.add_middleware(RequestTimingMiddleware, logger=logger)
    with TestClient(app) as client:
        assert client.get("/api/protected").status_code == 401
        assert client.get("/unknown-private-value").status_code == 404
        assert client.options("/api/protected", headers={"Origin": "https://example.test", "Access-Control-Request-Method": "GET"}).status_code == 200
    logged = events(stream)
    assert [event["request"]["status_code"] for event in logged] == [401, 404, 200]
    assert [event["level"] for event in logged] == ["warning", "warning", "info"]
    assert logged[1]["request"]["route"] == "unmatched"
    assert "private-detail" not in stream.getvalue()
    assert "unknown-private-value" not in stream.getvalue()


def test_unhandled_errors_are_timed_without_logging_exception_messages(output):
    logger, stream = output
    app = FastAPI()

    @app.get("/api/fail")
    def fail():
        raise RuntimeError("private SQL parameters and password")

    app.add_middleware(RequestTimingMiddleware, logger=logger)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/fail").status_code == 500
    event, = events(stream)
    assert event["level"] == "error"
    assert event["request"]["status_code"] == 500
    assert event["request"]["error_type"] == "RuntimeError"
    assert event["request"]["outcome"] == "error"
    assert "private SQL" not in stream.getvalue()


def test_streaming_duration_includes_the_final_body(output, monkeypatch):
    logger, stream = output
    clock = SimpleNamespace(value=10.0)
    monkeypatch.setattr(request_logging, "perf_counter", lambda: clock.value)

    async def application(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/api/stream")
        clock.value = 10.1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        clock.value = 12.0
        await send({"type": "http.response.body", "body": b"private", "more_body": False})

    async def send(message):
        pass

    asyncio.run(RequestTimingMiddleware(application, logger=logger)({"type": "http", "method": "GET"}, None, send))
    request = events(stream)[0]["request"]
    assert request["response_started_ms"] == 100
    assert request["duration_ms"] == 2000


def test_cancelled_request_is_logged_and_cancellation_propagates(output):
    logger, stream = output

    async def application(scope, receive, send):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(RequestTimingMiddleware(application, logger=logger)({"type": "http", "method": "POST"}, None, None))
    request = events(stream)[0]["request"]
    assert request["outcome"] == "cancelled" and request["status_code"] == 499


def test_logging_failure_does_not_change_http_response():
    class BrokenLogger:
        def log(self, *args, **kwargs):
            raise RuntimeError("Synthetic logging failure")

    app = FastAPI()

    @app.get("/api/ok")
    def ok():
        return {"ok": True}

    app.add_middleware(RequestTimingMiddleware, logger=BrokenLogger())
    with TestClient(app) as client:
        assert client.get("/api/ok").json() == {"ok": True}


def test_betterstack_configures_nonblocking_buffered_delivery(monkeypatch, capsys):
    import logtail

    captured = {}

    class FakeHandler(logging.NullHandler):
        def __init__(self, **kwargs):
            super().__init__()
            captured.update(kwargs)

    monkeypatch.setattr(logtail, "LogtailHandler", FakeHandler)
    monkeypatch.setattr(settings, "betterstack_source_token", SecretStr("synthetic-source-token"))
    monkeypatch.setattr(settings, "betterstack_ingestion_url", "https://example.betterstackdata.com")
    logger = create_request_logger()
    assert len(logger.handlers) == 2 and not logger.propagate
    assert captured["source_token"] == "synthetic-source-token"
    assert captured["host"] == "https://example.betterstackdata.com"
    assert captured["drop_extra_events"] is True
    assert captured["buffer_capacity"] == 1000
    assert captured["flush_timeout"] == 5
    assert "synthetic-source-token" not in capsys.readouterr().out


@pytest.mark.parametrize("host", ["", "http://example.test", "https://user:password@example.test", "https://example.test/api", "https://example.test?token=private"])
def test_incomplete_or_invalid_sink_config_stays_local(monkeypatch, capsys, host):
    monkeypatch.setattr(settings, "betterstack_source_token", SecretStr("synthetic-source-token"))
    monkeypatch.setattr(settings, "betterstack_ingestion_url", host)
    logger = create_request_logger()
    assert len(logger.handlers) == 1
    output = capsys.readouterr().out
    assert "forwarding disabled" in output
    assert "synthetic-source-token" not in output


def test_default_logger_writes_json_without_remote_sink(monkeypatch, capsys):
    monkeypatch.setattr(settings, "betterstack_source_token", SecretStr(""))
    monkeypatch.setattr(settings, "betterstack_ingestion_url", "")
    logger = create_request_logger()
    logger.info("HTTP request completed", extra={"request": {"duration_ms": 125.5}})
    assert len(logger.handlers) == 1
    event = json.loads(capsys.readouterr().out)
    assert event["request"]["duration_ms"] == 125.5


def test_sdk_sends_structured_events_on_background_thread(monkeypatch):
    from logtail.uploader import Uploader

    captured = []
    delivered = threading.Event()
    caller_thread = threading.get_ident()

    def fake_upload(self, frames):
        captured.append((threading.get_ident(), frames))
        delivered.set()
        return SimpleNamespace(status_code=202)

    monkeypatch.setattr(Uploader, "__call__", fake_upload)
    monkeypatch.setattr(settings, "betterstack_source_token", SecretStr("synthetic-source-token"))
    monkeypatch.setattr(settings, "betterstack_ingestion_url", "https://example.betterstackdata.com")
    logger = create_request_logger()
    remote = logger.handlers[1]
    logger.info("HTTP request completed", extra={"service": "bini-api", "request": {"duration_ms": 12.5, "route": "/api/health"}})
    remote.flush()
    assert delivered.is_set()
    thread_id, frames = captured[0]
    assert thread_id != caller_thread
    assert frames[0]["request"]["duration_ms"] == 12.5
    assert frames[0]["request"]["route"] == "/api/health"
    assert "synthetic-source-token" not in json.dumps(frames)
    # Stop the synthetic SDK worker before restoring the mocked transport.
    remote.flush_thread.should_run = False
    remote.flush_thread.join(timeout=2)
    assert not remote.flush_thread.is_alive()
