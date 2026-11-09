"""Payload-free timings shared by async requests and their sync worker threads."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from time import perf_counter


@dataclass
class Timings:
    values: dict = field(default_factory=dict)
    mutex: Lock = field(default_factory=Lock)

    def add(self, name, value):
        with self.mutex:
            self.values[name] = self.values.get(name, 0) + value

    def snapshot(self):
        with self.mutex:
            return {key: round(value, 3) for key, value in self.values.items()}


current_timings = ContextVar('bini_timings', default=None)


@contextmanager
def phase(name):
    timings = current_timings.get()
    started = perf_counter()
    try:
        yield
    finally:
        if timings is not None:
            timings.add(name + '_ms', (perf_counter() - started) * 1000)


def install_query_timing(engine):
    from sqlalchemy import event

    @event.listens_for(engine, 'before_cursor_execute')
    def before(conn, cursor, statement, parameters, context, executemany):
        context._bini_timing = (current_timings.get(), perf_counter())

    def finish(context, failed=False):
        timing = getattr(context, '_bini_timing', None)
        if timing is not None:
            context._bini_timing = None
            stats, started = timing
            if stats is not None:
                stats.add('query_count', 1)
                stats.add('query_ms', (perf_counter() - started) * 1000)
                if failed:
                    stats.add('query_errors', 1)

    @event.listens_for(engine, 'after_cursor_execute')
    def after(conn, cursor, statement, parameters, context, executemany):
        finish(context)

    @event.listens_for(engine, 'handle_error')
    def failed(context):
        finish(context.execution_context, failed=True)
