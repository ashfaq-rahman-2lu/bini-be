"""Stage extend or replace imports without changing existing investment data."""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('investment_imports', sa.Column('mode', sa.String(10), nullable=False, server_default='extend'))
    op.create_check_constraint('ck_investment_import_mode', 'investment_imports', "mode IN ('extend','replace')")
    op.drop_constraint('investment_imports_institution_id_fingerprint_key', 'investment_imports', type_='unique')
    op.create_unique_constraint('uq_investment_import_document_mode_status', 'investment_imports', ['institution_id', 'fingerprint', 'mode', 'status'])


def downgrade():
    # Do not discard reports to satisfy the older document-identity constraint.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investment_imports GROUP BY institution_id, fingerprint HAVING count(*) > 1) THEN
            RAISE EXCEPTION 'Cannot downgrade: multiple import modes share a document. Resolve these imports first.';
        END IF;
    END $$""")
    op.drop_constraint('uq_investment_import_document_mode_status', 'investment_imports', type_='unique')
    op.create_unique_constraint('investment_imports_institution_id_fingerprint_key', 'investment_imports', ['institution_id', 'fingerprint'])
    op.drop_constraint('ck_investment_import_mode', 'investment_imports', type_='check')
    op.drop_column('investment_imports', 'mode')
