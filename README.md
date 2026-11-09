# Bini backend

FastAPI + PostgreSQL backend for EBL bank statement imports, financial reconciliation, and duplicate review. This directory is the backend repository root. Local infrastructure belongs to this repository in `infrastructure/compose.yaml`.

## Requirements

- Python 3.12+
- Docker with Compose

Local development binds the frontend, API, and database to loopback addresses. The API requires the single account configured with `AUTH_USERNAME`, `AUTH_PASSWORD`, and `AUTH_SECRET`; see authentication setup below.

## Start locally

From this backend checkout (its location and directory name do not matter):

```sh
docker compose -f infrastructure/compose.yaml up -d --wait
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
cp .env.example .env
# Edit .env: set AUTH_USERNAME, AUTH_PASSWORD and AUTH_SECRET before starting.
.venv/bin/alembic upgrade head
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Start the frontend separately using its repository README. This backend does not require a frontend checkout. API documentation: **http://127.0.0.1:8000/docs**. Copy `.env.example` only during initial setup; preserve an existing `.env` when restarting.

The Compose project name is explicitly `bini-io`, so moving this repository preserves its existing container and database volume identity.

PostgreSQL uses port **55433**, database **bini**, and the dedicated Docker volume `bini-io_postgres_data`. The example credentials are local-development defaults. Override `POSTGRES_PASSWORD` for Compose and update backend `DATABASE_URL` consistently when configuring your own database. Changing the container environment alone does not change an existing database role's password.

If port 8000 is occupied, choose another API port and set `API_PROXY_TARGET=http://127.0.0.1:YOUR_PORT` when starting Vite. The frontend uses `/api` by default. The frontend repository documents `VITE_API_BASE_URL` for independent deployment later. Only public API configuration belongs in Vite variables; database credentials stay in the backend.

## Import and review

1. Choose **Import statement**, select up to 20 EBL BDT PDFs, and enter a shared password or individual passwords.
2. Files are uploaded sequentially through `POST /api/imports`. Each successful file becomes a separate draft; failures do not stop later files. Retry failed files with corrected passwords.
3. Open each draft from the result cards or **Recent imports**, then review its rows, financial checks, and duplicate proposals.
4. For ambiguous rows, select an existing transaction or keep the occurrence separately.
5. Confirm the import to add new transactions and link every supporting source occurrence.
6. Filter the ledger by description, inclusive posting-date range, direction, and account.

The per-file import limit is 10 MB / 50 pages, with a 60-second parser process timeout. Scanned PDFs and other bank layouts are rejected. Reconciliation failures cannot be overridden; inspect the source text and correct the parser before retrying. Opening balances, summaries, watermarks, and unsettled-card rows do not become posted transactions.

Matching uses account, posting date, direction, amount, balance, normalized full description, and reference evidence. Similar date/description/amount alone never triggers merging. Multiple source rows cannot silently map to one transaction. Confirmation locks the account, checks the review revision, and commits atomically. Repeated confirmation is safe. Concurrent changes return a refreshed review that must be confirmed again.

Imported PDF bytes and source text are retained in PostgreSQL. Protected imports are not decrypted to disk; if you choose an already unlocked PDF, those uploaded bytes are stored as the source. Passwords travel only through request/process memory and are not stored in the database, logs, browser storage, or command arguments. Source text and metadata remain sensitive local data. Cancelling deletes an unconfirmed statement and its rows. Confirmed imports cannot be edited or deleted in this release.

Money uses Python Decimal / PostgreSQL NUMERIC(18,2), with decimal strings in JSON. UI totals cover the whole filtered result, not only the current page. Running balances are statement-provided, and net movement is not automatically classified as income or expenses.

## PDF unlock tool and saved imports

`POST /api/pdfs/unlock` accepts a PDF and its password and returns an unencrypted PDF attachment. It supports PDFs independently of the EBL parser, including scanned documents, with the same 10 MB / 50-page limits and a 60-second process timeout. It clones the document tree to retain page content, metadata, outlines, and form fields. Rewriting a PDF may invalidate an existing digital signature.

This endpoint has no database dependency. The backend does not persist its password or decrypted output. The frontend offers a download for the user to save to their computer; it does not put PDF contents or passwords in browser localStorage. The download response uses `Cache-Control: no-store`. Downloaded copies are unencrypted.

