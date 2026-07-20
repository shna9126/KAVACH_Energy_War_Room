"""Normalize dedupe key and purge duplicates

Revision ID: 20260710_0003
Revises: 20260710_0002
Create Date: 2026-07-10 01:00:00

"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260710_0003"
down_revision: Union[str, None] = "20260710_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _dedupe_key(source: str, source_id: str | None, signal_ts, raw_payload) -> str:
    if source_id:
        raw = f"{source}|{source_id}"
    else:
        if isinstance(raw_payload, str):
            payload_json = raw_payload
        else:
            payload_json = json.dumps(raw_payload, sort_keys=True, separators=(",", ":"), default=str)
        raw = f"{source}|{signal_ts}|{payload_json}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def upgrade() -> None:
    conn = op.get_bind()

    # Temporarily drop unique index so updates can be staged before duplicate cleanup.
    with op.batch_alter_table("raw_signals") as batch_op:
        batch_op.drop_index("ix_raw_signals_dedupe_key")

    rows = conn.execute(sa.text("select id, source, source_id, signal_ts, raw_payload from raw_signals order by id")).fetchall()

    groups: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        key = _dedupe_key(row.source, row.source_id, row.signal_ts, row.raw_payload)
        groups[key].append(row.id)

    # Keep first row per key, delete later duplicates.
    duplicate_ids: list[int] = []
    for ids in groups.values():
        if len(ids) > 1:
            duplicate_ids.extend(ids[1:])

    if duplicate_ids:
        conn.execute(sa.text("delete from raw_signals where id in :ids").bindparams(sa.bindparam("ids", expanding=True)), {"ids": duplicate_ids})

    # Re-read current rows and assign normalized dedupe keys.
    rows_after = conn.execute(sa.text("select id, source, source_id, signal_ts, raw_payload from raw_signals order by id")).fetchall()
    for row in rows_after:
        key = _dedupe_key(row.source, row.source_id, row.signal_ts, row.raw_payload)
        conn.execute(sa.text("update raw_signals set dedupe_key = :k where id = :id"), {"k": key, "id": row.id})

    with op.batch_alter_table("raw_signals") as batch_op:
        batch_op.create_index("ix_raw_signals_dedupe_key", ["dedupe_key"], unique=True)


def downgrade() -> None:
    # No safe inverse for deleted duplicates; keep as no-op.
    pass
