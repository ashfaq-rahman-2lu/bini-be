"""Persist confirmation/reconciliation jobs without external queue services."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0006'
down_revision = '0005'
branch_labels = depends_on = None


def upgrade():
    op.create_table('operation_jobs',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('kind', sa.String(30), nullable=False),
        sa.Column('target_id', sa.Uuid()),
        sa.Column('dedupe_key', sa.String(64), nullable=False, unique=True),
        sa.Column('active_key', sa.String(80), unique=True),
        sa.Column('payload', JSONB(), nullable=False),
        sa.Column('result', JSONB()),
        sa.Column('status', sa.String(12), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('error_status', sa.Integer()),
        sa.Column('error_detail', sa.String(1000)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('available_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('queued','succeeded','failed')"),
        sa.CheckConstraint("kind IN ('investment_confirm','bank_confirm','reconcile')"))
    op.create_index('ix_operation_jobs_queue', 'operation_jobs', ['available_at', 'created_at'], postgresql_where=sa.text("status = 'queued'"))


def downgrade():
    op.drop_table('operation_jobs')
