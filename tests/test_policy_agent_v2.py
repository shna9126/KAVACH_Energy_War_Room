from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents.policy_agent import PolicyAgentConfig, generate_policy_plan
from digital_twin.graph_state import (
    Country,
    CrudeGrade,
    DigitalTwinState,
    PricePoint,
    SPRSite,
    SupplierCapacity,
)
from ingestion.storage import RecommendationRecord, SimulationRecord


def _sim(disruption: float = 0.7, duration_days: float = 12.0) -> SimulationRecord:
    return SimulationRecord(
        id=101,
        hypothesis_id=1,
        horizon="1wk",
        percentiles={"disruption_prob": disruption, "duration_days": duration_days, "price_shock_pct": 0.18},
        distribution=None,
        metadata={"source": "test"},
    )


def _procurement_gap(gap_kbd: float = 400.0) -> RecommendationRecord:
    return RecommendationRecord(
        id=201,
        simulation_id=101,
        recommendation_type="procurement_plan",
        recommendation_payload={
            "procurement": {"demand_kbd": 1800.0, "secured_kbd": 1800.0 - gap_kbd, "gap_kbd": gap_kbd}
        },
        score=0.5,
    )


def _twin_with_prices_and_suppliers(spot_price: float = 85.0) -> DigitalTwinState:
    return DigitalTwinState(
        countries=[
            Country(iso3="IND", name="India", role="consumer"),
            Country(iso3="IRQ", name="Iraq", role="producer"),
            Country(iso3="SAU", name="Saudi Arabia", role="producer"),
            Country(iso3="RUS", name="Russia", role="producer"),
        ],
        crude_grades=[
            CrudeGrade(id="grade_brent", name="Brent", source_country_iso3="USA"),
            CrudeGrade(id="grade_basrah_medium", name="Basrah Medium", source_country_iso3="IRQ"),
            CrudeGrade(id="grade_arab_light", name="Arab Light", source_country_iso3="SAU"),
            CrudeGrade(id="grade_urals", name="Urals", source_country_iso3="RUS"),
        ],
        spr_sites=[
            SPRSite(id="spr_vizag", name="Vizag", country_iso3="IND", capacity_mbbl=9.77, current_fill_mbbl=9.77, max_drawdown_mbd=0.30),
            SPRSite(id="spr_mangalore", name="Mangalore", country_iso3="IND", capacity_mbbl=11.0, current_fill_mbbl=11.0, max_drawdown_mbd=0.35),
            SPRSite(id="spr_padur", name="Padur", country_iso3="IND", capacity_mbbl=17.0, current_fill_mbbl=17.0, max_drawdown_mbd=0.50),
        ],
        supplier_capacities=[
            # Cheapest — Iraq via Basrah Medium
            SupplierCapacity(country_iso3="IRQ", grade_id="grade_basrah_medium", spare_capacity_kbd=700.0),
            SupplierCapacity(country_iso3="SAU", grade_id="grade_arab_light", spare_capacity_kbd=1500.0),
            SupplierCapacity(country_iso3="RUS", grade_id="grade_urals", spare_capacity_kbd=1200.0),
        ],
        prices=[
            PricePoint(grade_id="grade_brent", price_usd_per_bbl=spot_price, as_of=datetime(2026, 7, 14, tzinfo=timezone.utc)),
            PricePoint(grade_id="grade_basrah_medium", price_usd_per_bbl=spot_price - 6.0, as_of=datetime(2026, 7, 14, tzinfo=timezone.utc)),
            PricePoint(grade_id="grade_arab_light", price_usd_per_bbl=spot_price - 2.0, as_of=datetime(2026, 7, 14, tzinfo=timezone.utc)),
            PricePoint(grade_id="grade_urals", price_usd_per_bbl=spot_price - 4.0, as_of=datetime(2026, 7, 14, tzinfo=timezone.utc)),
        ],
    )


def test_policy_plan_backward_compat_without_twin():
    """Existing signature (no twin) must still work — replenishment section
    is empty, reserve health is null, but drawdown schedule remains valid.
    """
    rec = generate_policy_plan(
        _sim(),
        _procurement_gap(),
        PolicyAgentConfig(max_spr_draw_mbd=4.5, strategic_reserve_days=30),
    )
    policy = rec.recommendation_payload["policy"]
    assert policy["schedule_days"] >= 1
    assert len(policy["schedule"]) == policy["schedule_days"]
    assert policy["replenishment"]["target_supplier_iso3"] is None
    assert rec.recommendation_payload["reserve_health"]["sites"] == []


