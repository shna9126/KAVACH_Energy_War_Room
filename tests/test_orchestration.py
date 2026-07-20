from datetime import datetime, timezone

from ingestion.schemas.raw_signal import RawSignal
from ingestion.storage import append_raw_signals, append_structured_events, fetch_unprocessed_raw_signals, StructuredEventInput, ensure_tables
from orchestration.graph import run_pipeline_for_structured_event


def test_run_pipeline_for_structured_event_end_to_end(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "kavach.db"
    database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    ensure_tables(database_url)

    monkeypatch.setenv("HYPOTHESIS_MODE", "deterministic")
    monkeypatch.setenv("REDTEAM_MODE", "deterministic")
    monkeypatch.setenv("SCENARIO_NUM_SIMULATIONS", "400")

    raw = [
        RawSignal.from_payload(
            source="unit",
            raw_payload={"title": "Shipping route disruption concern"},
            timestamp=datetime(2026, 7, 10, tzinfo=timezone.utc),
            entities_hint=["Hormuz"],
            source_id="orch-1",
        )
    ]
    append_raw_signals(database_url, raw)
    unprocessed = fetch_unprocessed_raw_signals(database_url)
    append_structured_events(
        database_url,
        [
            StructuredEventInput(
                raw_signal_id=unprocessed[0].id,
                event_ts=unprocessed[0].signal_ts,
                action_type="supply_disruption",
                target="shipping_corridor",
                confidence=0.72,
                actors=["Hormuz"],
                extracted_payload={"source": "unit"},
            )
        ],
    )

    state = run_pipeline_for_structured_event(database_url, structured_event_id=1)
    assert state.structured_event_id == 1
    assert state.hypothesis_id is not None
    assert state.rebuttal_id is not None
    assert len(state.simulation_ids) > 0
    assert len(state.economic_recommendation_ids) > 0
    assert len(state.procurement_recommendation_ids) > 0
    assert len(state.policy_recommendation_ids) > 0
