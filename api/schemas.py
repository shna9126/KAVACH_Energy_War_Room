from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str


class SignalItem(BaseModel):
    structured_event_id: int
    event_ts: datetime | None
    action_type: str
    target: str
    confidence: float
    actors: list[str] = Field(default_factory=list)
    source: str
    source_id: str | None


class AisVesselItem(BaseModel):
    mmsi: str
    name: str | None = None
    lat: float
    lon: float
    status: str | None = None
    sog: float | None = None
    cog: float | None = None
    heading: float | None = None
    source: str = "ais_stream"


class TriggerPipelineRequest(BaseModel):
    structured_event_id: int
    mission_objective: str | None = None
    annual_import_budget_usd_bn: float | None = None


class PipelineStateResponse(BaseModel):
    pipeline_id: str
    started_at_utc: str
    structured_event_id: int
    mission_objective: str | None = None
    hypothesis_id: int | None = None
    hypothesis_text: str | None = None
    hypothesis_confidence: float | None = None
    rebuttal_id: int | None = None
    rebuttal_text: str | None = None
    counter_confidence: float | None = None
    reconciled_confidence: float | None = None
    disagreement: bool = False
    confidence_delta: float | None = None
    simulation_ids: list[int] = Field(default_factory=list)
    economic_recommendation_ids: list[int] = Field(default_factory=list)
    procurement_recommendation_ids: list[int] = Field(default_factory=list)
    policy_recommendation_ids: list[int] = Field(default_factory=list)
    refinery_recommendation_ids: list[int] = Field(default_factory=list)
    completed_at_utc: str | None = None


class HypothesisDetails(BaseModel):
    id: int
    hypothesis_text: str
    confidence: float
    reasoning_chain: list[str] = Field(default_factory=list)
    causal_chain: dict[str, Any] | None = None


class RedTeamDetails(BaseModel):
    id: int
    rebuttal_text: str
    counter_confidence: float
    reconciled_confidence: float
    disproof_signals: list[str] = Field(default_factory=list)


class SimulationDetails(BaseModel):
    id: int
    horizon: str
    percentiles: dict[str, float] = Field(default_factory=dict)
    distribution: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecommendationDetails(BaseModel):
    id: int
    simulation_id: int | None = None
    recommendation_type: str
    recommendation_payload: dict[str, Any] = Field(default_factory=dict)
    score: float


class PipelineDetailsResponse(BaseModel):
    state: PipelineStateResponse
    hypothesis: HypothesisDetails | None = None
    redteam: RedTeamDetails | None = None
    simulations: list[SimulationDetails] = Field(default_factory=list)
    economic: list[RecommendationDetails] = Field(default_factory=list)
    procurement: list[RecommendationDetails] = Field(default_factory=list)
    policy: list[RecommendationDetails] = Field(default_factory=list)
    refinery: list[RecommendationDetails] = Field(default_factory=list)
    # Annual crude import budget (USD bn/yr) actually used by the Economic
    # Agent for this pipeline. UI-supplied value takes precedence over
    # ECON_ANNUAL_IMPORT_BILL_USD_BN; null when no economic recommendation ran.
    effective_import_budget_usd_bn: float | None = None


class WhatIfRequest(BaseModel):
    simulation_id: int
    demand_kbd: float = 1800
    mission_objective: str | None = None


class WhatIfResponse(BaseModel):
    simulation_id: int
    demand_kbd: float
    mission_objective: str | None = None
    recommendation: dict[str, Any]
    score: float


class WhatIfScenarioRequest(BaseModel):
    hypothesis_id: int | None = None
    simulation_id: int | None = None
    scenario_name: str
    scenario_params: dict[str, Any] = Field(default_factory=dict)
    demand_kbd: float = 1800.0
    num_simulations: int = 4000
    mission_objective: str | None = None


class WhatIfScenarioResponse(BaseModel):
    scenario_name: str
    scenario_description: str
    branch_id: str
    parent_branch_id: str | None = None
    applied_overrides: dict[str, Any] = Field(default_factory=dict)
    twin_delta: dict[str, Any] = Field(default_factory=dict)
    scenario_percentiles: dict[str, Any] = Field(default_factory=dict)
    procurement: dict[str, Any] = Field(default_factory=dict)
    procurement_score: float | None = None
    policy: dict[str, Any] = Field(default_factory=dict)
    policy_score: float | None = None
    refinery: dict[str, Any] = Field(default_factory=dict)
    refinery_score: float | None = None
    confidence_used: float
    live_state_touched: bool = False


class WhatIfPresetItem(BaseModel):
    name: str
    description: str
    params: dict[str, Any] = Field(default_factory=dict)


class KgHistoryItem(BaseModel):
    structured_event_id: int
    event_ts: datetime | None
    action_type: str
    target: str
    actors: list[str] = Field(default_factory=list)


class KgHistoryResponse(BaseModel):
    node: str
    history: list[KgHistoryItem] = Field(default_factory=list)


class BacktestRequest(BaseModel):
    start: datetime
    end: datetime


class BacktestRunItem(BaseModel):
    structured_event_id: int
    event_ts: datetime | None = None
    action_type: str | None = None
    target: str | None = None
    actors: list[str] = Field(default_factory=list)
    hypothesis_confidence: float | None = None
    reconciled_confidence: float | None = None
    disagreement: bool = False
    confidence_delta: float | None = None
    predicted_disruption_prob: float | None = None
    predicted_outcome: str | None = None
    actual_outcome: str | None = None
    matched: bool | None = None
    actual_evidence_count: int | None = None


class BacktestResponse(BaseModel):
    events: int
    runs: list[BacktestRunItem] = Field(default_factory=list)
    calibration_score: float | None = None
    disagreement_rate: float | None = None
    mean_hypothesis_confidence: float | None = None
    mean_reconciled_confidence: float | None = None
    accuracy_rate: float | None = None
    runtime_ms: int | None = None
    workers_used: int | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    message: str | None = None
