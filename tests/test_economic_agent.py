from agents.economic_agent import EconomicAgentConfig, generate_economic_impact
from ingestion.storage import SimulationRecord


def test_generate_economic_impact_outputs() -> None:
    sim = SimulationRecord(
        id=1,
        hypothesis_id=10,
        horizon="1wk",
        percentiles={"disruption_prob": 0.6, "price_shock_pct": 0.12, "duration_days": 14},
        distribution=None,
        metadata={},
    )
    cfg = EconomicAgentConfig(annual_import_bill_usd_bn=220, nominal_gdp_usd_bn=4000, pass_through_to_cpi=0.22)
    rec = generate_economic_impact(sim, cfg)

    assert rec.simulation_id == 1
    assert rec.recommendation_type == "economic_impact"
    payload = rec.recommendation_payload
    assert payload["economic_impact"]["import_bill_delta_usd_bn"] > 0
    assert payload["economic_impact"]["cad_delta_pct_of_gdp"] > 0
    assert payload["economic_impact"]["cpi_delta_pct"] > 0
    assert rec.score is not None
