from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents.procurement_agent import ProcurementAgentConfig, generate_procurement_plan
from digital_twin.graph_state import (
    Chokepoint,
    Country,
    CrudeGrade,
    DigitalTwinState,
    Port,
    PricePoint,
    Refinery,
    Route,
    SanctionEntry,
    SupplierCapacity,
)
from ingestion.storage import RecommendationRecord, SimulationRecord


def _sim(disruption: float = 0.5, price_shock: float = 0.10) -> SimulationRecord:
    return SimulationRecord(
        id=321,
        hypothesis_id=1,
        horizon="1wk",
        percentiles={"disruption_prob": disruption, "price_shock_pct": price_shock, "duration_days": 8},
        distribution=None,
        metadata={"source": "test"},
    )


def _base_twin(hormuz_closed: bool = False, sikka_closed: bool = False) -> DigitalTwinState:
    return DigitalTwinState(
        countries=[
            Country(iso3="IND", name="India", role="consumer"),
            Country(iso3="SAU", name="Saudi Arabia", role="producer"),
            Country(iso3="IRQ", name="Iraq", role="producer"),
            Country(iso3="RUS", name="Russia", role="producer"),
            Country(iso3="ARE", name="United Arab Emirates", role="producer"),
        ],
        ports=[
            Port(
                id="port_ras_tanura", name="Ras Tanura", country_iso3="SAU", lat=26.6, lon=50.1,
            ),
            Port(id="port_basrah", name="Basrah", country_iso3="IRQ", lat=29.6, lon=48.8),
            Port(id="port_fujairah", name="Fujairah", country_iso3="ARE", lat=25.1, lon=56.3),
            Port(id="port_novorossiysk", name="Novorossiysk", country_iso3="RUS", lat=44.7, lon=37.8),
            Port(
                id="port_sikka", name="Sikka", country_iso3="IND", lat=22.4, lon=69.8,
                status="closed" if sikka_closed else "open",
            ),
            Port(id="port_paradip", name="Paradip", country_iso3="IND", lat=20.3, lon=86.6),
        ],
        chokepoints=[
            Chokepoint(
                id="cp_hormuz", name="Strait of Hormuz", lat=26.5, lon=56.2,
                throughput_mbd=20.5, risk_score=0.35,
                status="closed" if hormuz_closed else "open",
            ),
        ],
        routes=[
            Route(
                id="route_saudi_sikka", origin_port_id="port_ras_tanura",
                destination_port_id="port_sikka", chokepoint_ids=["cp_hormuz"],
                distance_nm=1550, transit_days=6, insurance_premium_multiplier=1.0,
            ),
            Route(
                id="route_basrah_sikka", origin_port_id="port_basrah",
                destination_port_id="port_sikka", chokepoint_ids=["cp_hormuz"],
                distance_nm=1650, transit_days=6.5, insurance_premium_multiplier=1.0,
            ),
            Route(
                id="route_fujairah_paradip", origin_port_id="port_fujairah",
                destination_port_id="port_paradip", chokepoint_ids=[],
                distance_nm=2200, transit_days=8, insurance_premium_multiplier=1.0,
            ),
            Route(
                id="route_russia_paradip", origin_port_id="port_novorossiysk",
                destination_port_id="port_paradip", chokepoint_ids=[],
                distance_nm=6200, transit_days=22, insurance_premium_multiplier=1.1,
            ),
        ],
        crude_grades=[
            CrudeGrade(id="grade_brent", name="Brent", source_country_iso3="USA"),
            CrudeGrade(id="grade_arab_light", name="Arab Light", source_country_iso3="SAU"),
            CrudeGrade(id="grade_basrah_medium", name="Basrah Medium", source_country_iso3="IRQ"),
            CrudeGrade(id="grade_murban", name="Murban", source_country_iso3="ARE"),
            CrudeGrade(id="grade_urals", name="Urals", source_country_iso3="RUS"),
        ],
        refineries=[
            Refinery(
                id="ref_jamnagar", name="Jamnagar", operator="Reliance", country_iso3="IND",
                capacity_kbd=1240, utilization_pct=0.9,
                compatible_grade_ids=["grade_arab_light", "grade_basrah_medium", "grade_urals", "grade_murban"],
            ),
            Refinery(
                id="ref_paradip", name="Paradip", operator="IOCL", country_iso3="IND",
                capacity_kbd=300, utilization_pct=0.85,
                compatible_grade_ids=["grade_basrah_medium", "grade_urals"],
            ),
        ],
        supplier_capacities=[
            SupplierCapacity(country_iso3="SAU", grade_id="grade_arab_light", spare_capacity_kbd=1500.0),
            SupplierCapacity(country_iso3="IRQ", grade_id="grade_basrah_medium", spare_capacity_kbd=700.0),
            SupplierCapacity(country_iso3="ARE", grade_id="grade_murban", spare_capacity_kbd=600.0),
            SupplierCapacity(country_iso3="RUS", grade_id="grade_urals", spare_capacity_kbd=1200.0),
        ],
        prices=[
            PricePoint(grade_id="grade_brent", price_usd_per_bbl=85.0, as_of=datetime(2026, 7, 14, tzinfo=timezone.utc)),
            PricePoint(grade_id="grade_arab_light", price_usd_per_bbl=84.0, as_of=datetime(2026, 7, 14, tzinfo=timezone.utc)),
            PricePoint(grade_id="grade_basrah_medium", price_usd_per_bbl=79.0, as_of=datetime(2026, 7, 14, tzinfo=timezone.utc)),
            PricePoint(grade_id="grade_murban", price_usd_per_bbl=86.0, as_of=datetime(2026, 7, 14, tzinfo=timezone.utc)),
            PricePoint(grade_id="grade_urals", price_usd_per_bbl=72.0, as_of=datetime(2026, 7, 14, tzinfo=timezone.utc)),
        ],
    )


