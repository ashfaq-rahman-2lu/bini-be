"""Detect decisions changed in another browser tab."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("statements", sa.Column("review_revision", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("statements", "review_revision")
