"""API routes exposing the Digital Twin state.

Definition of Done for PRD v2 Upgrade 1:
    GET /digital-twin/state    returns a full snapshot
    GET /digital-twin/summary  returns per-slice row counts (cheap health check)
"""
from __future__ import annotations

import os

from fastapi import APIRouter

from digital_twin import build_digital_twin
from digital_twin.graph_state import DigitalTwinState


router = APIRouter(prefix="/digital-twin", tags=["digital-twin"])


@router.get("/state", response_model=DigitalTwinState)
def get_state() -> DigitalTwinState:
    database_url = os.getenv("DATABASE_URL", "").strip() or None
    return build_digital_twin(database_url, enable_live_enrichers=False)


@router.get("/summary")
def get_summary() -> dict:
    database_url = os.getenv("DATABASE_URL", "").strip() or None
    twin = build_digital_twin(database_url, enable_live_enrichers=False)
    return {
        "as_of_utc": twin.as_of_utc.isoformat(),
        "branch_id": twin.branch_id,
        "counts": twin.summary(),
        "provenance": [p.model_dump(mode="json") for p in twin.provenance],
    }
