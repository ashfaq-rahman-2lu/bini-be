-- PostgreSQL: permanently clear all Bini application data in the connected database.
-- Preserves tables, constraints, indexes, and Alembic migration history.
-- Run the entire script against the intended database.

BEGIN;

SET LOCAL lock_timeout = '5s';

TRUNCATE TABLE
    public.investment_reconciliation_cases,
    public.investment_reinvestment_associations,
    public.investment_return_allocations,
    public.investment_bank_links,
    public.investment_sources,
    public.investment_events,
    public.investment_projects,
    public.investment_imports,
    public.investment_institutions,
    public.source_links,
    public.statement_rows,
    public.transactions,
    public.statements,
    public.accounts
RESTART IDENTITY RESTRICT;

COMMIT;
