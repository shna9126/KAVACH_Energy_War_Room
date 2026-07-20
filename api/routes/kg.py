from __future__ import annotations

import os

from fastapi import APIRouter
from sqlalchemy import select

from api.schemas import KgHistoryItem, KgHistoryResponse
from ingestion.storage import StructuredEventRow, get_engine


router = APIRouter(prefix="/kg", tags=["kg"])


@router.get("/history", response_model=KgHistoryResponse)
def get_node_history(node: str, limit: int = 50) -> KgHistoryResponse:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return KgHistoryResponse(node=node, history=[])

    needle = node.strip().lower()
    engine = get_engine(database_url)
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                StructuredEventRow.id,
                StructuredEventRow.event_ts,
                StructuredEventRow.action_type,
                StructuredEventRow.target,
                StructuredEventRow.actors,
            )
            .order_by(StructuredEventRow.event_ts.desc())
            .limit(limit * 3)
        ).all()

    history: list[KgHistoryItem] = []
    for row in rows:
        actors = row.actors or []
        actors_l = [str(a).lower() for a in actors]
        target_l = (row.target or "").lower()
        if needle in target_l or any(needle in a for a in actors_l):
            history.append(
                KgHistoryItem(
                    structured_event_id=row.id,
                    event_ts=row.event_ts,
                    action_type=row.action_type,
                    target=row.target,
                    actors=actors,
                )
            )
        if len(history) >= limit:
            break

    return KgHistoryResponse(node=node, history=history)
