# Bini backend

FastAPI + PostgreSQL backend for EBL bank statement imports, financial reconciliation, and duplicate review. This directory is the backend repository root. Local infrastructure belongs to this repository in `infrastructure/compose.yaml`.

## Requirements

- Python 3.12+
- Docker with Compose

The frontend, API, and database bind to loopback addresses. This release has no authentication and is intended for local use. Public deployment belongs to the later authentication milestone.

## Start locally

From this backend checkout (its location and directory name do not matter):

```sh
docker compose -f infrastructure/compose.yaml up -d --wait
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
cp .env.example .env
.venv/bin/alembic upgrade head
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Start the frontend separately using its repository README. This backend does not require a frontend checkout. API documentation: **http://127.0.0.1:8000/docs**. Copy `.env.example` only during initial setup; preserve an existing `.env` when restarting.

The Compose project name is explicitly `bini-io`, so moving this repository preserves its existing container and database volume identity.

PostgreSQL uses port **55433**, database **bini**, and the dedicated Docker volume `bini-io_postgres_data`. The example credentials are local-development defaults. Override `POSTGRES_PASSWORD` for Compose and update backend `DATABASE_URL` consistently when configuring your own database. Changing the container environment alone does not change an existing database role's password.

If port 8000 is occupied, choose another API port and set `API_PROXY_TARGET=http://127.0.0.1:YOUR_PORT` when starting Vite. The frontend uses `/api` by default. The frontend repository documents `VITE_API_BASE_URL` for independent deployment later. Only public API configuration belongs in Vite variables; database credentials stay in the backend.

## Import and review

1. Choose **Import statement**, select one EBL BDT PDF, and enter its password.
2. Review the extracted rows, financial checks, and duplicate proposals.
3. For ambiguous rows, select an existing transaction or keep the occurrence separately.
4. Confirm the import to add new transactions and link every supporting source occurrence.
5. Filter the ledger by description, inclusive posting-date range, direction, and account.

The import limit is 10 MB / 50 pages, with a 60-second parser process timeout. Scanned PDFs and other bank layouts are rejected. Reconciliation failures cannot be overridden; inspect the source text and correct the parser before retrying. Opening balances, summaries, watermarks, and unsettled-card rows do not become posted transactions.

Matching uses account, posting date, direction, amount, balance, normalized full description, and reference evidence. Similar date/description/amount alone never triggers merging. Multiple source rows cannot silently map to one transaction. Confirmation locks the account, checks the review revision, and commits atomically. Repeated confirmation is safe. Concurrent changes return a refreshed review that must be confirmed again.

The original PDF bytes and source text are retained in PostgreSQL; decrypted PDF copies are not written. Passwords travel only through request/process memory and are not stored in the database, logs, browser storage, or command arguments. Source text and metadata remain sensitive local data. Cancelling deletes an unconfirmed statement and its rows. Confirmed imports cannot be edited or deleted in this release.

Money uses Python Decimal / PostgreSQL NUMERIC(18,2), with decimal strings in JSON. UI totals cover the whole filtered result, not only the current page. Running balances are statement-provided, and net movement is not automatically classified as income or expenses.

## Tests

Tests truncate only a dedicated database with a name ending in `_test`. They must never target your main ledger.

```sh
docker compose -f infrastructure/compose.yaml exec -T postgres createdb -U bini bini_test
DATABASE_URL=postgresql+psycopg://bini:bini_local_only@127.0.0.1:55433/bini_test .venv/bin/alembic upgrade head
.venv/bin/python -m pytest tests -q
```

If the test database already exists, skip `createdb`. Set `TEST_DATABASE_URL` to use a different dedicated test database. To run the optional acceptance cases against your local private samples:

```sh
RUN_PRIVATE_SAMPLES=1 SAMPLE_DIR=/absolute/path/to/private-samples .venv/bin/python -m pytest tests -q
```

Set `SAMPLE_DIR` to a directory containing the supplied PDFs and `password.txt`. Without it, tests use the current workspace fallback `../sample`. Those checks read the supplied PDFs and password locally, without copying them into source code or a frontend bundle. The acceptance baseline is 9 and 11 rows, 5 overlapping occurrences, and 15 unique transactions in either import order.

Frontend browser integration tests are owned by the frontend repository. They accept `BACKEND_DIR` to point at this checkout and require this backend development environment plus a migrated test database. Do not run backend and frontend suites simultaneously against the same test database.

## Database persistence and backup

`docker compose -f infrastructure/compose.yaml stop` stops PostgreSQL and preserves data. Restart with `up -d --wait`. Do not remove its volume unless you intend to delete the ledger.

To create a logical backup, choose a private output location:

```sh
docker compose -f infrastructure/compose.yaml exec -T postgres pg_dump -U bini -Fc bini > /YOUR/PRIVATE/LOCATION/bini-backup.dump
```

Restore into a separate, empty database with `pg_restore`; validate before switching the application's database URL.

## Structure and future work

- `app/`: API, validation schemas, database models, parser, and matching services.
- `migrations/`: versioned PostgreSQL schema changes.
- `tests/`: synthetic financial/import/concurrency tests and optional private-sample checks.
- `infrastructure/compose.yaml`: dedicated local PostgreSQL service and persistent volume.

Deferred: authentication/registration, public hosting, Excel export, batch uploads, categories, notes, inline extraction editing, PDF viewer, OCR, other banks, and investment CSV imports. GraphQL is not required for the initial REST workflow. The parser and financial services can support an additional interface later without duplicating business rules.

The backend `.gitignore` independently excludes Python environments, caches, local environment files, private PDFs/password files, uploads, data, and backups, including infrastructure-local files. Commit source, migrations, requirements/lockfiles, tests, `.env.example`, and `infrastructure/compose.yaml`. Keep real statements and secrets outside version control.