def test_policy_plan_with_twin_populates_replenishment_and_reserve_health():
    twin = _twin_with_prices_and_suppliers(spot_price=85.0)
    rec = generate_policy_plan(
        _sim(disruption=0.7, duration_days=10),
        _procurement_gap(gap_kbd=600.0),
        PolicyAgentConfig(max_spr_draw_mbd=4.5, strategic_reserve_days=30),
        twin=twin,
    )

    policy = rec.recommendation_payload["policy"]
    refill = policy["replenishment"]

    # WHEN: refill scheduled after drawdown ends + cool-off
    assert refill["when_day"] == policy["schedule_days"] + 5

    # AT WHAT PRICE: trigger price is strictly below spot
    assert refill["trigger_price_usd_bbl"] is not None
    assert refill["trigger_price_usd_bbl"] < refill["spot_price_usd_bbl"]

    # FROM WHOM: cheapest supplier — Iraq (Basrah Medium at spot-6)
    assert refill["target_supplier_iso3"] == "IRQ"
    assert refill["target_grade_id"] == "grade_basrah_medium"

    # HOW MUCH: refill volume equals total drawdown
    assert refill["refill_volume_mbbl"] == pytest.approx(policy["total_draw_million_barrels"])

    # Refill schedule is populated with per-day allocations
    assert refill["refill_schedule"]
    assert all(entry["supplier_iso3"] == "IRQ" for entry in refill["refill_schedule"])

    # Cost + savings are positive
    assert refill["estimated_cost_usd_bn"] > 0
    assert refill["estimated_savings_vs_spot_usd_bn"] >= 0

    # Reserve health forecast: per-site breakdown, all sites present
    reserve = rec.recommendation_payload["reserve_health"]
    site_ids = {s["spr_site_id"] for s in reserve["sites"]}
    assert site_ids == {"spr_vizag", "spr_mangalore", "spr_padur"}
    assert reserve["days_of_import_cover_before"] > reserve["days_of_import_cover_after_drawdown"]
    assert reserve["after_refill_mbbl"] > reserve["after_drawdown_mbbl"]

    # Drawdown schedule includes per-site weighting
    for entry in policy["schedule"]:
        assert "per_site" in entry
        assert entry["per_site"], "Per-site drawdown weighting must be present when twin has SPR sites"

    # Score respects the new refill bonus without exceeding bounds
    assert 0.0 <= (rec.score or 0.0) <= 1.0


def test_policy_plan_no_gap_produces_empty_replenishment():
    twin = _twin_with_prices_and_suppliers()
    zero_gap = RecommendationRecord(
        id=1,
        simulation_id=101,
        recommendation_type="procurement_plan",
        recommendation_payload={"procurement": {"demand_kbd": 1800, "secured_kbd": 1800, "gap_kbd": 0.0}},
        score=0.9,
    )
    rec = generate_policy_plan(_sim(disruption=0.1, duration_days=5), zero_gap, PolicyAgentConfig(), twin=twin)
    refill = rec.recommendation_payload["policy"]["replenishment"]
    assert refill["refill_volume_mbbl"] == 0.0
    assert refill["target_supplier_iso3"] is None
    assert refill["refill_schedule"] == []
    assert "not required" in refill["rationale"].lower()


def test_policy_plan_excludes_sanctioned_country_from_refill():
    from digital_twin.graph_state import SanctionEntry

    twin = _twin_with_prices_and_suppliers(spot_price=85.0)
    # Add a country-level sanction on Iraq — should knock IRQ out of the refill ranking
    twin = twin.model_copy(update={
        "sanctions": [SanctionEntry(entity="Iraq state oil marketing organisation", schema_type="Organization")]
    })
    rec = generate_policy_plan(
        _sim(disruption=0.6),
        _procurement_gap(gap_kbd=500.0),
        PolicyAgentConfig(),
        twin=twin,
    )
    refill = rec.recommendation_payload["policy"]["replenishment"]
    assert "IRQ" in refill["excluded_countries"]
    assert refill["target_supplier_iso3"] != "IRQ"
