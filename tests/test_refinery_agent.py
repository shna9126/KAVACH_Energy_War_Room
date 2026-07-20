from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents.refinery_agent import RefineryAgentConfig, assess_refinery_impact
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
from ingestion.storage import SimulationRecord


def _sim(disruption: float = 0.6, duration_days: float = 10.0) -> SimulationRecord:
    return SimulationRecord(
        id=555,
        hypothesis_id=1,
        horizon="1wk",
        percentiles={
            "disruption_prob": disruption,
            "duration_days": duration_days,
            "price_shock_pct": 0.14,
        },
        distribution=None,
        metadata={"source": "test"},
    )


def _twin_with_two_refineries(hormuz_closed: bool = False) -> DigitalTwinState:
    return DigitalTwinState(
        countries=[
            Country(iso3="IND", name="India", role="consumer"),
            Country(iso3="SAU", name="Saudi Arabia", role="producer"),
            Country(iso3="IRQ", name="Iraq", role="producer"),
            Country(iso3="RUS", name="Russia", role="producer"),
        ],
        ports=[
            Port(id="port_ras_tanura", name="Ras Tanura", country_iso3="SAU", lat=26.6, lon=50.1),
            Port(id="port_basrah", name="Basrah", country_iso3="IRQ", lat=29.6, lon=48.8),
            Port(id="port_novorossiysk", name="Novorossiysk", country_iso3="RUS", lat=44.7, lon=37.8),
            Port(id="port_sikka", name="Sikka", country_iso3="IND", lat=22.4, lon=69.8),
            Port(id="port_vizag", name="Visakhapatnam", country_iso3="IND", lat=17.7, lon=83.2),
            Port(id="port_paradip", name="Paradip", country_iso3="IND", lat=20.3, lon=86.6),
        ],
        chokepoints=[
            Chokepoint(
                id="cp_hormuz",
                name="Strait of Hormuz",
                lat=26.5,
                lon=56.2,
                throughput_mbd=20.5,
                status="closed" if hormuz_closed else "open",
            ),
        ],
        routes=[
            Route(
                id="route_saudi_sikka",
                origin_port_id="port_ras_tanura",
                destination_port_id="port_sikka",
                chokepoint_ids=["cp_hormuz"],
                distance_nm=1550,
                transit_days=6,
            ),
            Route(
                id="route_basrah_vizag",
                origin_port_id="port_basrah",
                destination_port_id="port_vizag",
                chokepoint_ids=["cp_hormuz"],
                distance_nm=1800,
                transit_days=7,
            ),
            Route(
                id="route_russia_paradip",
                origin_port_id="port_novorossiysk",
                destination_port_id="port_paradip",
                chokepoint_ids=[],
                distance_nm=6200,
                transit_days=22,
            ),
        ],
        crude_grades=[
            CrudeGrade(id="grade_arab_light", name="Arab Light", source_country_iso3="SAU"),
            CrudeGrade(id="grade_basrah_medium", name="Basrah Medium", source_country_iso3="IRQ"),
            CrudeGrade(id="grade_urals", name="Urals", source_country_iso3="RUS"),
        ],
        refineries=[
            Refinery(
                id="ref_jamnagar",
                name="Jamnagar",
                operator="Reliance",
                country_iso3="IND",
                capacity_kbd=1240.0,
                compatible_grade_ids=["grade_arab_light", "grade_basrah_medium", "grade_urals"],
                utilization_pct=0.90,
            ),
            Refinery(
                id="ref_vizag",
                name="Visakhapatnam",
                operator="HPCL",
                country_iso3="IND",
                capacity_kbd=166.0,
                compatible_grade_ids=["grade_basrah_medium"],  # Gulf-only
                utilization_pct=0.85,
            ),
        ],
        spr_sites=[],
        supplier_capacities=[
            SupplierCapacity(country_iso3="SAU", grade_id="grade_arab_light", spare_capacity_kbd=1500.0),
            SupplierCapacity(country_iso3="IRQ", grade_id="grade_basrah_medium", spare_capacity_kbd=700.0),
            SupplierCapacity(country_iso3="RUS", grade_id="grade_urals", spare_capacity_kbd=1200.0),
        ],
        prices=[
            PricePoint(grade_id="grade_arab_light", price_usd_per_bbl=84.0, as_of=datetime(2026, 7, 14, tzinfo=timezone.utc)),
            PricePoint(grade_id="grade_basrah_medium", price_usd_per_bbl=79.0, as_of=datetime(2026, 7, 14, tzinfo=timezone.utc)),
            PricePoint(grade_id="grade_urals", price_usd_per_bbl=72.0, as_of=datetime(2026, 7, 14, tzinfo=timezone.utc)),
        ],
    )