`GET /api/imports` lists the latest 50 saved drafts/confirmed imports (configurable `limit`, maximum 100), so reviews remain accessible after navigating away or reloading. It returns only ID, filename, status, and creation time. Each confirmation still rechecks matching and the account revision; a multi-file upload does not automatically confirm any draft. No database migration is required for these features.

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

Deferred: registration/multiple users, Excel export, categories, notes, inline extraction editing, PDF viewer, OCR, and other banks. GraphQL is not required for the REST workflow.

The backend `.gitignore` independently excludes Python environments, caches, local environment files, private PDFs/password files, uploads, data, and backups, including infrastructure-local files. Commit source, migrations, requirements/lockfiles, tests, `.env.example`, and `infrastructure/compose.yaml`. Keep real statements and secrets outside version control.

## Investment tracking

Apply migrations through `0005` with `.venv/bin/alembic upgrade head` and restart an API process running without `--reload`. Migration `0003` adds seven investment tables; `0004` adds extend/replace import modes and updates document uniqueness. Existing imports default to extend; neither migration deletes application rows or rewrites the bank ledger. No additional packages or infrastructure are required.

Use **Investments → Import investment CSV** for the institution's multiline UTF-8 BDT export (maximum 2 MB / 10,000 rows). Review the extracted transactions and warnings before confirming. Drafts do not affect investment totals. Cancelling removes draft source data. Confirmed CSV bytes, parsed original columns, and source row references are retained privately in PostgreSQL. No downloaded CSV or password is bundled with either repository.

Choose **Extend existing data** (the default) to retain existing movements, projects, principal/profit classifications, money traces and bank matches. Institution name plus the full campaign name/number identifies a project. An identical extend document is reused; regenerated overlapping exports match full normalized records, including the reported wallet/capital balances, preserving repeated occurrence counts. Changed full records are shown as new; inspect previews before confirming. Date/amount alone is never used to merge records.

Choose **Replace existing data** to rebuild only the named institution from the supplied CSV. Use complete history: movements/projects absent from the CSV are removed. The preview shows counts of movements, projects, other imports/previews, saved splits, traces and bank matches to remove. Confirmation requires both the ordinary review acknowledgment and `acknowledge_replace: true`. It removes the institution's old source imports (including other pending previews), movements, project statuses/expected targets, classifications, allocations and bank links before importing fresh records. Bank accounts/statements/transactions and other institutions are unchanged. Uploading or cancelling a replacement draft does not change the ledger. Replacement deletion and recreation share one transaction and the existing investment write lock; any error rolls everything back. Stale reviews must refresh; other previews removed by a completed replacement must be uploaded again.

Confirmation is idempotent for an import ID. A repeated confirmation does not replace again. Uploading the same CSV again in replace mode deliberately creates/reuses a pending replacement, even if that file was confirmed before; this supports restoring a full archive after later extensions. Only one draft per institution/document/mode is kept, with confirmed and draft replacement generations allowed to coexist until confirmation removes the old reports.

Financial definitions:

- **External funding** includes BANK_TRANSFER investments only. **Total invested** also includes REINVESTMENT funding.
- **Repayments** contain capital and profit. They are unclassified until a principal/profit split is explicitly saved. Source profit notes remain separate evidence, because they may describe a different period or a cumulative payment.
- **Principal outstanding** for an active project is investment funding less classified returned principal and capital refunds. It is unavailable while any repayment remains unclassified or classified principal exceeds recorded funding. It assumes the recorded investment history is complete. A closed, finally settled project has no outstanding claim; unreturned classified principal is shown as capital written off, separately from final net gain/loss.
- **Final gain/loss** equals repayments plus capital refunds less all project investment funding, including reinvestment funding. It is shown only after the user confirms complete history and final settlement and closes a project. Final ROI uses that project's total funding, is not annualized, and is unavailable for projects without recorded funding. Closing can recognize a capital loss; it does not fabricate installment splits.
- **Returns less investments** shows cash recovery, not an active project's estimated loss. Expected total repayments are an optional user target, separate from actual returns.
- **Wallet balance** is the final value in the latest confirmed report, with its as-of date. Untraced returns are a missing attribution, not an available-wallet balance.
- **Disbursed / reinvestment** settles the wallet funding entry. It is neither another investment nor a bank payout. Daily reinvestment funding and settlement totals must agree. Only **Disbursed / cashout** counts as a provider bank payout.