def test_procurement_ranking_populated_with_scorecards_and_confidence():
    rec = generate_procurement_plan(_sim(), None, ProcurementAgentConfig(demand_kbd=1800), twin=_base_twin())
    payload = rec.recommendation_payload
    assert payload["procurement"]["universe_source"] == "digital_twin"
    ranking = payload["ranking"]
    assert ranking, "Ranking must be populated when twin has suppliers"

    for entry in ranking:
        assert entry["status"] in {"selected", "candidate", "rejected"}
        assert 0.0 <= entry["score"] <= 1.0
        assert 0.0 <= entry["confidence"] <= 1.0
        sc = entry["scorecard"]
        # PRD-mandated fields: Risk, Transit Time, Cost, Insurance, Port Status, Compatibility
        for k in (
            "risk_adjusted_cost",
            "transit_days",
            "cost_index",
            "insurance_multiplier",
            "port_status",
            "blend_compatibility_pct",
            "geopolitical_risk",
            "sanctions_risk",
        ):
            assert k in sc, f"scorecard missing {k}"
        # Constraint checklist has all five checks
        names = {c["name"] for c in entry["constraints"]}
        assert names >= {
            "destination_port_open",
            "sanctions_clear",
            "blend_compatible",
            "chokepoint_open",
            "spare_capacity_available",
        }
        assert entry["why_ranked"]


def test_procurement_hormuz_closure_rejects_gulf_routes_with_reasons():
    twin = _base_twin(hormuz_closed=True)
    rec = generate_procurement_plan(_sim(disruption=0.85), None, ProcurementAgentConfig(demand_kbd=1800), twin=twin)
    ranking = rec.recommendation_payload["ranking"]
    by_country = {(r["supplier_country_iso3"], r["grade_id"]): r for r in ranking}

    # Saudi + Iraq routes cross Hormuz → must be rejected
    saudi = by_country[("SAU", "grade_arab_light")]
    iraq = by_country[("IRQ", "grade_basrah_medium")]
    assert saudi["status"] == "rejected"
    assert iraq["status"] == "rejected"
    assert any("chokepoint" in reason.lower() for reason in saudi["rejected_reasons"])

    # UAE (Fujairah bypasses Hormuz) and Russia routes stay eligible
    uae = by_country[("ARE", "grade_murban")]
    russia = by_country[("RUS", "grade_urals")]
    assert uae["status"] in {"selected", "candidate"}
    assert russia["status"] in {"selected", "candidate"}

    # Selected suppliers should exclude rejected ones
    allocations = rec.recommendation_payload["procurement"]["allocations"]
    allocated_iso = {a["supplier_country_iso3"] for a in allocations}
    assert "SAU" not in allocated_iso and "IRQ" not in allocated_iso


def test_procurement_sanctioned_supplier_flagged_as_rejected():
    twin = _base_twin()
    twin = twin.model_copy(update={
        "sanctions": [SanctionEntry(entity="Russia state entity", schema_type="Organization", imposed_by=["US"])]
    })
    rec = generate_procurement_plan(_sim(), None, ProcurementAgentConfig(demand_kbd=1800), twin=twin)
    ranking = rec.recommendation_payload["ranking"]
    russia = next(r for r in ranking if r["supplier_country_iso3"] == "RUS")

    assert russia["status"] == "rejected"
    assert russia["scorecard"]["sanctions_risk"] >= 0.5
    assert any("sanctions" in reason.lower() for reason in russia["rejected_reasons"])
    # Sanctions detail should mention who imposed
    sanction_check = next(c for c in russia["constraints"] if c["name"] == "sanctions_clear")
    assert "imposed by" in sanction_check["detail"] or "US" in sanction_check["detail"]


def test_procurement_closed_destination_port_yields_rejection_reason():
    twin = _base_twin(sikka_closed=True)
    rec = generate_procurement_plan(_sim(), None, ProcurementAgentConfig(demand_kbd=1800), twin=twin)
    ranking = rec.recommendation_payload["ranking"]
    # Saudi + Iraq both route into Sikka in this twin
    saudi = next(r for r in ranking if r["supplier_country_iso3"] == "SAU")
    port_check = next(c for c in saudi["constraints"] if c["name"] == "destination_port_open")
    assert port_check["satisfied"] is False
    assert saudi["status"] == "rejected"
    assert any("port" in reason.lower() and "closed" in reason.lower() for reason in saudi["rejected_reasons"])


def test_procurement_requires_twin():
    """PRD v2 rejects the old hardcoded 5-supplier universe. Procurement must
    always score against real Digital Twin data — the call raises a clear
    error if no twin is supplied.
    """
    with pytest.raises(TypeError):
        generate_procurement_plan(_sim(), None, ProcurementAgentConfig(demand_kbd=1800))
    with pytest.raises(ValueError, match="Digital Twin"):
        generate_procurement_plan(_sim(), None, ProcurementAgentConfig(demand_kbd=1800), twin=None)
