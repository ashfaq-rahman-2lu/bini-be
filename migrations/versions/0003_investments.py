"""Investment subledger and money-tracing links (no bank ledger changes)."""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE TABLE investment_institutions (\n\tid UUID NOT NULL, \n\tname VARCHAR(120) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (name)\n)')
    op.execute("CREATE TABLE investment_imports (\n\tid UUID NOT NULL, \n\tinstitution_id UUID NOT NULL, \n\tfingerprint VARCHAR(64) NOT NULL, \n\tfilename VARCHAR(255) NOT NULL, \n\toriginal_csv BYTEA NOT NULL, \n\tparsed JSONB NOT NULL, \n\tstatus VARCHAR(12) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (institution_id, fingerprint), \n\tCHECK (status IN ('draft','confirmed')), \n\tFOREIGN KEY(institution_id) REFERENCES investment_institutions (id)\n)")
    op.execute("CREATE TABLE investment_projects (\n\tid UUID NOT NULL, \n\tinstitution_id UUID NOT NULL, \n\tname VARCHAR(255) NOT NULL, \n\tstatus VARCHAR(12) NOT NULL, \n\texpected_total NUMERIC(18, 2), \n\tPRIMARY KEY (id), \n\tUNIQUE (institution_id, name), \n\tCHECK (status IN ('active','closed')), \n\tFOREIGN KEY(institution_id) REFERENCES investment_institutions (id)\n)")
    op.execute("CREATE TABLE investment_events (\n\tid UUID NOT NULL, \n\tinstitution_id UUID NOT NULL, \n\tproject_id UUID, \n\tsignature VARCHAR(64) NOT NULL, \n\toccurrence INTEGER NOT NULL, \n\tposting_date DATE NOT NULL, \n\tkind VARCHAR(30) NOT NULL, \n\tamount NUMERIC(18, 2) NOT NULL, \n\tdescription TEXT NOT NULL, \n\treported_profit NUMERIC(18, 2), \n\tprincipal NUMERIC(18, 2), \n\tready_balance NUMERIC(18, 2) NOT NULL, \n\treported_capital NUMERIC(18, 2) NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (institution_id, signature, occurrence), \n\tCHECK (amount > 0), \n\tCHECK (kind IN ('investment','reinvestment','repayment','refund','cashout','reinvestment_settlement')), \n\tCHECK (principal IS NULL OR (principal >= 0 AND principal <= amount)), \n\tFOREIGN KEY(institution_id) REFERENCES investment_institutions (id), \n\tFOREIGN KEY(project_id) REFERENCES investment_projects (id)\n)")
    op.execute('CREATE INDEX ix_investment_events_project_id ON investment_events (project_id)')
    op.execute('CREATE INDEX ix_investment_events_institution_id ON investment_events (institution_id)')
    op.execute('CREATE TABLE investment_sources (\n\tid UUID NOT NULL, \n\timport_id UUID NOT NULL, \n\tordinal INTEGER NOT NULL, \n\tevent_id UUID NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (import_id, ordinal), \n\tFOREIGN KEY(import_id) REFERENCES investment_imports (id) ON DELETE CASCADE, \n\tFOREIGN KEY(event_id) REFERENCES investment_events (id)\n)')
    op.execute('CREATE INDEX ix_investment_sources_event_id ON investment_sources (event_id)')
    op.execute('CREATE TABLE investment_return_allocations (\n\tid UUID NOT NULL, \n\tsource_id UUID NOT NULL, \n\ttarget_id UUID NOT NULL, \n\tamount NUMERIC(18, 2) NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (source_id, target_id), \n\tCHECK (amount > 0), \n\tCHECK (source_id <> target_id), \n\tFOREIGN KEY(source_id) REFERENCES investment_events (id), \n\tFOREIGN KEY(target_id) REFERENCES investment_events (id)\n)')
    op.execute('CREATE INDEX ix_investment_return_allocations_source_id ON investment_return_allocations (source_id)')
    op.execute('CREATE INDEX ix_investment_return_allocations_target_id ON investment_return_allocations (target_id)')
    op.execute('CREATE TABLE investment_bank_links (\n\tid UUID NOT NULL, \n\tevent_id UUID NOT NULL, \n\ttransaction_id UUID NOT NULL, \n\tamount NUMERIC(18, 2) NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (event_id, transaction_id), \n\tCHECK (amount > 0), \n\tFOREIGN KEY(event_id) REFERENCES investment_events (id), \n\tFOREIGN KEY(transaction_id) REFERENCES transactions (id)\n)')
    op.execute('CREATE INDEX ix_investment_bank_links_event_id ON investment_bank_links (event_id)')
    op.execute('CREATE INDEX ix_investment_bank_links_transaction_id ON investment_bank_links (transaction_id)')


def downgrade():
    op.drop_table('investment_bank_links')
    op.drop_table('investment_return_allocations')
    op.drop_table('investment_sources')
    op.drop_table('investment_events')
    op.drop_table('investment_projects')
    op.drop_table('investment_imports')
    op.drop_table('investment_institutions')
