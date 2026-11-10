from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import QueuePool

from .config import settings
from .performance import current_timings, install_query_timing, phase


class Base(DeclarativeBase):
    pass


class TimedPool(QueuePool):
    def connect(self):
        with phase('connection'):
            connection = super().connect()
        stats = current_timings.get()
        if stats is not None:
            stats.add('connection_count', 1)
        return connection


class TimedSession(Session):
    def commit(self):
        with phase('commit'):
            return super().commit()

    def close(self):
        with phase("transaction_cleanup"):
            return super().close()

    def rollback(self):
        with phase("transaction_cleanup"):
            return super().rollback()

    def flush(self, objects=None):
        with phase('flush'):
            return super().flush(objects)


engine = create_engine(settings.database_url, pool_pre_ping=True, poolclass=TimedPool)
install_query_timing(engine)
SessionLocal = sessionmaker(engine, class_=TimedSession, expire_on_commit=False)


def get_db():
    with SessionLocal() as session:
        yield session
