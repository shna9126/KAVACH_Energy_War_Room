from agents.scenario_agent import HORIZON_DAYS, ScenarioAgentConfig, generate_simulations
from ingestion.storage import HypothesisRecord


def _sample_hypothesis() -> HypothesisRecord:
    return HypothesisRecord(
        id=1,
        structured_event_id=10,
        hypothesis_text="Supply disruption likely in shipping corridor",
        confidence=0.7,
        reasoning_chain=["event", "impact"],
        model_name="deterministic_v1",
    )


def test_generate_simulations_shape_and_ranges() -> None:
    hypo = _sample_hypothesis()
    cfg = ScenarioAgentConfig(num_simulations=500, base_seed=7)
    sims = generate_simulations(hypo, cfg)

    assert len(sims) == len(HORIZON_DAYS)
    horizons = {s.horizon for s in sims}
    assert horizons == set(HORIZON_DAYS.keys())

    for sim in sims:
        p = sim.percentiles
        assert 0.0 <= p["disruption_prob"] <= 1.0
        assert 0.0 <= p["price_shock_pct"] <= 1.0
        assert p["duration_days"] >= 1.0