The parser checks monetary precision, types, chronological order, summary/TOTAL controls, wallet movements, and reported business-capital movements. A partial export may infer nonzero opening balances, which produce warnings about missing history. The provider's “Capital With Business” subtracts complete repayments, including profit; it must not be interpreted as outstanding principal. Negative reported wallet values and the supplied summary/capital discrepancy are retained with warnings. Nonzero protection-pool activity is rejected explicitly pending a dedicated model.

Monetary cells accept both Western (`138,920`) and Indian (`1,38,920`) grouping, with at most two decimal places. Column G also accepts the CSV updater's running `=SUM($F$20:F20)-SUM($C$20:C20)` formulas (using the actual first/current CSV record numbers), and TOTAL may reference the final transaction's G cell. The parser computes these specific formulas with Decimal arithmetic, validates their references, and retains the original formula text as source evidence; arbitrary formulas are rejected. Summary rows padded with empty columns are still validated. Corrected repayments must reconcile with summary/TOTAL controls and column J; the importer does not silently repair inconsistent source balances.

TOTAL's repayment cell F also accepts `=SUM(F20:F90)` using the actual first/final transaction record numbers. The “Total Repaid (from campaigns)” summary amount may reference that exact TOTAL cell (for example `=F91`). Both resolve to Decimal monetary values for validation; incorrect ranges, references to other cells, and arbitrary expressions are rejected. Numeric repayment controls remain supported, and unchanged transactions retain the same duplicate identities across numeric/formula exports.

Column J accepts the updater's `=SUM($D$20:D20)-SUM($E$20:E20)-SUM($F$20:F20)` running capital formula, optionally followed by a signed opening capital amount such as `+250.00` for partial history. TOTAL's J may reference the final transaction (for example `=J90`). References and monetary bounds are validated, Decimal arithmetic resolves the values, and running-capital reconciliation still blocks inconsistent opening amounts across rows. Original formulas remain in source evidence; equivalent numeric/formula records have identical duplicate signatures. This follows the provider's capital convention, not classified principal outstanding.

Summary “Ready Balance” accepts a reference to TOTAL's G cell (for example `=G91`); “Total Currently Invested” accepts the sum of TOTAL's J and G cells (`=J91+G91`). The parser validates both references against the actual TOTAL record, resolves them with Decimal arithmetic, and rejects arbitrary expressions. Numeric summaries remain supported: Ready Balance must reconcile exactly, while a differing numeric Total Currently Invested produces a warning against final capital with business **plus** ready balance. Formula and equivalent numeric summaries do not change transaction duplicate identities.

Trace returned funds by allocating an installment/refund to one or more later reinvestments or cashouts. A destination can combine multiple source returns. Reconciliation derives attribution only when source order, whole-credit amounts, wallet balances and business ownership establish a unique answer. Otherwise it requires review. Traces cannot exceed either side, cross institutions, or consume future returns. Follow a cashout trace to its bank links to see whether the payout actually appears in the bank ledger. Remove incorrect traces/links before replacing them.

Bank suggestions use BDT and the correct debit/credit direction. For an external investment booked on July 2, the default search includes bank debits June 29–July 2. Cashouts search bank credits from the payout date through three days later. The UI can widen the window to 30 days or look up a bank transaction ID manually. Uniquely resolved exact matches with the configured description patterns can be saved automatically by reconciliation. Multiple candidates require a choice. Split/combined transfers use explicit link amounts; a bank transaction's capacity is shared across all investment events and institutions. A PostgreSQL advisory transaction lock serializes all investment mutations to prevent concurrent over-allocation; bank transactions themselves are unchanged.

REST endpoints:

- `GET /api/investments` — consistent project totals, activity, source references, traces and bank links.
- `GET/POST /api/investments/imports`, `GET/DELETE /api/investments/imports/{id}`, `POST /api/investments/imports/{id}/confirm` — saved review workflow.
- `PATCH /api/investments/projects/{id}` — status and optional expected total; closing requires `complete_history: true`.
- `PATCH /api/investments/events/{id}/split` — explicit principal amount, or `null` to clear classification.
- `POST /api/investments/allocations`, `DELETE /api/investments/allocations/{id}` — return-to-destination traces.
- `GET /api/investments/events/{id}/bank-candidates` — direction/date/account suggestions.
- `POST /api/investments/bank-links`, `DELETE /api/investments/bank-links/{id}` — explicit bank transfer links.

