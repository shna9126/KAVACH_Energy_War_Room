"""Add hypothesis reviews table

Revision ID: 20260710_0004
Revises: 20260710_0003
Create Date: 2026-07-10 02:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260710_0004"
down_revision: Union[str, None] = "20260710_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hypothesis_reviews",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("hypothesis_id", sa.Integer(), sa.ForeignKey("hypotheses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rebuttal_text", sa.Text(), nullable=False),
        sa.Column("counter_confidence", sa.Float(), nullable=True),
        sa.Column("disproof_signals", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("reconciled_confidence", sa.Float(), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_hypothesis_reviews_hypothesis_id", "hypothesis_reviews", ["hypothesis_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_hypothesis_reviews_hypothesis_id", table_name="hypothesis_reviews")
    op.drop_table("hypothesis_reviews")
