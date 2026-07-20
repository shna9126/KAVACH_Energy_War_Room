"""Initial Layer 2 tables

Revision ID: 20260710_0001
Revises:
Create Date: 2026-07-10 00:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260710_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "raw_signals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=512), nullable=True),
        sa.Column("signal_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entities_hint", sa.JSON(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("inserted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_raw_signals_source", "raw_signals", ["source"])
    op.create_index("ix_raw_signals_source_id", "raw_signals", ["source_id"])
    op.create_index("ix_raw_signals_signal_ts", "raw_signals", ["signal_ts"])

    op.create_table(
        "structured_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("raw_signal_id", sa.Integer(), sa.ForeignKey("raw_signals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action_type", sa.String(length=128), nullable=True),
        sa.Column("target", sa.String(length=256), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("actors", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("extracted_payload", sa.JSON(), nullable=False),
        sa.Column("inserted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_structured_events_raw_signal_id", "structured_events", ["raw_signal_id"])
    op.create_index("ix_structured_events_event_ts", "structured_events", ["event_ts"])

    op.create_table(
        "hypotheses",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("structured_event_id", sa.Integer(), sa.ForeignKey("structured_events.id", ondelete="SET NULL"), nullable=True),
        sa.Column("hypothesis_text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("reasoning_chain", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_hypotheses_structured_event_id", "hypotheses", ["structured_event_id"])

    op.create_table(
        "simulations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("hypothesis_id", sa.Integer(), sa.ForeignKey("hypotheses.id", ondelete="SET NULL"), nullable=True),
        sa.Column("horizon", sa.String(length=32), nullable=False),
        sa.Column("percentiles", sa.JSON(), nullable=False),
        sa.Column("distribution", sa.JSON(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_simulations_hypothesis_id", "simulations", ["hypothesis_id"])

    op.create_table(
        "recommendations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("simulation_id", sa.Integer(), sa.ForeignKey("simulations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("recommendation_type", sa.String(length=64), nullable=False),
        sa.Column("recommendation_payload", sa.JSON(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_recommendations_simulation_id", "recommendations", ["simulation_id"])


def downgrade() -> None:
    op.drop_index("ix_recommendations_simulation_id", table_name="recommendations")
    op.drop_table("recommendations")

    op.drop_index("ix_simulations_hypothesis_id", table_name="simulations")
    op.drop_table("simulations")

    op.drop_index("ix_hypotheses_structured_event_id", table_name="hypotheses")
    op.drop_table("hypotheses")

    op.drop_index("ix_structured_events_event_ts", table_name="structured_events")
    op.drop_index("ix_structured_events_raw_signal_id", table_name="structured_events")
    op.drop_table("structured_events")

    op.drop_index("ix_raw_signals_signal_ts", table_name="raw_signals")
    op.drop_index("ix_raw_signals_source_id", table_name="raw_signals")
    op.drop_index("ix_raw_signals_source", table_name="raw_signals")
    op.drop_table("raw_signals")