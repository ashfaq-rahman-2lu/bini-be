"""Investment reconciliation evidence; preserves existing manual links."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0005'
down_revision = '0004'
branch_labels = depends_on = None


def upgrade():
    for table in ('investment_return_allocations', 'investment_bank_links'):
        op.add_column(table, sa.Column('provenance', sa.String(12), nullable=False, server_default='manual'))
        op.add_column(table, sa.Column('explanation', sa.Text(), nullable=False, server_default=''))
        op.add_column(table, sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table('investment_reinvestment_associations',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('funding_id', sa.Uuid(), sa.ForeignKey('investment_events.id'), nullable=False),
        sa.Column('event_id', sa.Uuid(), sa.ForeignKey('investment_events.id'), nullable=False, unique=True),
        sa.Column('provenance', sa.String(12), nullable=False, server_default='manual'),
        sa.Column('explanation', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint('funding_id <> event_id'))
    op.create_index('ix_investment_reinvestment_associations_funding_id', 'investment_reinvestment_associations', ['funding_id'])
    op.create_table('investment_reconciliation_cases',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('institution_id', sa.Uuid(), sa.ForeignKey('investment_institutions.id'), nullable=False),
        sa.Column('key', sa.String(100), nullable=False),
        sa.Column('event_id', sa.Uuid(), sa.ForeignKey('investment_events.id')),
        sa.Column('kind', sa.String(40), nullable=False),
        sa.Column('status', sa.String(12), nullable=False),
        sa.Column('details', JSONB(), nullable=False),
        sa.Column('resolutions', JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('resolved_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('institution_id', 'key'))
    op.create_index('ix_investment_reconciliation_cases_institution_id', 'investment_reconciliation_cases', ['institution_id'])


def downgrade():
    op.drop_table('investment_reconciliation_cases')
    op.drop_table('investment_reinvestment_associations')
    for table in ('investment_return_allocations', 'investment_bank_links'):
        for column in ('created_at', 'explanation', 'provenance'):
            op.drop_column(table, column)
