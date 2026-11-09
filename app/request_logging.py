"""Structured HTTP timings with optional, buffered Better Stack delivery."""
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from time import perf_counter
from urllib.parse import urlsplit

from .config import settings
from .performance import Timings, current_timings

PROCESS_STARTED = perf_counter()


class RequestJSONFormatter(logging.Formatter):
    def format(self, record):
        # Only these fields are allowed into our stdout event. Never serialize a
        # whole request, LogRecord, exception, SQL statement, or payload.
        return json.dumps({
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
            "service": "bini-api",
            "environment": settings.log_environment,
            "request": getattr(record, "request", {}),
            "performance": getattr(record, "performance", {}),
            "job": getattr(record, "job", {}),
        }, separators=(",", ":"))


def create_request_logger():
    # A dedicated logger forwards only our whitelisted timing events, not ORM,
    # parser, or server exception logs that could contain financial data.
    logger = logging.Logger("bini.requests", level=logging.INFO)
    logger.propagate = False
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(RequestJSONFormatter())
    logger.addHandler(console)
    token = settings.betterstack_source_token.get_secret_value()
    host = settings.betterstack_ingestion_url
    if not token and not host:
        return logger
    try:
        url = urlsplit(host)
        valid_host = (url.scheme == "https" and bool(url.hostname) and
                      not url.username and not url.password and
                      url.path in {"", "/"} and not url.query and not url.fragment)
    except ValueError:
        valid_host = False
    if not token or not valid_host:
        logger.warning("Better Stack forwarding disabled: configure BETTERSTACK_SOURCE_TOKEN and an HTTPS origin in BETTERSTACK_INGESTION_URL.")
        return logger
    from logtail import LogtailHandler

    logger.addHandler(LogtailHandler(
        source_token=token, host=host,
        buffer_capacity=1000, flush_interval=1,
        drop_extra_events=True, timeout=(2, 3), flush_timeout=5,
        raise_exceptions=False,
    ))
    return logger


class RequestTimingMiddleware:
    def __init__(self, app, logger=None):
        self.app = app
        self.logger = logger if logger is not None else create_request_logger()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        started = perf_counter()
        stats = Timings()
        timing_token = current_timings.set(stats)
        status = None
        response_started_ms = None
        disconnected = False
        error_type = None
        outcome = "completed"

        async def timed_receive():
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
            return message

        async def timed_send(message):
            nonlocal status, response_started_ms
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started_ms = round((perf_counter() - started) * 1000, 3)
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                message = {**message, "headers": [*headers, (b"x-request-id", request_id.encode("ascii"))]}
            await send(message)

        try:
            await self.app(scope, timed_receive, timed_send)
        except BaseException as error:
            # Exception messages/tracebacks may include SQL parameters or secrets.
            error_type = type(error).__name__
            outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
            raise
        finally:
            route = scope.get("route")
            request = {
                "id": request_id,
                "method": scope["method"],
                "route": getattr(route, "path", "unmatched"),
                "status_code": status if status is not None else (499 if outcome == "cancelled" or disconnected else 500),
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "response_started_ms": response_started_ms,
                "outcome": "disconnected" if disconnected and outcome == "completed" else outcome,
                "client_disconnected": disconnected,
            }
            if error_type:
                request["error_type"] = error_type
            level = logging.ERROR if error_type or request["status_code"] >= 500 else logging.WARNING if request["status_code"] >= 400 else logging.INFO
            try:
                self.logger.log(level, "HTTP request completed", extra={
                    "service": "bini-api", "environment": settings.log_environment,
                    "request": request,
                    "performance": dict(stats.snapshot(), process_uptime_ms=round((started - PROCESS_STARTED) * 1000, 3)),
                })
            except Exception:
                # Logging failures must not change the API result or mask errors.
                pass
            finally:
                current_timings.reset(timing_token)
