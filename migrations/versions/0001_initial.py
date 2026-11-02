"""Initial bank statement schema."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("accounts",
        sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("bank", sa.String(20), nullable=False),
        sa.Column("identifier", sa.String(80), nullable=False), sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("label", sa.String(120), nullable=False), sa.Column("revision", sa.Integer(), nullable=False),
        sa.UniqueConstraint("bank", "identifier", "currency"))
    op.create_table("statements",
        sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False, unique=True), sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("original_pdf", sa.LargeBinary(), nullable=False), sa.Column("period_from", sa.Date(), nullable=False),
        sa.Column("period_to", sa.Date(), nullable=False), sa.Column("opening_balance", sa.Numeric(18,2), nullable=False),
        sa.Column("closing_balance", sa.Numeric(18,2), nullable=False), sa.Column("controls", JSONB, nullable=False),
        sa.Column("checks", JSONB, nullable=False), sa.Column("parser_version", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('draft','confirmed')"))
    op.create_index("ix_statements_account_id", "statements", ["account_id"])
    op.create_table("transactions",
        sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False), sa.Column("direction", sa.String(6), nullable=False),
        sa.Column("amount", sa.Numeric(18,2), nullable=False), sa.Column("balance", sa.Numeric(18,2), nullable=False),
        sa.Column("description", sa.Text(), nullable=False), sa.Column("normalized_description", sa.Text(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False), sa.CheckConstraint("direction IN ('debit','credit')"), sa.CheckConstraint("amount > 0"))
    op.create_index("ix_transaction_match", "transactions", ["account_id","posting_date","direction","amount"])
    op.create_table("statement_rows",
        sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("statement_id", sa.Uuid(), sa.ForeignKey("statements.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False), sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("location", JSONB, nullable=False), sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False), sa.Column("direction", sa.String(6), nullable=False),
        sa.Column("amount", sa.Numeric(18,2), nullable=False), sa.Column("balance", sa.Numeric(18,2), nullable=False),
        sa.Column("description", sa.Text(), nullable=False), sa.Column("normalized_description", sa.Text(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False), sa.Column("decision", sa.String(10)),
        sa.Column("match_id", sa.Uuid(), sa.ForeignKey("transactions.id")), sa.UniqueConstraint("statement_id","ordinal"))
    op.create_table("source_links",
        sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("transaction_id", sa.Uuid(), sa.ForeignKey("transactions.id"), nullable=False),
        sa.Column("statement_row_id", sa.Uuid(), sa.ForeignKey("statement_rows.id", ondelete="CASCADE"), nullable=False, unique=True))
    op.create_index("ix_source_links_transaction_id", "source_links", ["transaction_id"])


def downgrade():
    for table in ["source_links", "statement_rows", "transactions", "statements", "accounts"]:
        op.drop_table(table)