def test_refinery_impact_without_twin_returns_deferred_payload():
    rec = assess_refinery_impact(_sim(), twin=None)
    payload = rec.recommendation_payload["refinery_impact"]
    assert rec.recommendation_type == "refinery_impact"
    assert payload["refineries"] == []
    assert payload["aggregate"]["refinery_count"] == 0
    assert "deferred" in payload["notes"].lower()


def test_refinery_impact_baseline_with_healthy_twin():
    twin = _twin_with_two_refineries(hormuz_closed=False)
    rec = assess_refinery_impact(_sim(disruption=0.3, duration_days=5.0), twin=twin)
    payload = rec.recommendation_payload["refinery_impact"]

    assert payload["aggregate"]["refinery_count"] == 2
    assert payload["aggregate"]["throughput_loss_kbd"] >= 0

    by_id = {r["refinery_id"]: r for r in payload["refineries"]}
    jamnagar = by_id["ref_jamnagar"]
    vizag = by_id["ref_vizag"]

    # Recommended crude is cheapest available compatible grade
    # (Urals @ $72 for Jamnagar; Basrah Medium @ $79 for Vizag)
    assert jamnagar["recommended_crude"]["grade_id"] == "grade_urals"
    assert vizag["recommended_crude"]["grade_id"] == "grade_basrah_medium"

    # No refinery is starved when all corridors are open
    assert jamnagar["starved"] is False
    assert vizag["starved"] is False


def test_refinery_impact_hormuz_closure_starves_gulf_only_refinery():
    twin = _twin_with_two_refineries(hormuz_closed=True)
    rec = assess_refinery_impact(_sim(disruption=0.9, duration_days=14.0), twin=twin)
    payload = rec.recommendation_payload["refinery_impact"]
    by_id = {r["refinery_id"]: r for r in payload["refineries"]}

    # Vizag is Gulf-only (Basrah Medium via Hormuz) — should be starved.
    vizag = by_id["ref_vizag"]
    assert vizag["compatible_grades_available"] == 0
    assert vizag["starved"] is True
    assert vizag["recommended_crude"] is None
    assert vizag["feedstock_gap_days"] > 0
    assert vizag["expected_utilization_pct"] <= 25.0  # Floored at min_utilization_when_starved (0.20)

    # Jamnagar has Urals via Russia (no chokepoint), so it should keep running.
    jamnagar = by_id["ref_jamnagar"]
    assert jamnagar["compatible_grades_available"] >= 1
    assert jamnagar["starved"] is False
    assert jamnagar["recommended_crude"]["grade_id"] == "grade_urals"

    # Worst-hit refinery reported at aggregate
    assert payload["aggregate"]["worst_hit_refinery_id"] == "ref_vizag"
    # Throughput loss > 0
    assert payload["aggregate"]["throughput_loss_kbd"] > 0


def test_refinery_impact_excludes_sanctioned_source_country():
    twin = _twin_with_two_refineries(hormuz_closed=False)
    twin = twin.model_copy(update={
        "sanctions": [SanctionEntry(entity="Russia state entity", schema_type="Organization")]
    })
    rec = assess_refinery_impact(_sim(disruption=0.5, duration_days=8.0), twin=twin)
    payload = rec.recommendation_payload["refinery_impact"]
    jamnagar = next(r for r in payload["refineries"] if r["refinery_id"] == "ref_jamnagar")

    # Urals should now appear in unavailable_grades due to Russia sanctions
    unavailable_ids = {u["grade_id"] for u in jamnagar["unavailable_grades"]}
    assert "grade_urals" in unavailable_ids
    # Recommended crude is now the next cheapest available (Basrah Medium)
    assert jamnagar["recommended_crude"]["grade_id"] == "grade_basrah_medium"
    # Aggregate reports the sanctioned country
    assert "RUS" in payload["aggregate"]["sanctioned_countries_excluded"]


def test_refinery_impact_score_reflects_sector_health():
    twin = _twin_with_two_refineries(hormuz_closed=False)
    healthy = assess_refinery_impact(_sim(disruption=0.1, duration_days=2.0), twin=twin)
    twin_closed = _twin_with_two_refineries(hormuz_closed=True)
    stressed = assess_refinery_impact(_sim(disruption=0.9, duration_days=14.0), twin=twin_closed)
    assert healthy.score > stressed.score
    assert 0.0 <= (stressed.score or 0.0) <= 1.0
    assert 0.0 <= (healthy.score or 0.0) <= 1.0
