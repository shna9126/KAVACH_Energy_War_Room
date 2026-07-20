from datetime import datetime, timezone

from agents.policy_agent import PolicyAgentConfig, generate_policy_plan
from agents.procurement_agent import ProcurementAgentConfig, generate_procurement_plan
from digital_twin.graph_state import (
    Country,
    CrudeGrade,
    DigitalTwinState,
    Port,
    PricePoint,
    Refinery,
    Route,
    SupplierCapacity,
)
from ingestion.storage import RecommendationRecord, SimulationRecord


def _sample_sim() -> SimulationRecord:
    return SimulationRecord(
        id=7,
        hypothesis_id=3,
        horizon="1wk",
        percentiles={"disruption_prob": 0.58, "price_shock_pct": 0.11, "duration_days": 12},
        distribution=None,
        metadata={"source": "test"},
    )


def _minimal_twin() -> DigitalTwinState:
    """Smallest twin that satisfies procurement's real-data requirement."""
    return DigitalTwinState(
        countries=[
            Country(iso3="IND", name="India", role="consumer"),
            Country(iso3="SAU", name="Saudi Arabia", role="producer"),
        ],
        ports=[
            Port(id="port_sikka", name="Sikka", country_iso3="IND", lat=22.4, lon=69.8),
            Port(id="port_ras_tanura", name="Ras Tanura", country_iso3="SAU", lat=26.6, lon=50.1),
        ],
        routes=[
            Route(
                id="route_sau_ind",
                origin_port_id="port_ras_tanura",
                destination_port_id="port_sikka",
                distance_nm=1550,
                transit_days=6,
            ),
        ],
        crude_grades=[
            CrudeGrade(id="grade_brent", name="Brent", source_country_iso3="USA"),
            CrudeGrade(id="grade_arab_light", name="Arab Light", source_country_iso3="SAU"),
        ],
        refineries=[
            Refinery(
                id="ref_jamnagar",
                name="Jamnagar",
                operator="Reliance",
                country_iso3="IND",
                capacity_kbd=1240,
                compatible_grade_ids=["grade_arab_light"],
            ),
        ],
        supplier_capacities=[
            SupplierCapacity(country_iso3="SAU", grade_id="grade_arab_light", spare_capacity_kbd=1500.0),
        ],
        prices=[
            PricePoint(grade_id="grade_brent", price_usd_per_bbl=85.0, as_of=datetime(2026, 7, 14, tzinfo=timezone.utc)),
            PricePoint(grade_id="grade_arab_light", price_usd_per_bbl=84.0, as_of=datetime(2026, 7, 14, tzinfo=timezone.utc)),
        ],
    )


def test_generate_procurement_plan() -> None:
    sim = _sample_sim()
    econ = RecommendationRecord(
        id=1,
        simulation_id=7,
        recommendation_type="economic_impact",
        recommendation_payload={"economic_impact": {"cpi_delta_pct": 1.2, "cad_delta_pct_of_gdp": 0.03}},
        score=0.7,
    )
    rec = generate_procurement_plan(sim, econ, ProcurementAgentConfig(demand_kbd=1800), twin=_minimal_twin())
    assert rec.recommendation_type == "procurement_plan"
    proc = rec.recommendation_payload["procurement"]
    assert proc["universe_source"] == "digital_twin"
    assert proc["demand_kbd"] == 1800
    assert proc["secured_kbd"] >= 0
    assert 0.0 <= (rec.score or 0.0) <= 1.0


def test_generate_policy_plan() -> None:
    sim = _sample_sim()
    procurement = RecommendationRecord(
        id=2,
        simulation_id=7,
        recommendation_type="procurement_plan",
        recommendation_payload={"procurement": {"demand_kbd": 1800, "secured_kbd": 1500, "gap_kbd": 300}},
        score=0.6,
    )
    rec = generate_policy_plan(sim, procurement, PolicyAgentConfig(max_spr_draw_mbd=4.5, strategic_reserve_days=30))
    assert rec.recommendation_type == "policy_plan"
    policy = rec.recommendation_payload["policy"]
    assert policy["recommended_spr_draw_mbd_day1"] >= 0
    assert policy["schedule_days"] >= 1
    assert len(policy["schedule"]) == policy["schedule_days"]
    assert 0.0 <= (rec.score or 0.0) <= 1.0
