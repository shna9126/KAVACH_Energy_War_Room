from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from agents.hypothesis_agent import HypothesisAgentConfig, generate_hypothesis
from agents.whatif_engine import (
    PRESET_BUILDERS,
    WhatIfConfig,
    list_presets,
    run_whatif,
)
from digital_twin import build_digital_twin
from ingestion.schemas.raw_signal import RawSignal
from ingestion.storage import (
    HypothesisRecord,
    StructuredEventRecord,
    append_hypotheses,
    append_raw_signals,
    append_structured_events,
    ensure_tables,
    fetch_hypothesis_by_id,
    fetch_hypothesis_by_structured_event_id,
    fetch_unprocessed_raw_signals,
    StructuredEventInput,
)


@pytest.fixture()
def database_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'whatif.db'}"
    ensure_tables(url)
    return url


def _seed_hormuz_event_and_hypothesis(database_url: str) -> tuple[HypothesisRecord, StructuredEventRecord]:
    signal = RawSignal.from_payload(
        source="newsapi",
        raw_payload={"headline": "Iran threatens Hormuz"},
        timestamp=datetime(2026, 7, 12, tzinfo=timezone.utc),
        entities_hint=["Iran", "Hormuz"],
        source_id="iran-hormuz-1",
    )
    append_raw_signals(database_url, [signal])
    raw_rows = fetch_unprocessed_raw_signals(database_url, limit=5)
    raw_row = raw_rows[0]

    event_input = StructuredEventInput(
        raw_signal_id=raw_row.id,
        event_ts=raw_row.signal_ts,
        action_type="strait_closure_threat",
        target="strait_of_hormuz",
        confidence=0.7,
        actors=["Iran", "IRGC Navy"],
        extracted_payload={"headline": "Iran threatens Hormuz"},
    )
    append_structured_events(database_url, [event_input])

    from sqlalchemy import select

    from ingestion.storage import StructuredEventRow, get_engine

    engine = get_engine(database_url)
    with engine.connect() as conn:
        row = conn.execute(select(StructuredEventRow).order_by(StructuredEventRow.id.desc())).first()
    event = StructuredEventRecord(
        id=row.id,
        raw_signal_id=row.raw_signal_id,
        event_ts=row.event_ts,
        action_type=row.action_type,
        target=row.target,
        confidence=row.confidence,
        actors=row.actors or [],
        extracted_payload=row.extracted_payload or {},
    )

    twin = build_digital_twin(database_url)
    hypothesis_input = generate_hypothesis(event, HypothesisAgentConfig(mode="deterministic"), twin)
    append_hypotheses(database_url, [hypothesis_input])
    hypothesis = fetch_hypothesis_by_structured_event_id(database_url, event.id)
    assert hypothesis is not None
    return hypothesis, event


def test_list_presets_returns_all_seven():
    presets = list_presets()
    names = {p["name"] for p in presets}
    assert names == set(PRESET_BUILDERS.keys())
    assert "close_hormuz" in names


def test_run_whatif_close_hormuz_branches_without_mutating_live(database_url):
    hypothesis, event = _seed_hormuz_event_and_hypothesis(database_url)
    live_twin = build_digital_twin(database_url)
    live_hormuz_before = live_twin.chokepoint_by_id("cp_hormuz")
    assert live_hormuz_before is not None
    original_status = live_hormuz_before.status

    result = run_whatif(
        live_twin=live_twin,
        hypothesis=hypothesis,
        event=event,
        scenario_name="close_hormuz",
        scenario_params=None,
        cfg=WhatIfConfig(num_simulations=500),
    )

    # Result advertises the branch, not the live twin
    assert result.branch_id != live_twin.branch_id
    assert result.parent_branch_id == live_twin.branch_id
    assert result.live_state_touched is False

    # Twin delta reports the chokepoint change
    cp_changes = {c["id"] for c in result.twin_delta.chokepoints_changed}
    assert "cp_hormuz" in cp_changes

    # Live twin still open (never mutated)
    assert live_twin.chokepoint_by_id("cp_hormuz").status == original_status

    # Procurement + policy both carry reasoning_chain (DoD Upgrade 2)
    assert result.procurement["reasoning_chain"]
    assert result.policy["reasoning_chain"]

    # Scenario percentiles reflect an escalated shock
    assert result.scenario_percentiles["disruption_prob"] > 0.3


def test_run_whatif_saudi_boost_lifts_supplier_capacity(database_url):
    hypothesis, event = _seed_hormuz_event_and_hypothesis(database_url)
    live_twin = build_digital_twin(database_url)

    result = run_whatif(
        live_twin=live_twin,
        hypothesis=hypothesis,
        event=event,
        scenario_name="saudi_output_boost",
        scenario_params={"boost_kbd": 1000.0},
        cfg=WhatIfConfig(num_simulations=500),
    )

    supplier_changes = result.twin_delta.suppliers_changed
    assert any(
        s["country_iso3"] == "SAU" and s["spare_capacity_kbd"]["branch"] > s["spare_capacity_kbd"]["live"]
        for s in supplier_changes
    )


def test_run_whatif_unknown_scenario_raises(database_url):
    hypothesis, event = _seed_hormuz_event_and_hypothesis(database_url)
    live_twin = build_digital_twin(database_url)
    with pytest.raises(ValueError):
        run_whatif(
            live_twin=live_twin,
            hypothesis=hypothesis,
            event=event,
            scenario_name="not_a_real_preset",
            cfg=WhatIfConfig(num_simulations=100),
        )


def test_whatif_scenario_api_route(database_url, monkeypatch):
    hypothesis, event = _seed_hormuz_event_and_hypothesis(database_url)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.delenv("API_AUTH_KEY", raising=False)

    from api.main import app

    client = TestClient(app)
    presets_resp = client.get("/whatif/scenarios")
    assert presets_resp.status_code == 200
    presets = presets_resp.json()
    assert any(p["name"] == "close_hormuz" for p in presets)

    resp = client.post(
        "/whatif/scenario",
        json={
            "hypothesis_id": hypothesis.id,
            "scenario_name": "close_hormuz",
            "scenario_params": {},
            "num_simulations": 400,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scenario_name"] == "close_hormuz"
    assert body["live_state_touched"] is False
    assert body["branch_id"] != "live"
    assert body["procurement"]["reasoning_chain"]
    assert body["policy"]["reasoning_chain"]
    # Confirm the live twin is still un-touched via a follow-up GET
    live_resp = client.get("/digital-twin/state")
    assert live_resp.status_code == 200
    live_body = live_resp.json()
    hormuz = next(c for c in live_body["chokepoints"] if c["id"] == "cp_hormuz")
    assert hormuz["status"] == "open"
