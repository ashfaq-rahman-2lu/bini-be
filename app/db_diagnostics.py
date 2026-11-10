"""Read-only latency probe: python -m app.db_diagnostics --samples 10."""
import argparse
import json
from statistics import median
from time import perf_counter

from sqlalchemy import text

from .db import engine


def measure(samples):
    started = perf_counter()
    with engine.connect() as connection:
        connection_ms = (perf_counter() - started) * 1000
        query_ms = []
        for _ in range(samples):
            started = perf_counter()
            connection.execute(text('SELECT 1'))
            query_ms.append((perf_counter() - started) * 1000)
        started = perf_counter()
        connection.rollback()
        rollback_ms = (perf_counter() - started) * 1000
    checkout_ms = []
    for _ in range(samples):
        started = perf_counter()
        with engine.connect():
            checkout_ms.append((perf_counter() - started) * 1000)
    def summary(values):
        return dict(min_ms=round(min(values), 3), median_ms=round(median(values), 3), max_ms=round(max(values), 3))
    return dict(samples=samples, initial_connection_ms=round(connection_ms, 3),
        select_one=summary(query_ms), pool_checkout=summary(checkout_ms), rollback_ms=round(rollback_ms, 3))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100:
        parser.error('--samples must be between 1 and 100')
    try:
        print(json.dumps(measure(args.samples)))
    except Exception as error:
        # Connection exceptions can include credentials; expose only their type.
        print(json.dumps(dict(error_type=type(error).__name__)))
        raise SystemExit(1) from None
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
