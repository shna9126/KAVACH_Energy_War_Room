"""What-if Simulation Engine (PRD v2 Upgrade 4).

Every scenario creates a *temporary Digital Twin branch* via
`digital_twin.simulation_state.branch_for_scenario`. The live twin is never
mutated. On the branch we:

    1. escalate the hypothesis' confidence to reflect the scenario intensity
    2. re-run Monte Carlo (`agents.scenario_agent.generate_simulations`)
    3. re-run procurement (`agents.procurement_agent.generate_procurement_plan`)
    4. re-run twin-aware policy plan (`agents.policy_agent.generate_policy_plan`)
    5. attach the reasoning chain to every emitted recommendation

Preset scenarios enumerated in the PRD:

    close_hormuz              close Strait of Hormuz, spike Brent, kill Gulf routes
    saudi_output_boost        raise Saudi Arab Light spare capacity
    russia_export_cut         cut Russian spare capacity by pct
    insurance_shock           multiply insurance premium on Gulf/Red-Sea routes
    port_closure              close a specific port
    refinery_offline          take a refinery offline
    demand_shock              raise Indian import demand by pct
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field

from agents.policy_agent import PolicyAgentConfig, generate_policy_plan
from agents.procurement_agent import ProcurementAgentConfig, generate_procurement_plan
from agents.reasoning_chain import attach_and_enforce, build_causal_chain
from agents.refinery_agent import RefineryAgentConfig, assess_refinery_impact
from agents.scenario_agent import ScenarioAgentConfig, generate_simulations
from digital_twin.graph_state import DigitalTwinState
from digital_twin.simulation_state import (
    ScenarioOverrides,
    SupplierCapacityOverride,
    branch_for_scenario,
)
from ingestion.storage import (
    HypothesisRecord,
    RecommendationInput,
    RecommendationRecord,
    SimulationInput,
    SimulationRecord,
    StructuredEventRecord,
)


# ---------------------------------------------------------------------------
# Config + result models
# ---------------------------------------------------------------------------


@dataclass
class WhatIfConfig:
    horizon: str = "1wk"                  # which simulation horizon to use
    num_simulations: int = 4000            # a bit lighter for interactive latency
    base_seed: int = 4242
    demand_kbd: float = 1800.0
    max_spr_draw_mbd: float = 4.5
    strategic_reserve_days: int = 30
    mission_objective: str = "balanced_resilience"


class TwinDelta(BaseModel):
    chokepoints_changed: list[dict[str, Any]] = Field(default_factory=list)
    ports_changed: list[dict[str, Any]] = Field(default_factory=list)
    refineries_changed: list[dict[str, Any]] = Field(default_factory=list)
    routes_changed: list[dict[str, Any]] = Field(default_factory=list)
    suppliers_changed: list[dict[str, Any]] = Field(default_factory=list)
    prices_changed: list[dict[str, Any]] = Field(default_factory=list)


class WhatIfResult(BaseModel):
    scenario_name: str
    scenario_description: str
    branch_id: str
    parent_branch_id: str | None
    applied_overrides: dict[str, Any]
    twin_delta: TwinDelta
    scenario_percentiles: dict[str, Any]
    procurement: dict[str, Any]
    procurement_score: float | None
    policy: dict[str, Any]
    policy_score: float | None
    refinery: dict[str, Any]
    refinery_score: float | None
    confidence_used: float
    live_state_touched: bool = False  # invariant — always False


# ---------------------------------------------------------------------------
# Preset builders
# ---------------------------------------------------------------------------


def preset_close_hormuz(_: dict[str, Any] | None = None) -> tuple[ScenarioOverrides, str, float]:
    overrides = ScenarioOverrides(
        chokepoint_status={"cp_hormuz": "closed"},
        chokepoint_risk={"cp_hormuz": 0.95},
        route_insurance_multiplier={
            "route_rastanura_sikka": 3.5,
            "route_basrah_vadinar": 3.5,
            "route_kharg_mangalore": 3.5,
        },
        route_risk_score={
            "route_rastanura_sikka": 0.9,
            "route_basrah_vadinar": 0.9,
            "route_kharg_mangalore": 0.95,
        },
        price_shock_pct={"grade_brent": 35.0},
        notes=["Strait of Hormuz closed — all Gulf transits blocked."],
    )
    return overrides, "Strait of Hormuz closed to tanker traffic", 0.92


def preset_saudi_output_boost(params: dict[str, Any] | None) -> tuple[ScenarioOverrides, str, float]:
    boost_kbd = float((params or {}).get("boost_kbd", 500.0))
    overrides = ScenarioOverrides(
        supplier_spare_capacity_kbd=[
            SupplierCapacityOverride(country_iso3="SAU", grade_id="grade_arab_light", spare_capacity_kbd=1500.0 + boost_kbd),
            SupplierCapacityOverride(country_iso3="SAU", grade_id="grade_arab_medium", spare_capacity_kbd=800.0 + boost_kbd * 0.5),
        ],
        price_shock_pct={"grade_brent": -6.0},
        notes=[f"Saudi Arabia raises spare capacity by {boost_kbd:.0f} kbd."],
    )
    return overrides, f"Saudi Arabia lifts spare output by {boost_kbd:.0f} kbd", 0.5


def preset_russia_export_cut(params: dict[str, Any] | None) -> tuple[ScenarioOverrides, str, float]:
    pct = float((params or {}).get("pct", 40.0))
    factor = max(0.0, 1.0 - pct / 100.0)
    overrides = ScenarioOverrides(
        supplier_spare_capacity_kbd=[
            SupplierCapacityOverride(country_iso3="RUS", grade_id="grade_urals", spare_capacity_kbd=1200.0 * factor),
            SupplierCapacityOverride(country_iso3="RUS", grade_id="grade_espo", spare_capacity_kbd=400.0 * factor),
        ],
        price_shock_pct={"grade_brent": pct * 0.25, "grade_urals": pct * 0.4},
        notes=[f"Russia cuts export volumes by {pct:.0f}%."],
    )
    return overrides, f"Russia cuts exports by {pct:.0f}%", 0.7


def preset_insurance_shock(params: dict[str, Any] | None) -> tuple[ScenarioOverrides, str, float]:
    multiplier = float((params or {}).get("multiplier", 2.0))
    overrides = ScenarioOverrides(
        route_insurance_multiplier={
            "route_rastanura_sikka": multiplier,
            "route_basrah_vadinar": multiplier,
            "route_kharg_mangalore": multiplier,
            "route_fujairah_mundra": multiplier * 0.7,
            "route_novorossiysk_paradip": multiplier * 1.2,
        },
        price_shock_pct={"grade_brent": (multiplier - 1.0) * 4.0},
        notes=[f"Insurance premium multiplier set to {multiplier:.2f}× across Gulf/Red-Sea corridors."],
    )
    return overrides, f"Insurance premiums {multiplier:.1f}× on Gulf/Red-Sea routes", 0.65


def preset_port_closure(params: dict[str, Any] | None) -> tuple[ScenarioOverrides, str, float]:
    port_id = str((params or {}).get("port_id", "port_ras_tanura"))
    overrides = ScenarioOverrides(
        port_status={port_id: "closed"},
        port_congestion_pct={port_id: 100.0},
        notes=[f"Port {port_id} closed."],
    )
    return overrides, f"Port {port_id} closed", 0.6


def preset_refinery_offline(params: dict[str, Any] | None) -> tuple[ScenarioOverrides, str, float]:
    refinery_id = str((params or {}).get("refinery_id", "ref_jamnagar"))
    overrides = ScenarioOverrides(
        refinery_status={refinery_id: "offline"},
        refinery_utilization_pct={refinery_id: 0.0},
        notes=[f"Refinery {refinery_id} taken offline."],
    )
    return overrides, f"Refinery {refinery_id} offline", 0.55


def preset_demand_shock(params: dict[str, Any] | None) -> tuple[ScenarioOverrides, str, float]:
    pct = float((params or {}).get("pct", 8.0))
    overrides = ScenarioOverrides(
        # Demand isn't a twin field per-se; we express upstream shock via a
        # small Brent premium and note the demand delta for the caller to
        # apply to procurement config.
        price_shock_pct={"grade_brent": pct * 0.5},
        notes=[f"Indian import demand +{pct:.1f}%."],
    )
    return overrides, f"Indian import demand +{pct:.1f}%", 0.5


PresetBuilder = Callable[[dict[str, Any] | None], tuple[ScenarioOverrides, str, float]]


PRESET_BUILDERS: dict[str, PresetBuilder] = {
    "close_hormuz": preset_close_hormuz,
    "saudi_output_boost": preset_saudi_output_boost,
    "russia_export_cut": preset_russia_export_cut,
    "insurance_shock": preset_insurance_shock,
    "port_closure": preset_port_closure,
    "refinery_offline": preset_refinery_offline,
    "demand_shock": preset_demand_shock,
}


PRESET_METADATA: dict[str, dict[str, Any]] = {
    "close_hormuz": {"params": {}, "description": "Close Strait of Hormuz to tanker traffic."},
    "saudi_output_boost": {"params": {"boost_kbd": 500.0}, "description": "Boost Saudi Arabia spare output (kbd)."},
    "russia_export_cut": {"params": {"pct": 40.0}, "description": "Cut Russian export volumes by pct."},
    "insurance_shock": {"params": {"multiplier": 2.0}, "description": "Multiply insurance premium on Gulf/Red-Sea routes."},
    "port_closure": {"params": {"port_id": "port_ras_tanura"}, "description": "Close a specific port by id."},
    "refinery_offline": {"params": {"refinery_id": "ref_jamnagar"}, "description": "Take a specific refinery offline."},
    "demand_shock": {"params": {"pct": 8.0}, "description": "Raise Indian import demand by pct."},
}


def list_presets() -> list[dict[str, Any]]:
    return [
        {"name": name, **meta}
        for name, meta in PRESET_METADATA.items()
    ]


# ---------------------------------------------------------------------------
# Twin delta computation
# ---------------------------------------------------------------------------


def _diff_lists(live: list[Any], branch: list[Any], key_attr: str, watch: list[str]) -> list[dict[str, Any]]:
    live_map = {getattr(x, key_attr): x for x in live}
    diffs: list[dict[str, Any]] = []
    for b in branch:
        k = getattr(b, key_attr)
        l = live_map.get(k)
        if l is None:
            continue
        entry: dict[str, Any] = {key_attr: k}
        changed = False
        for attr in watch:
            lv = getattr(l, attr, None)
            bv = getattr(b, attr, None)
            if lv != bv:
                entry[attr] = {"live": lv, "branch": bv}
                changed = True
        if changed:
            diffs.append(entry)
    return diffs


def compute_twin_delta(live: DigitalTwinState, branch: DigitalTwinState) -> TwinDelta:
    return TwinDelta(
        chokepoints_changed=_diff_lists(live.chokepoints, branch.chokepoints, "id", ["status", "risk_score"]),
        ports_changed=_diff_lists(live.ports, branch.ports, "id", ["status", "congestion_pct"]),
        refineries_changed=_diff_lists(
            live.refineries, branch.refineries, "id", ["status", "utilization_pct"]
        ),
        routes_changed=_diff_lists(
            live.routes, branch.routes, "id", ["insurance_premium_multiplier", "risk_score"]
        ),
        suppliers_changed=[
            {
                "country_iso3": bs.country_iso3,
                "grade_id": bs.grade_id,
                "spare_capacity_kbd": {"live": ls.spare_capacity_kbd, "branch": bs.spare_capacity_kbd},
            }
            for ls, bs in zip(live.supplier_capacities, branch.supplier_capacities)
            if ls.spare_capacity_kbd != bs.spare_capacity_kbd
        ],
        prices_changed=[
            {
                "grade_id": bp.grade_id,
                "price_usd_per_bbl": {"live": lp.price_usd_per_bbl, "branch": bp.price_usd_per_bbl},
            }
            for lp, bp in zip(live.prices, branch.prices)
            if lp.price_usd_per_bbl != bp.price_usd_per_bbl
        ][:20],
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def resolve_scenario(
    name: str, params: dict[str, Any] | None
) -> tuple[ScenarioOverrides, str, float]:
    builder = PRESET_BUILDERS.get(name)
    if builder is None:
        raise ValueError(f"Unknown what-if scenario preset: {name!r}. Options: {sorted(PRESET_BUILDERS)}")
    return builder(params)


def _pick_simulation(sims: list[SimulationInput], horizon: str) -> SimulationInput:
    for s in sims:
        if s.horizon == horizon:
            return s
    return sims[0]


def _sim_input_to_record(sim: SimulationInput, hypothesis_id: int) -> SimulationRecord:
    """Turn a fresh (not-yet-persisted) SimulationInput into a Record so the
    downstream agents (which expect a Record) can consume it in-memory.
    """
    return SimulationRecord(
        id=-1,  # not persisted; downstream agents don't rely on the id
        hypothesis_id=hypothesis_id,
        horizon=sim.horizon,
        percentiles=sim.percentiles,
        distribution=sim.distribution,
        metadata=sim.metadata,
    )


def _rec_input_to_record(rec: RecommendationInput) -> RecommendationRecord:
    return RecommendationRecord(
        id=-1,
        simulation_id=rec.simulation_id,
        recommendation_type=rec.recommendation_type,
        recommendation_payload=rec.recommendation_payload,
        score=rec.score,
    )


def run_whatif(
    live_twin: DigitalTwinState,
    hypothesis: HypothesisRecord,
    event: StructuredEventRecord | None,
    scenario_name: str,
    scenario_params: dict[str, Any] | None = None,
    cfg: WhatIfConfig | None = None,
) -> WhatIfResult:
    """Run one what-if scenario against a branched twin.

    - The live twin is not mutated (guaranteed by `branch_for_scenario`).
    - No writes to persistent storage occur — this is a pure read+compute path.
    """
    cfg = cfg or WhatIfConfig()
    overrides, description, escalated_confidence = resolve_scenario(scenario_name, scenario_params)

    branch = branch_for_scenario(live_twin, overrides)
    delta = compute_twin_delta(live_twin, branch)

    # Escalate hypothesis confidence so the scenario agent produces a shock
    # profile consistent with the branch state.
    scenario_conf = max(
        float(hypothesis.confidence or 0.6), escalated_confidence
    )
    escalated_hypothesis = HypothesisRecord(
        id=hypothesis.id,
        structured_event_id=hypothesis.structured_event_id,
        hypothesis_text=hypothesis.hypothesis_text,
        confidence=scenario_conf,
        reasoning_chain=hypothesis.reasoning_chain,
        reasoning_chain_json=hypothesis.reasoning_chain_json,
        model_name=hypothesis.model_name,
    )

    sim_cfg = ScenarioAgentConfig(
        num_simulations=cfg.num_simulations,
        base_seed=cfg.base_seed,
        mission_objective=cfg.mission_objective,
    )
    sims = generate_simulations(escalated_hypothesis, sim_cfg)
    picked = _pick_simulation(sims, cfg.horizon)
    sim_record = _sim_input_to_record(picked, escalated_hypothesis.id)

    # For demand-shock preset, escalate demand.
    demand_kbd = cfg.demand_kbd
    if scenario_name == "demand_shock":
        pct = float((scenario_params or {}).get("pct", 8.0))
        demand_kbd = demand_kbd * (1.0 + pct / 100.0)

    proc_input = generate_procurement_plan(
        sim_record,
        None,
        ProcurementAgentConfig(
            demand_kbd=demand_kbd,
            mission_objective=cfg.mission_objective,
        ),
        twin=branch,
    )
    proc_record = _rec_input_to_record(proc_input)

    policy_input = generate_policy_plan(
        sim_record,
        proc_record,
        PolicyAgentConfig(
            max_spr_draw_mbd=cfg.max_spr_draw_mbd,
            strategic_reserve_days=cfg.strategic_reserve_days,
            default_import_demand_kbd=demand_kbd,
            mission_objective=cfg.mission_objective,
        ),
        twin=branch,
    )

    refinery_input = assess_refinery_impact(
        sim_record,
        branch,
        RefineryAgentConfig(mission_objective=cfg.mission_objective),
    )

    # Attach the reasoning chain — build it against the branch so entity
    # attribution reflects the scenario, not the live world.
    if event is not None:
        chain = build_causal_chain(
            event,
            branch,
            hypothesis_text=hypothesis.hypothesis_text,
            confidence=scenario_conf,
            raw_reasoning_steps=hypothesis.reasoning_chain,
            source="hybrid",
        )
        enriched = attach_and_enforce([proc_input, policy_input, refinery_input], chain)
        proc_input, policy_input, refinery_input = enriched[0], enriched[1], enriched[2]

    return WhatIfResult(
        scenario_name=scenario_name,
        scenario_description=description,
        branch_id=branch.branch_id,
        parent_branch_id=branch.parent_branch_id,
        applied_overrides=overrides.model_dump(),
        twin_delta=delta,
        scenario_percentiles=picked.percentiles,
        procurement=proc_input.recommendation_payload,
        procurement_score=proc_input.score,
        policy=policy_input.recommendation_payload,
        policy_score=policy_input.score,
        refinery=refinery_input.recommendation_payload,
        refinery_score=refinery_input.score,
        confidence_used=scenario_conf,
        live_state_touched=False,
    )


__all__ = [
    "WhatIfConfig",
    "WhatIfResult",
    "TwinDelta",
    "PRESET_BUILDERS",
    "PRESET_METADATA",
    "list_presets",
    "resolve_scenario",
    "compute_twin_delta",
    "run_whatif",
]
