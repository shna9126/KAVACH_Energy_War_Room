"""Immutable branching for what-if simulation.

`branch_for_scenario` returns a *new* `DigitalTwinState` with targeted
overrides applied. The original (live) state is never mutated, which is the
Definition of Done for PRD v2 Upgrade 4 — every what-if creates a temporary
Digital Twin branch and the live state remains unchanged.

Supported overrides are intentionally narrow in this first pass; they can be
extended as agents demand more surgical mutations without changing callers.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from digital_twin.graph_state import (
    Chokepoint,
    DigitalTwinState,
    Port,
    ProvenanceEntry,
    Refinery,
    Route,
)


class SupplierCapacityOverride(BaseModel):
    country_iso3: str
    grade_id: str | None = None
    spare_capacity_kbd: float


class ScenarioOverrides(BaseModel):
    """Declarative what-if perturbations applied to the twin."""

    chokepoint_status: dict[str, str] = Field(default_factory=dict)  # cp_id -> status
    chokepoint_risk: dict[str, float] = Field(default_factory=dict)  # cp_id -> 0..1
    port_status: dict[str, str] = Field(default_factory=dict)  # port_id -> status
    port_congestion_pct: dict[str, float] = Field(default_factory=dict)
    refinery_status: dict[str, str] = Field(default_factory=dict)  # refinery_id -> status
    refinery_utilization_pct: dict[str, float] = Field(default_factory=dict)
    route_insurance_multiplier: dict[str, float] = Field(default_factory=dict)  # route_id -> mult
    route_risk_score: dict[str, float] = Field(default_factory=dict)
    supplier_spare_capacity_kbd: list[SupplierCapacityOverride] = Field(default_factory=list)
    price_shock_pct: dict[str, float] = Field(default_factory=dict)  # grade_id -> +/- pct
    notes: list[str] = Field(default_factory=list)


def _apply_chokepoint(cp: Chokepoint, o: ScenarioOverrides) -> Chokepoint:
    updates: dict[str, Any] = {}
    if cp.id in o.chokepoint_status:
        updates["status"] = o.chokepoint_status[cp.id]
    if cp.id in o.chokepoint_risk:
        updates["risk_score"] = max(0.0, min(1.0, o.chokepoint_risk[cp.id]))
    return cp.model_copy(update=updates) if updates else cp


def _apply_port(p: Port, o: ScenarioOverrides) -> Port:
    updates: dict[str, Any] = {}
    if p.id in o.port_status:
        updates["status"] = o.port_status[p.id]
    if p.id in o.port_congestion_pct:
        updates["congestion_pct"] = max(0.0, min(100.0, o.port_congestion_pct[p.id]))
    return p.model_copy(update=updates) if updates else p


def _apply_refinery(r: Refinery, o: ScenarioOverrides) -> Refinery:
    updates: dict[str, Any] = {}
    if r.id in o.refinery_status:
        updates["status"] = o.refinery_status[r.id]
    if r.id in o.refinery_utilization_pct:
        updates["utilization_pct"] = max(0.0, min(1.0, o.refinery_utilization_pct[r.id]))
    return r.model_copy(update=updates) if updates else r


def _apply_route(r: Route, o: ScenarioOverrides) -> Route:
    updates: dict[str, Any] = {}
    if r.id in o.route_insurance_multiplier:
        updates["insurance_premium_multiplier"] = max(0.0, o.route_insurance_multiplier[r.id])
    if r.id in o.route_risk_score:
        updates["risk_score"] = max(0.0, min(1.0, o.route_risk_score[r.id]))
    return r.model_copy(update=updates) if updates else r


def branch_for_scenario(
    state: DigitalTwinState,
    overrides: ScenarioOverrides | dict[str, Any],
    *,
    branch_id: str | None = None,
) -> DigitalTwinState:
    """Return a deep-copied `DigitalTwinState` with `overrides` applied.

    The input `state` is not modified. The returned branch has a fresh
    `branch_id` and records its parent in `parent_branch_id`.
    """
    if isinstance(overrides, dict):
        overrides = ScenarioOverrides.model_validate(overrides)

    base = state.model_copy(deep=True)

    chokepoints = [_apply_chokepoint(c, overrides) for c in base.chokepoints]
    ports = [_apply_port(p, overrides) for p in base.ports]
    refineries = [_apply_refinery(r, overrides) for r in base.refineries]
    routes = [_apply_route(r, overrides) for r in base.routes]

    supplier_capacities = []
    supplier_override_map = {
        (o.country_iso3, o.grade_id): o.spare_capacity_kbd
        for o in overrides.supplier_spare_capacity_kbd
    }
    for cap in base.supplier_capacities:
        key = (cap.country_iso3, cap.grade_id)
        if key in supplier_override_map:
            supplier_capacities.append(
                cap.model_copy(update={"spare_capacity_kbd": max(0.0, supplier_override_map[key])})
            )
        else:
            supplier_capacities.append(cap)

    prices = []
    for price in base.prices:
        pct = overrides.price_shock_pct.get(price.grade_id)
        if pct is None:
            prices.append(price)
        else:
            new_price = max(0.0, price.price_usd_per_bbl * (1.0 + pct / 100.0))
            prices.append(price.model_copy(update={"price_usd_per_bbl": new_price}))

    override_summary = overrides.model_dump()
    non_empty = {k: v for k, v in override_summary.items() if v}
    provenance = list(base.provenance) + [
        ProvenanceEntry(
            slice_name="branch",
            source=f"override:{sorted(non_empty.keys())}",
            row_count=sum(len(v) if hasattr(v, "__len__") else 1 for v in non_empty.values()),
        )
    ]

    return base.model_copy(
        update={
            "branch_id": branch_id or f"scenario-{uuid.uuid4().hex[:8]}",
            "parent_branch_id": state.branch_id,
            "as_of_utc": datetime.now(timezone.utc),
            "chokepoints": chokepoints,
            "ports": ports,
            "refineries": refineries,
            "routes": routes,
            "supplier_capacities": supplier_capacities,
            "prices": prices,
            "provenance": provenance,
        }
    )
