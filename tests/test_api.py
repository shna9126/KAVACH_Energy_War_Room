import os
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from api.main import app
from ingestion.schemas.raw_signal import RawSignal
from ingestion.storage import (
    StructuredEventInput,
    StructuredEventRow,
    append_raw_signals,
    append_structured_events,
    ensure_tables,
    fetch_unprocessed_raw_signals,
    get_engine,
)


def _seed_one_structured_event(database_url: str) -> int:
    raw = [
        RawSignal.from_payload(
            source="unit",
            raw_payload={"title": "Freight risk in corridor"},
            timestamp=datetime(2026, 7, 10, tzinfo=timezone.utc),
            entities_hint=["Hormuz"],
            source_id="api-seed-1",
        )
    ]
    append_raw_signals(database_url, raw)
    unprocessed = fetch_unprocessed_raw_signals(database_url, limit=1)
    append_structured_events(
        database_url,
        [
            StructuredEventInput(
                raw_signal_id=unprocessed[0].id,
                event_ts=unprocessed[0].signal_ts,
                action_type="supply_disruption",
                target="shipping_corridor",
                confidence=0.7,
                actors=["Hormuz"],
                extracted_payload={"source": "unit"},
            )
        ],
    )
    engine = get_engine(database_url)
    with engine.connect() as conn:
        row = conn.execute(
            select(StructuredEventRow.id).order_by(StructuredEventRow.id.desc()).limit(1)
        ).first()
    assert row is not None
    return int(row.id)


def test_api_endpoints(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "kavach.db"
    database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    ensure_tables(database_url)
    structured_event_id = _seed_one_structured_event(database_url)

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("HYPOTHESIS_MODE", "deterministic")
    monkeypatch.setenv("REDTEAM_MODE", "deterministic")
    monkeypatch.setenv("SCENARIO_NUM_SIMULATIONS", "400")

    client = TestClient(app)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    recent = client.get("/signals/recent")
    assert recent.status_code == 200
    assert isinstance(recent.json(), list)
    assert len(recent.json()) >= 1

    trig = client.post("/pipeline/trigger", json={"structured_event_id": structured_event_id})
    assert trig.status_code == 200
    state = trig.json()
    assert state["structured_event_id"] == structured_event_id
    assert state["hypothesis_id"] is not None
    pipeline_id = state["pipeline_id"]

    get_run = client.get(f"/pipeline/{pipeline_id}")
    assert get_run.status_code == 200
    assert get_run.json()["pipeline_id"] == pipeline_id

    details = client.get(f"/pipeline/{pipeline_id}/details")
    assert details.status_code == 200
    details_json = details.json()
    assert details_json["state"]["pipeline_id"] == pipeline_id
    assert isinstance(details_json["simulations"], list)

    kg = client.get("/kg/history", params={"node": "Hormuz", "limit": 10})
    assert kg.status_code == 200
    assert kg.json()["node"] == "Hormuz"

    whatif = client.post("/whatif", json={"simulation_id": state["simulation_ids"][0], "demand_kbd": 1700})
    assert whatif.status_code == 200
    assert whatif.json()["simulation_id"] == state["simulation_ids"][0]

    backtest = client.post(
        "/backtest",
        json={
            "start": "2026-07-01T00:00:00+00:00",
            "end": "2026-07-31T23:59:59+00:00",
        },
    )
    assert backtest.status_code == 200
    assert "events" in backtest.json()


def test_api_auth_optional_and_enforced(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "kavach_auth.db"
    database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    ensure_tables(database_url)
    structured_event_id = _seed_one_structured_event(database_url)

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("HYPOTHESIS_MODE", "deterministic")
    monkeypatch.setenv("REDTEAM_MODE", "deterministic")
    monkeypatch.setenv("SCENARIO_NUM_SIMULATIONS", "200")

    client = TestClient(app)

    # Auth disabled by default.
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    open_trigger = client.post("/pipeline/trigger", json={"structured_event_id": structured_event_id})
    assert open_trigger.status_code == 200
    sim_id = open_trigger.json()["simulation_ids"][0]

    # Auth enabled: missing header should fail.
    monkeypatch.setenv("API_AUTH_KEY", "secret-key")
    denied_trigger = client.post("/pipeline/trigger", json={"structured_event_id": structured_event_id})
    assert denied_trigger.status_code == 401

    denied_whatif = client.post("/whatif", json={"simulation_id": sim_id, "demand_kbd": 1750})
    assert denied_whatif.status_code == 401

    denied_backtest = client.post(
        "/backtest",
        json={
            "start": "2026-07-01T00:00:00+00:00",
            "end": "2026-07-31T23:59:59+00:00",
        },
    )
    assert denied_backtest.status_code == 401

    headers = {"X-API-Key": "secret-key"}
    ok_trigger = client.post(
        "/pipeline/trigger",
        json={"structured_event_id": structured_event_id},
        headers=headers,
    )
    assert ok_trigger.status_code == 200

    ok_whatif = client.post(
        "/whatif",
        json={"simulation_id": sim_id, "demand_kbd": 1750},
        headers=headers,
    )
    assert ok_whatif.status_code == 200

    ok_backtest = client.post(
        "/backtest",
        json={
            "start": "2026-07-01T00:00:00+00:00",
            "end": "2026-07-31T23:59:59+00:00",
        },
        headers=headers,
    )
    assert ok_backtest.status_code == 200
