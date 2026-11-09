FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.lock.txt ./
RUN python -m pip install --no-cache-dir -r requirements.lock.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

USER 10001:10001

EXPOSE 8000

# Single-instance deployment: migrations must succeed before accepting requests.
CMD ["sh", "-c", "python -m alembic upgrade head && exec python -m uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\""]
