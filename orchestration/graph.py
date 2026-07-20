from __future__ import annotations

import hashlib
import os
import time
import uuid
from datetime import datetime, timezone

# Module-level cache to avoid rebuilding the Digital Twin on every pipeline run.
# Each database_url maps to (twin_object, build_timestamp). TTL = 5 minutes.
_twin_cache: dict = {}
_TWIN_CACHE_TTL = 300

from agents.economic_agent import EconomicAgentConfig, generate_economic_impact
from agents.hypothesis_agent import HypothesisAgentConfig, generate_hypothesis
from agents.policy_agent import PolicyAgentConfig, generate_policy_plan
from agents.procurement_agent import ProcurementAgentConfig, generate_procurement_plan
from agents.reasoning_chain import (
    attach_and_enforce,
    build_chain_from_hypothesis,
)
from agents.redteam_agent import RedTeamAgentConfig, generate_redteam_review
from agents.refinery_agent import RefineryAgentConfig, assess_refinery_impact
from agents.scenario_agent import ScenarioAgentConfig, generate_simulations
from digital_twin import build_digital_twin
from ingestion.storage import (
    append_hypotheses,
    append_hypothesis_reviews,
    append_recommendations,
    append_simulations,
    fetch_hypothesis_by_structured_event_id,
    fetch_hypothesis_review_by_hypothesis_id,
    fetch_live_market_context,
    fetch_recommendation_map_by_simulation,
    fetch_recommendations_by_type,
    fetch_simulations_by_hypothesis_id,
    fetch_structured_event_by_id,
    SimulationRecord,
)
from orchestration.state import PipelineState


def _first_new_ids(before_ids: set[int], after_ids: list[int]) -> list[int]:
    return [x for x in after_ids if x not in before_ids]