All monetary responses are decimal strings. This release returns the investment workspace as one consistent snapshot; the UI paginates activity in groups of 50. Institution naming and per-user account ownership remain future work: the configured single account accesses the whole workspace. Import earlier history before marking a partially covered project complete. Renaming a campaign or changing a provider's historical balances requires reviewing changed records rather than assuming they are duplicates.

Import upload accepts multipart `mode=extend|replace` (default `extend`). Import listings/reviews return `mode`; draft replacement reviews additionally return removal counts in `replacement`. Confirmations send `expected_revision`, `acknowledge_warnings`, and, for replacement, `acknowledge_replace`. Invalid mode, malformed CSV, missing acknowledgment, and stale revision are rejected before any deletion.

Tests use the dedicated `*_test` database, now also resetting investment data. `RUN_PRIVATE_SAMPLES=1` optionally validates the local investment CSV; default tests use generated synthetic CSVs. To intentionally clear *all* application data later, investment bank links and traces must be removed before bank transactions; existing bank-only DELETE sequences will be blocked by these new foreign keys. No application-data reset occurs during migrations.


## Wallet reconciliation and project money trails

Migration `0005` adds reconciliation cases with review history and reinvestment group associations, plus provenance, explanations and timestamps on return allocations and bank links. Existing links default to **manual** and are preserved. Their new timestamp records migration time, since their original creation time was not previously recorded. The migration does not generate money links or alter bank transactions/source documents.

Run `alembic upgrade head` before starting the updated API. The initial matching action is **Reconcile now**. CSV and bank-statement confirmation in the frontend subsequently sends a separate reconciliation POST; failure leaves the completed import intact and offers retry. GET endpoints only calculate/read results. New imports and all link/review mutations advance review revisions. Bank confirmations and investment writes share the investment advisory lock, then acquire any account/statement locks in that order.

The pure service in `app/investment_reconciliation.py` merges confirmed source-ordinal ordering constraints and replays each canonical event once. It rejects conflicting or indeterminate same-day order. Nonzero opening funds with unknown business ownership, negative wallets, inconsistent balances and unresolved consumption block dependent wallet matching. Unallocated money is never presented as confirmed available cash in an unresolved wallet. Bank matching continues independently.

A cashout leaving a positive wallet must consume whole outstanding credits from exactly one business. Full-wallet cashouts consume all available credits, including multiple businesses. Automatic attribution requires a unique combination; bounded searches stop for review rather than using FIFO or claiming uniqueness from incomplete results.

Reinvestment reserves source credits against the destination funding event. Associated settlement rows release those reservations; they are not a second investment or a bank transfer. A single unambiguous same-day funding group may be associated automatically, including its bank top-up. Multiple groups require reviewed settlement associations. Grouping can be reviewed before attribution. An available balance different from reinvestment funding produces a delayed-booking case. Select the actual credits, explain the earlier instruction/later booking, and optionally record the instruction date. Later credits remain available. Reviews cannot consume future credits or exclude known earlier available funds from the full-wallet instruction.

Bank automation requires BDT, correct debit/credit direction, an exact remaining-amount match, and the expected normalized description: `Other Bank Transfer SKB-BEFTN` for funding; `EFT INFLOW - THE CITY BANK LTD., CBL, Payment,` for receipts. Funding searches zero–three calendar days before booking; cashouts search zero–three days after. Competing events across institutions prevent automatic matching. BUSINESS DATE is supporting metadata and never replaces bank posting date. Manual matching still supports wider dates and split/combined transfers. Previously automatic decisions are revalidated when history changes; conflicting links remain visible for review and are not silently overwritten.

Project trails contain direct funding, installments, their cashouts/reinvestments, settlement groups and related bank links. Shared destinations show all contributing installments, with each project's own share of a full bank receipt. If a mixed payout is split among bank receipts, the project share of each receipt remains unknown; no proportional share is invented. Subsequent returns of a destination project belong to that project. Profit/principal classifications remain unchanged.

Additional REST endpoints:

