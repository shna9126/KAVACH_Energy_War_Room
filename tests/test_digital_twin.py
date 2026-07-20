from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from digital_twin import build_digital_twin, branch_for_scenario, refresh_digital_twin
from digital_twin.graph_state import PricePoint, SanctionEntry, TradeFlow
from digital_twin.simulation_state import ScenarioOverrides
from ingestion.schemas.raw_signal import RawSignal
from ingestion.storage import append_raw_signals, ensure_tables


@pytest.fixture()
def database_url(tmp_path):
    db_file = tmp_path / "twin_test.db"
    url = f"sqlite:///{db_file}"
    ensure_tables(url)
    return url


def _seed_price_row(database_url: str) -> None:
    signal = RawSignal.from_payload(
        source="world_bank_prices",
        raw_payload={"value": 85.5, "date": "2026", "indicator": {"value": "Brent"}},
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
        entities_hint=["Brent"],
        source_id="worldbank:2026",
    )
    append_raw_signals(database_url, [signal])


def _seed_sanction_row(database_url: str) -> None:
    signal = RawSignal.from_payload(
        source="opensanctions",
        raw_payload={
            "id": "test-sanctioned-vessel",
            "caption": "MV Sanctioned Tanker",
            "schema": "Vessel",
            "countries": ["us"],
            "datasets": ["us_ofac_sdn"],
        },
        timestamp=datetime(2026, 5, 1, tzinfo=timezone.utc),
        entities_hint=["MV Sanctioned Tanker"],
        source_id="test-sanctioned-vessel",
    )
    append_raw_signals(database_url, [signal])


def test_build_digital_twin_hydrates_static_topology(database_url):
    twin = build_digital_twin(database_url)

    counts = twin.summary()
    assert counts["countries"] >= 10
    assert counts["ports"] >= 12
    assert counts["chokepoints"] >= 4
    assert counts["routes"] >= 6
    assert counts["refineries"] >= 6
    assert counts["spr_sites"] == 3
    assert counts["crude_grades"] >= 8
    assert counts["supplier_capacities"] >= 6

    # Cross-slice integrity: every route's ports and chokepoints resolve
    port_ids = {p.id for p in twin.ports}
    cp_ids = {c.id for c in twin.chokepoints}
    for route in twin.routes:
        assert route.origin_port_id in port_ids
        assert route.destination_port_id in port_ids
        for cp in route.chokepoint_ids:
            assert cp in cp_ids

    # Refinery grade compatibility resolves
    grade_ids = {g.id for g in twin.crude_grades}
    for refinery in twin.refineries:
        for grade in refinery.compatible_grade_ids:
            assert grade in grade_ids


def test_build_digital_twin_pulls_dynamic_slices_from_sql(database_url):
    _seed_price_row(database_url)
    _seed_sanction_row(database_url)

    twin = build_digital_twin(database_url)

    assert len(twin.prices) >= 1
    assert twin.prices[0].price_usd_per_bbl == pytest.approx(85.5)
    assert twin.prices[0].grade_id == "grade_brent"

    assert len(twin.sanctions) >= 1
    assert twin.sanctions[0].entity == "MV Sanctioned Tanker"

    # Provenance records data source per slice
    provenance = {p.slice_name: p for p in twin.provenance}
    assert "SqlPriceProvider" in provenance["prices"].source or "sql:raw_signals" in provenance["prices"].source
    assert provenance["prices"].row_count == len(twin.prices)


def test_branch_for_scenario_does_not_mutate_live_state(database_url):
    _seed_price_row(database_url)
    twin = build_digital_twin(database_url)

    original_hormuz = twin.chokepoint_by_id("cp_hormuz")
    assert original_hormuz is not None
    original_status = original_hormuz.status
    original_risk = original_hormuz.risk_score
    original_price = twin.latest_price("grade_brent")
    assert original_price is not None
    original_price_value = original_price.price_usd_per_bbl

    branch = branch_for_scenario(
        twin,
        ScenarioOverrides(
            chokepoint_status={"cp_hormuz": "closed"},
            chokepoint_risk={"cp_hormuz": 0.95},
            price_shock_pct={"grade_brent": 40.0},
            notes=["hormuz-closure-test"],
        ),
    )

    # Branch reflects the overrides
    branch_hormuz = branch.chokepoint_by_id("cp_hormuz")
    assert branch_hormuz is not None
    assert branch_hormuz.status == "closed"
    assert branch_hormuz.risk_score == pytest.approx(0.95)
    branch_price = branch.latest_price("grade_brent")
    assert branch_price is not None
    assert branch_price.price_usd_per_bbl == pytest.approx(original_price_value * 1.40)

    # Live twin is untouched
    live_hormuz = twin.chokepoint_by_id("cp_hormuz")
    assert live_hormuz is not None
    assert live_hormuz.status == original_status
    assert live_hormuz.risk_score == pytest.approx(original_risk)
    live_price = twin.latest_price("grade_brent")
    assert live_price is not None
    assert live_price.price_usd_per_bbl == pytest.approx(original_price_value)

    # Branch tracks lineage
    assert branch.branch_id != twin.branch_id
    assert branch.parent_branch_id == twin.branch_id


def test_refresh_digital_twin_keeps_topology_and_updates_dynamic(database_url):
    twin = build_digital_twin(database_url)
    assert twin.prices == []

    _seed_price_row(database_url)
    refreshed = refresh_digital_twin(twin, database_url)

    assert len(refreshed.prices) >= 1
    assert len(refreshed.ports) == len(twin.ports)
    assert len(refreshed.refineries) == len(twin.refineries)
    assert refreshed.as_of_utc >= twin.as_of_utc


def test_digital_twin_api_route(database_url, monkeypatch):
    _seed_price_row(database_url)
    monkeypatch.setenv("DATABASE_URL", database_url)

    from api.main import app

    client = TestClient(app)
    response = client.get("/digital-twin/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["counts"]["countries"] >= 10
    assert body["counts"]["prices"] >= 1

    state_response = client.get("/digital-twin/state")
    assert state_response.status_code == 200
    state_body = state_response.json()
    assert state_body["branch_id"] == "live"
    assert len(state_body["chokepoints"]) >= 4
