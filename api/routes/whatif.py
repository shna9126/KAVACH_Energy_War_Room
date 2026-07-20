from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from agents.procurement_agent import ProcurementAgentConfig, generate_procurement_plan
from agents.whatif_engine import (
    WhatIfConfig,
    list_presets,
    run_whatif,
)
from api.auth import require_api_key
from api.schemas import (
    WhatIfPresetItem,
    WhatIfRequest,
    WhatIfResponse,
    WhatIfScenarioRequest,
    WhatIfScenarioResponse,
)
from digital_twin import build_digital_twin
from ingestion.storage import (
    RecommendationInput,
    SimulationRecord,
    SimulationRow,
    append_recommendations,
    fetch_hypothesis_by_id,
    fetch_recommendation_map_by_simulation,
    fetch_structured_event_by_id,
    get_engine,
)


router = APIRouter(prefix="/whatif", tags=["whatif"])


@router.post("", response_model=WhatIfResponse)
def run_whatif_legacy(
    payload: WhatIfRequest,
    _: None = Depends(require_api_key),
) -> WhatIfResponse:
    """Legacy demand-only what-if. Preserved for backward compatibility with the
    original frontend. Prefer POST /whatif/scenario for PRD v2 behavior.
    """
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise HTTPException(status_code=400, detail="DATABASE_URL missing")

    simulation_id = payload.simulation_id
    demand_kbd = payload.demand_kbd

    engine = get_engine(database_url)
    with engine.connect() as conn:
        row = conn.execute(
            select(
                SimulationRow.id,
                SimulationRow.hypothesis_id,
                SimulationRow.horizon,
                SimulationRow.percentiles,
                SimulationRow.distribution,
                SimulationRow.metadata_json.label("metadata_json"),
            ).where(SimulationRow.id == simulation_id)
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="simulation_id not found")

    sim = SimulationRecord(
        id=row.id,
        hypothesis_id=row.hypothesis_id,
        horizon=row.horizon,
        percentiles=row.percentiles or {},
        distribution=row.distribution,
        metadata=row.metadata_json or {},
    )
    eco_map = fetch_recommendation_map_by_simulation(database_url, "economic_impact", [simulation_id])
    cfg = ProcurementAgentConfig(
        demand_kbd=float(demand_kbd),
        mission_objective=payload.mission_objective or "balanced_resilience",
    )
    twin = build_digital_twin(database_url)
    proc_input = generate_procurement_plan(sim, eco_map.get(simulation_id), cfg, twin)

    whatif_input = RecommendationInput(
        simulation_id=proc_input.simulation_id,
        recommendation_type="procurement_whatif",
        recommendation_payload=proc_input.recommendation_payload,
        score=proc_input.score,
    )
    append_recommendations(database_url, [whatif_input])

    return WhatIfResponse(
        simulation_id=simulation_id,
        demand_kbd=demand_kbd,
        mission_objective=payload.mission_objective,
        recommendation=proc_input.recommendation_payload,
        score=proc_input.score,
    )


@router.get("/scenarios", response_model=list[WhatIfPresetItem])
def get_scenarios() -> list[WhatIfPresetItem]:
    return [WhatIfPresetItem(**p) for p in list_presets()]


def _resolve_hypothesis_id(database_url: str, payload: WhatIfScenarioRequest) -> int:
    if payload.hypothesis_id is not None:
        return payload.hypothesis_id
    if payload.simulation_id is not None:
        engine = get_engine(database_url)
        with engine.connect() as conn:
            row = conn.execute(
                select(SimulationRow.hypothesis_id).where(SimulationRow.id == payload.simulation_id)
            ).first()
        if row is None or row.hypothesis_id is None:
            raise HTTPException(status_code=404, detail="simulation_id has no linked hypothesis")
        return int(row.hypothesis_id)
    raise HTTPException(status_code=400, detail="Provide hypothesis_id or simulation_id")


@router.post("/scenario", response_model=WhatIfScenarioResponse)
def run_whatif_scenario(
    payload: WhatIfScenarioRequest,
    _: None = Depends(require_api_key),
) -> WhatIfScenarioResponse:
    """Run a full twin-branched what-if scenario.

    Never mutates the live twin or persists to recommendation tables — this is a
    pure read+compute path so demos can interrogate arbitrary shocks live.
    """
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise HTTPException(status_code=400, detail="DATABASE_URL missing")

    hypothesis_id = _resolve_hypothesis_id(database_url, payload)
    hypothesis = fetch_hypothesis_by_id(database_url, hypothesis_id)
    if hypothesis is None:
        raise HTTPException(status_code=404, detail="hypothesis_id not found")

    event = None
    if hypothesis.structured_event_id is not None:
        event = fetch_structured_event_by_id(database_url, hypothesis.structured_event_id)

    live_twin = build_digital_twin(database_url)

    try:
        result = run_whatif(
            live_twin=live_twin,
            hypothesis=hypothesis,
            event=event,
            scenario_name=payload.scenario_name,
            scenario_params=payload.scenario_params,
            cfg=WhatIfConfig(
                demand_kbd=payload.demand_kbd,
                num_simulations=payload.num_simulations,
                mission_objective=payload.mission_objective or "balanced_resilience",
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return WhatIfScenarioResponse(
        scenario_name=result.scenario_name,
        scenario_description=result.scenario_description,
        branch_id=result.branch_id,
        parent_branch_id=result.parent_branch_id,
        applied_overrides=result.applied_overrides,
        twin_delta=result.twin_delta.model_dump(),
        scenario_percentiles=result.scenario_percentiles,
        procurement=result.procurement,
        procurement_score=result.procurement_score,
        policy=result.policy,
        policy_score=result.policy_score,
        refinery=result.refinery,
        refinery_score=result.refinery_score,
        confidence_used=result.confidence_used,
        live_state_touched=result.live_state_touched,
    )