- `GET /api/investments/reconciliation` — fresh analysis, revision token, saved cases and review history. Unsaved unique proposals are identified separately.
- `POST /api/investments/reconciliation/run` — atomically save unique allocations, bank matches, associations and current review cases; repeatable without duplicates.
- `PATCH /api/investments/reconciliation/cases/{id}` — review with `expected_revision`, `explanation`, selected `allocations`, `association_ids` or `bank_links`, and optional `delayed_booking` / `instruction_date`. Stale decisions return 409; invalid amounts roll back.
- `DELETE /api/investments/reconciliation/associations/{id}` — remove an incorrect association explicitly, then reconcile again.
- `GET /api/investments/projects/{id}/trail` — direct movements, wallet availability/reservations/consumption, source references, provenance and related bank receipts.

Extend imports preserve decisions and revalidate them. Replace previews include reconciliation case and association counts; confirmation clears that institution's reconciliation data atomically with its old investment data. Bank records and other institutions remain untouched. Source discrepancies still require correcting/reviewing the CSV; reconciliation never weakens TOTAL or running-balance validation.

`tests/test_reconciliation.py` exercises the synthetic Apon example, whole-wallet and automatic payouts, ambiguity, delayed booking, split settlements, posting/business dates, overlap/order, bank capacity, later imports, concurrency, revision conflicts and direct-only trails. Use the existing dedicated `_test` database setup; do not run backend and browser suites simultaneously against it.

Project updates accept `confirm_wallet_empty: true` with closed status and complete-history acknowledgment. Under the investment write lock, completion rejects returns without full saved allocations or with available/reserved wallet funds. Closing does not change allocations, bank records, or wallet balances; reopening uses the existing active status. No schema migration is required.

### Reviewed small wallet differences

Wallet source reviews can explicitly accept an absolute difference up to **BDT 5.00**. The server calculates `selected source credits − recorded payment`, validates whole-credit capacities, and stores the signed decimal-string difference, source selection, explanation, timestamp and original evidence in the existing reconciliation resolution history. No migration is needed. No differences are accepted automatically; CSV validation and bank matching remain exact.

Source allocations retain their full credit amounts. For these settlements they represent gross source contributions, not exact net shares. The difference belongs to the settlement pool rather than an arbitrary business. It is excluded from spendable wallet attribution, retained separately from the provider's original Ready Balance, and exposed in reconciliation, event and project-trail responses. A shortfall that leaves other credits without verified backing blocks dependent wallet spending. Related bank shares remain unassigned where the difference prevents exact attribution. Original investment, return, profit and bank amounts remain unchanged. Removing/changing source connections invalidates the accepted difference until reviewed again. Extend preserves the evidence; replace clears it with the institution's existing reconciliation history.

## Single-account authentication

Set these only in the backend `.env` locally, or in Railway's service variables:

```dotenv
AUTH_USERNAME=admin
AUTH_PASSWORD=<your-long-unique-password>
AUTH_SECRET=<random-signing-secret-at-least-32-characters>
AUTH_COOKIE_SECURE=true
```

Use your password manager to generate and save a strong password and a separate random signing secret (for example, `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`). The placeholders above are not working credentials. Missing credentials or a signing secret shorter than 32 characters cause login and protected routes to return 503; there is no default password or authentication bypass. Restart the backend after changing configuration. Never commit `.env` or put these values into frontend variables.

`POST /api/auth/login` accepts JSON `username` and `password`, sets an eight-hour signed `bini_session` cookie, and returns only the username. The browser sends the cookie automatically; JavaScript cannot read it (`HttpOnly`). It uses `SameSite=Lax`, path `/api`, and `Secure` by default. Set `AUTH_COOKIE_SECURE=false` only for local HTTP development, as shown in `.env.example`. Keep it `true` on Railway.

All financial and PDF routes require the cookie. `GET /api/auth/session` restores the frontend session after refresh; `POST /api/auth/logout` deletes the cookie. Every POST/PATCH/DELETE request, including login/logout, must include `X-Bini-Request: 1`. If an Origin header is present, it must be in `ALLOWED_ORIGINS`; credentialed CORS preflights also enforce that list. The frontend supplies this protection header automatically. API responses use `Cache-Control: no-store`. `/api/health` remains public. `/docs` and `/openapi.json` expose descriptions, not financial records; command-line API clients need a cookie jar and the protection header.

