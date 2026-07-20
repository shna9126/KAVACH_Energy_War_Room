"""Add dedupe key to raw_signals

Revision ID: 20260710_0002
Revises: 20260710_0001
Create Date: 2026-07-10 00:30:00

"""

from __future__ import annotations

import hashlib
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260710_0002"
down_revision: Union[str, None] = "20260710_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _build_dedupe_key(source: str, source_id: str | None, signal_ts, raw_payload) -> str:
    source_id_safe = source_id or ""
    signal_ts_safe = str(signal_ts)
    if isinstance(raw_payload, str):
        payload_json = raw_payload
    else:
        payload_json = json.dumps(raw_payload, sort_keys=True, separators=(",", ":"), default=str)
    raw = f"{source}|{source_id_safe}|{signal_ts_safe}|{payload_json}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def upgrade() -> None:
    with op.batch_alter_table("raw_signals") as batch_op:
        batch_op.add_column(sa.Column("dedupe_key", sa.String(length=64), nullable=True))

    conn = op.get_bind()
    rows = conn.execute(sa.text("select id, source, source_id, signal_ts, raw_payload from raw_signals")).fetchall()
    for row in rows:
        dedupe = _build_dedupe_key(row.source, row.source_id, row.signal_ts, row.raw_payload)
        conn.execute(sa.text("update raw_signals set dedupe_key = :dedupe where id = :id"), {"dedupe": dedupe, "id": row.id})

    with op.batch_alter_table("raw_signals") as batch_op:
        batch_op.alter_column("dedupe_key", existing_type=sa.String(length=64), nullable=False)
        batch_op.create_index("ix_raw_signals_dedupe_key", ["dedupe_key"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("raw_signals") as batch_op:
        batch_op.drop_index("ix_raw_signals_dedupe_key")
        batch_op.drop_column("dedupe_key")