def run_pipeline_for_structured_event(
    database_url: str,
    structured_event_id: int,
    mission_objective: str = "balanced_resilience",
    annual_import_budget_usd_bn: float | None = None,
) -> PipelineState:
    event = fetch_structured_event_by_id(database_url, structured_event_id)
    if event is None:
        raise ValueError(f"Structured event {structured_event_id} not found")

    state = PipelineState(
        pipeline_id=str(uuid.uuid4()),
        structured_event_id=structured_event_id,
        mission_objective=mission_objective,
    )

    # Build the Digital Twin snapshot once; every downstream reasoning step
    # consumes this world-model rather than raw storage (PRD v2 Upgrade 1+2).
    # Re-use a cached twin (TTL=5min) to avoid rebuilding the full world-model
    # on every pipeline trigger, which was the main latency bottleneck.
    _now = time.monotonic()
    _cached = _twin_cache.get(database_url)
    if _cached and (_now - _cached[1]) < _TWIN_CACHE_TTL:
        twin = _cached[0]
    else:
        twin = build_digital_twin(database_url)
        _twin_cache[database_url] = (twin, _now)

    # 1) Hypothesis — fetch live market context from DB to enrich the prompt
    live_ctx = fetch_live_market_context(database_url)
    hypothesis_cfg = HypothesisAgentConfig(
        mode=os.getenv("HYPOTHESIS_MODE", "auto"),
        api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        model=os.getenv("HYPOTHESIS_MODEL", "gemini-2.5-pro"),
        timeout_seconds=int(os.getenv("HYPOTHESIS_TIMEOUT_SECONDS", "45")),
        mission_objective=mission_objective,
    )
    hypothesis_input = generate_hypothesis(event, hypothesis_cfg, twin, live_ctx)
    append_hypotheses(database_url, [hypothesis_input])
    hypothesis = fetch_hypothesis_by_structured_event_id(database_url, structured_event_id)
    if hypothesis is None:
        raise RuntimeError("Failed to persist hypothesis")

    state.hypothesis_id = hypothesis.id
    state.hypothesis_text = hypothesis.hypothesis_text
    state.hypothesis_confidence = hypothesis.confidence

    # Re-derive a structured causal chain if the persisted row lacks one
    # (e.g. old rows or fallbacks that didn't have twin context).
    causal_chain = build_chain_from_hypothesis(hypothesis, event, twin)

    # 2) Red-team
    redteam_cfg = RedTeamAgentConfig(
        mode=os.getenv("REDTEAM_MODE", "auto"),
        api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        model=os.getenv("REDTEAM_MODEL", "gemini-2.5-pro"),
        timeout_seconds=int(os.getenv("REDTEAM_TIMEOUT_SECONDS", "45")),
        mission_objective=mission_objective,
    )
    review_input = generate_redteam_review(hypothesis, redteam_cfg)
    append_hypothesis_reviews(database_url, [review_input])
    review = fetch_hypothesis_review_by_hypothesis_id(database_url, hypothesis.id)
    if review is None:
        raise RuntimeError("Failed to persist hypothesis review")

    state.rebuttal_id = review.id
    state.rebuttal_text = review.rebuttal_text
    state.counter_confidence = review.counter_confidence
    state.reconciled_confidence = review.reconciled_confidence

    if hypothesis.confidence is not None and review.reconciled_confidence is not None:
        delta = abs(float(hypothesis.confidence) - float(review.reconciled_confidence))
        state.confidence_delta = round(delta, 4)
        threshold = float(os.getenv("DISAGREEMENT_THRESHOLD", "0.2"))
        state.disagreement = delta > threshold

    # 3) Scenario
    scenario_cfg = ScenarioAgentConfig(
        num_simulations=int(os.getenv("SCENARIO_NUM_SIMULATIONS", "10000")),
        base_seed=int(os.getenv("SCENARIO_RANDOM_SEED", "42")),
        mission_objective=mission_objective,
    )
    # Mix pipeline_id into the Monte Carlo seed so each refresh produces fresh
    # distributions even when the underlying hypothesis is unchanged. Prevents
    # the UI feeling stale ("same forecast on every refresh"). Toggle off via
    # SCENARIO_JITTER_PER_RUN=false to restore fully reproducible behavior.
    if os.getenv("SCENARIO_JITTER_PER_RUN", "true").strip().lower() in {"1", "true", "yes", "on"}:
        run_offset = int(
            hashlib.blake2b(state.pipeline_id.encode("utf-8"), digest_size=4).hexdigest(),
            16,
        )
        scenario_cfg.base_seed = (scenario_cfg.base_seed + run_offset) & 0x7FFFFFFF
    before_sim_ids = {s.id for s in fetch_simulations_by_hypothesis_id(database_url, hypothesis.id)}
    sims_input = generate_simulations(hypothesis, scenario_cfg)
    # allow_duplicates=True so every pipeline run persists a fresh cohort of
    # Monte Carlo results per horizon; otherwise the storage layer would
    # dedupe on (hypothesis_id, horizon) and silently drop the new sims,
    # leaving the UI reading stale percentiles from the very first run.
    append_simulations(database_url, sims_input, allow_duplicates=True)
    all_sims = fetch_simulations_by_hypothesis_id(database_url, hypothesis.id)
    new_sim_ids = _first_new_ids(before_sim_ids, [s.id for s in all_sims])
    if new_sim_ids:
        new_id_set = set(new_sim_ids)
        sims = [s for s in all_sims if s.id in new_id_set]
    else:
        # Fallback: keep only the most recent simulation per horizon so
        # downstream agents don't reprocess historical cohorts.
        latest_per_horizon: dict[str, SimulationRecord] = {}
        for s in all_sims:
            latest_per_horizon[s.horizon] = s  # rows arrive ordered by id asc
        sims = list(latest_per_horizon.values())
    state.simulation_ids = [s.id for s in sims]
    sim_ids = state.simulation_ids

    # 3b) Refinery impact — one report per simulation, feeds the frontend
    # war-room panel and provides context for downstream agents.
    refinery_cfg = RefineryAgentConfig(mission_objective=mission_objective)
    refinery_before = fetch_recommendations_by_type(database_url, "refinery_impact")
    refinery_before_ids = {r.id for r in refinery_before}
    refinery_inputs = [assess_refinery_impact(sim, twin, refinery_cfg) for sim in sims]
    refinery_inputs = attach_and_enforce(refinery_inputs, causal_chain)
    append_recommendations(database_url, refinery_inputs)
    refinery_after = fetch_recommendations_by_type(database_url, "refinery_impact")
    new_refinery_ids = _first_new_ids(refinery_before_ids, [r.id for r in refinery_after])
    if new_refinery_ids:
        state.refinery_recommendation_ids = new_refinery_ids
    else:
        refinery_map_after = fetch_recommendation_map_by_simulation(database_url, "refinery_impact", sim_ids)
        state.refinery_recommendation_ids = sorted([r.id for r in refinery_map_after.values()])

    # 4) Economic
    # User can override the annual import budget from the UI (mission control).
    # Falls back to the env default (ECON_ANNUAL_IMPORT_BILL_USD_BN) then 220.
    if annual_import_budget_usd_bn is not None and float(annual_import_budget_usd_bn) > 0:
        annual_bill = float(annual_import_budget_usd_bn)
    else:
        annual_bill = float(os.getenv("ECON_ANNUAL_IMPORT_BILL_USD_BN", "220"))
    eco_cfg = EconomicAgentConfig(
        annual_import_bill_usd_bn=annual_bill,
        nominal_gdp_usd_bn=float(os.getenv("ECON_NOMINAL_GDP_USD_BN", "4000")),
        pass_through_to_cpi=float(os.getenv("ECON_PASS_THROUGH_TO_CPI", "0.22")),
        mission_objective=mission_objective,
    )
    eco_before = fetch_recommendations_by_type(database_url, "economic_impact")
    eco_before_ids = {r.id for r in eco_before}
    eco_inputs = [generate_economic_impact(sim, eco_cfg) for sim in sims]
    eco_inputs = attach_and_enforce(eco_inputs, causal_chain)
    append_recommendations(database_url, eco_inputs)
    eco_after = fetch_recommendations_by_type(database_url, "economic_impact")
    new_eco_ids = _first_new_ids(eco_before_ids, [r.id for r in eco_after])
    if new_eco_ids:
        state.economic_recommendation_ids = new_eco_ids
    else:
        eco_map_after = fetch_recommendation_map_by_simulation(database_url, "economic_impact", sim_ids)
        state.economic_recommendation_ids = sorted([r.id for r in eco_map_after.values()])

    # 5) Procurement
    proc_cfg = ProcurementAgentConfig(
        demand_kbd=float(os.getenv("PROCUREMENT_DEMAND_KBD", "1800")),
        mission_objective=mission_objective,
    )
    proc_before = fetch_recommendations_by_type(database_url, "procurement_plan")
    proc_before_ids = {r.id for r in proc_before}
    eco_map = fetch_recommendation_map_by_simulation(database_url, "economic_impact", sim_ids)
    proc_inputs = [generate_procurement_plan(sim, eco_map.get(sim.id), proc_cfg, twin) for sim in sims]
    proc_inputs = attach_and_enforce(proc_inputs, causal_chain)
    append_recommendations(database_url, proc_inputs)
    proc_after = fetch_recommendations_by_type(database_url, "procurement_plan")
    new_proc_ids = _first_new_ids(proc_before_ids, [r.id for r in proc_after])
    if new_proc_ids:
        state.procurement_recommendation_ids = new_proc_ids
    else:
        proc_map_after = fetch_recommendation_map_by_simulation(database_url, "procurement_plan", sim_ids)
        state.procurement_recommendation_ids = sorted([r.id for r in proc_map_after.values()])

    # 6) Policy
    policy_cfg = PolicyAgentConfig(
        max_spr_draw_mbd=float(os.getenv("POLICY_MAX_SPR_DRAW_MBD", "4.5")),
        strategic_reserve_days=int(os.getenv("POLICY_STRATEGIC_RESERVE_DAYS", "30")),
        mission_objective=mission_objective,
    )
    policy_before = fetch_recommendations_by_type(database_url, "policy_plan")
    policy_before_ids = {r.id for r in policy_before}
    proc_map = fetch_recommendation_map_by_simulation(database_url, "procurement_plan", sim_ids)
    policy_inputs = [generate_policy_plan(sim, proc_map.get(sim.id), policy_cfg, twin) for sim in sims]
    policy_inputs = attach_and_enforce(policy_inputs, causal_chain)
    append_recommendations(database_url, policy_inputs)
    policy_after = fetch_recommendations_by_type(database_url, "policy_plan")
    new_policy_ids = _first_new_ids(policy_before_ids, [r.id for r in policy_after])
    if new_policy_ids:
        state.policy_recommendation_ids = new_policy_ids
    else:
        policy_map_after = fetch_recommendation_map_by_simulation(database_url, "policy_plan", sim_ids)
        state.policy_recommendation_ids = sorted([r.id for r in policy_map_after.values()])

    state.completed_at_utc = datetime.now(timezone.utc).isoformat()
    return state