Changing the username, password, or signing secret invalidates existing sessions. Keep the secret consistent across workers/instances. Sessions are stateless: sign-out deletes this browser's cookie and clears the frontend cache; a separately copied cookie remains valid until expiry or credential/secret rotation. Ten failed logins within one minute temporarily block further attempts in that process. The limiter resets on restart and is independent per worker.

No authentication tables, database migrations, or Supabase Auth configuration are needed. All signed-in requests access the same workspace. For Netlify + Railway, use the frontend's `/api` proxy setup in its README so the cookie works on the provider's default domains. Set `ALLOWED_ORIGINS` to the exact HTTPS frontend origin, for example `["https://your-site.netlify.app"]`. Direct requests from `netlify.app` to `up.railway.app` are cross-site and will not carry this Lax cookie. Same-site HTTPS custom domains are an alternative. Keep the unused Supabase Data API disabled for the existing deployment plan.

The pytest fixtures use separate synthetic credentials and cookie-authenticated clients. Authentication tests run without a database and cover cookie flags, logout, expiry/tampering, credential rotation, fail-closed configuration, every data route, CSRF/CORS, public health checks, and login throttling.


## Deploy the backend on Railway

The repository-root `Dockerfile` pins Python 3.12, installs `requirements.lock.txt`, and includes only application code and Alembic migrations. `.dockerignore` keeps local secrets, private statements, virtual environments, tests, and backups out of the build context. The container runs as an unprivileged user.

1. Commit and push this backend repository, including the authentication changes. In Railway, create a service from `bini-be` and select the branch containing these files. Leave Root Directory empty for the separate backend repository; if deploying the parent workspace, use `/backend`.
2. Use the included Dockerfile. Leave custom build and start commands unset so Railway uses the image command. Do not add a Railway PostgreSQL service: this application uses the existing Supabase database.
3. Set these Railway service variables privately before starting the service:

   ```dotenv
   DATABASE_URL=postgresql+psycopg://postgres.PROJECT_REF:ENCODED_PASSWORD@SESSION_POOLER_HOST:5432/postgres?sslmode=require
   AUTH_USERNAME=<your-login-name>
   AUTH_PASSWORD=<your-long-unique-login-password>
   AUTH_SECRET=<random-signing-secret-at-least-32-characters>
   AUTH_COOKIE_SECURE=true
   ALLOWED_ORIGINS=["https://your-site.netlify.app"]
   ```

   Copy the exact session-pooler host and username from Supabase's Connect dialog, replace the database password, and percent-encode reserved password characters. The Supabase database password is separate from the application's `AUTH_PASSWORD`. Local `.env` values are not uploaded or included in the image. Keep the unused Supabase Data API disabled.
4. Use one replica, set Healthcheck Path to `/api/health`, and enable Serverless in the deployment settings for infrequent use. Expect a cold start after sleeping. Sleeping reduces compute usage but does not waive any plan minimum or other charges. Avoid uptime pings if you want the service to sleep.
5. Deploy and generate a public domain under the service's networking settings. The image runs `python -m alembic upgrade head` before starting Uvicorn on `0.0.0.0:$PORT` (8000 when PORT is absent). Migrations failing prevent startup. Health returns `{"status":"ok"}` after connecting to PostgreSQL.
6. Set Netlify's build variable `BACKEND_URL=https://your-backend.up.railway.app`, with no `/api`, and rebuild the frontend. Its existing build script uses `/api` and generates the proxy needed for cookie sessions. Match `ALLOWED_ORIGINS` to the final frontend origin exactly. See the frontend README for the proxy timeout and same-site custom-domain alternative.
7. Check login, refresh persistence, a synthetic import, and logout. An unauthenticated request to `/api/transactions` should return 401. Health remains public.

Migrations create/update the schema; they do not copy your existing local records to Supabase. Back up and migrate existing data separately before switching production traffic. No persistent Railway volume is needed: uploaded source documents are stored in PostgreSQL.

Startup migrations are intended for this single-replica personal deployment. Before scaling to multiple replicas, move `python -m alembic upgrade head` to Railway's Pre-Deploy Command and override Start Command with `sh -c 'exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"'` so replicas do not run migrations concurrently.

Reference: [Railway Dockerfiles](https://docs.railway.com/builds/dockerfiles), [pre-deploy commands](https://docs.railway.com/deployments/pre-deploy-command), and [Serverless](https://docs.railway.com/deployments/serverless).
