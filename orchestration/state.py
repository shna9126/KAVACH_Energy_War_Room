from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class PipelineState(BaseModel):
    pipeline_id: str
    started_at_utc: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    structured_event_id: int
    mission_objective: str = "balanced_resilience"

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
