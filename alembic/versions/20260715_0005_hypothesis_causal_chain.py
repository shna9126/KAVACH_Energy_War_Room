"""Add reasoning_chain_json column to hypotheses table

Revision ID: 20260715_0005
Revises: 20260710_0004
Create Date: 2026-07-15 10:00:00

Persists the structured `CausalChain` produced by the Reasoning Chain
engine (PRD v2 Upgrade 2) alongside the existing flat `reasoning_chain`
list of strings. Nullable so older rows remain valid.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260715_0005"
down_revision: Union[str, None] = "20260710_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("hypotheses") as batch:
        batch.add_column(sa.Column("reasoning_chain_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("hypotheses") as batch:
        batch.drop_column("reasoning_chain_json")
