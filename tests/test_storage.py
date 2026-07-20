from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from ingestion.schemas.raw_signal import RawSignal
from ingestion.storage import (
    HypothesisInput,
    HypothesisReviewInput,
    RecommendationInput,
    SimulationInput,
    StructuredEventInput,
    append_hypotheses,
    append_hypothesis_reviews,
    append_raw_signals,
    append_recommendations,
    append_simulations,
    append_structured_events,
    ensure_tables,
    fetch_recommendation_map_by_simulation,
    fetch_recommendations_by_type,
    fetch_unprocessed_hypotheses,
    fetch_unprocessed_raw_signals,
    fetch_unprocessed_simulations,
    fetch_unprocessed_structured_events,
    fetch_unreviewed_hypotheses,
)


def test_append_raw_signals_sqlite(tmp_path) -> None:
    db_path = tmp_path / "kavach.db"
    database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    ensure_tables(database_url)

    signals = [
        RawSignal.from_payload(
            source="unit",
            raw_payload={"hello": "world"},
            timestamp=datetime(2026, 7, 10, tzinfo=timezone.utc),
            entities_hint=["world"],
            source_id="sample-1",
        )
    ]
    written = append_raw_signals(database_url, signals)
    assert written == 1

    written_again = append_raw_signals(database_url, signals)
    assert written_again == 0

    engine = create_engine(database_url)
    with engine.connect() as conn:
        total = conn.execute(text("select count(*) from raw_signals")).scalar_one()
    assert total == 1


def test_fetch_unprocessed_and_append_structured_events(tmp_path) -> None:
    db_path = tmp_path / "kavach.db"
    database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    ensure_tables(database_url)

    raw = [
        RawSignal.from_payload(
            source="unit",
            raw_payload={"title": "Shipping disruption reported"},
            timestamp=datetime(2026, 7, 10, tzinfo=timezone.utc),
            entities_hint=["Hormuz"],
            source_id="unit-1",
        )
    ]
    append_raw_signals(database_url, raw)

    unprocessed = fetch_unprocessed_raw_signals(database_url)
    assert len(unprocessed) == 1

    event = StructuredEventInput(
        raw_signal_id=unprocessed[0].id,
        event_ts=unprocessed[0].signal_ts,
        action_type="supply_disruption",
        target="shipping_corridor",
        confidence=0.7,
        actors=["Hormuz"],
        extracted_payload={"reason": "test"},
    )

    inserted = append_structured_events(database_url, [event])
    assert inserted == 1

    inserted_again = append_structured_events(database_url, [event])
    assert inserted_again == 0

    remaining = fetch_unprocessed_raw_signals(database_url)
    assert len(remaining) == 0


def test_append_hypotheses_and_unprocessed_structured_queue(tmp_path) -> None:
    db_path = tmp_path / "kavach.db"
    database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    ensure_tables(database_url)

    raw = [
        RawSignal.from_payload(
            source="unit",
            raw_payload={"title": "Freight risk rises"},
            timestamp=datetime(2026, 7, 10, tzinfo=timezone.utc),
            entities_hint=["Hormuz"],
            source_id="unit-h1",
        )
    ]
    append_raw_signals(database_url, raw)
    unprocessed_raw = fetch_unprocessed_raw_signals(database_url)
    assert len(unprocessed_raw) == 1

    event = StructuredEventInput(
        raw_signal_id=unprocessed_raw[0].id,
        event_ts=unprocessed_raw[0].signal_ts,
        action_type="supply_disruption",
        target="shipping_corridor",
        confidence=0.71,
        actors=["Hormuz"],
        extracted_payload={"source": "unit"},
    )
    append_structured_events(database_url, [event])

    events_for_hypothesis = fetch_unprocessed_structured_events(database_url)
    assert len(events_for_hypothesis) == 1

    hypothesis = HypothesisInput(
        structured_event_id=events_for_hypothesis[0].id,
        hypothesis_text="Supply disruptions may elevate import costs.",
        confidence=0.73,
        reasoning_chain=["Shipping risk up", "Freight premiums up"],
        model_name="deterministic_v1",
    )
    inserted = append_hypotheses(database_url, [hypothesis])
    assert inserted == 1

    inserted_again = append_hypotheses(database_url, [hypothesis])
    assert inserted_again == 0

    remaining_events = fetch_unprocessed_structured_events(database_url)
    assert len(remaining_events) == 0


def test_simulation_recommendation_queue_and_dedupe(tmp_path) -> None:
    db_path = tmp_path / "kavach.db"
    database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    ensure_tables(database_url)

    inserted_sims = append_simulations(
        database_url,
        [
            SimulationInput(
                hypothesis_id=None,
                horizon="1wk",
                percentiles={"disruption_prob": 0.5, "price_shock_pct": 0.1, "duration_days": 10},
                distribution=None,
                metadata={"source": "test"},
            )
        ],
    )
    assert inserted_sims == 1

    pending = fetch_unprocessed_simulations(database_url, recommendation_type="economic_impact")
    assert len(pending) == 1

    rec = RecommendationInput(
        simulation_id=pending[0].id,
        recommendation_type="economic_impact",
        recommendation_payload={"economic_impact": {"cpi_delta_pct": 0.2}},
        score=0.4,
    )
    inserted_recs = append_recommendations(database_url, [rec])
    assert inserted_recs == 1

    inserted_recs_again = append_recommendations(database_url, [rec])
    assert inserted_recs_again == 0

    pending_after = fetch_unprocessed_simulations(database_url, recommendation_type="economic_impact")
    assert len(pending_after) == 0


def test_hypothesis_to_simulation_queue_and_dedupe(tmp_path) -> None:
    db_path = tmp_path / "kavach.db"
    database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    ensure_tables(database_url)

    raw = [
        RawSignal.from_payload(
            source="unit",
            raw_payload={"title": "Disruption risk rises"},
            timestamp=datetime(2026, 7, 10, tzinfo=timezone.utc),
            entities_hint=["Hormuz"],
            source_id="unit-sim-1",
        )
    ]
    append_raw_signals(database_url, raw)
    unprocessed_raw = fetch_unprocessed_raw_signals(database_url)
    append_structured_events(
        database_url,
        [
            StructuredEventInput(
                raw_signal_id=unprocessed_raw[0].id,
                event_ts=unprocessed_raw[0].signal_ts,
                action_type="supply_disruption",
                target="shipping_corridor",
                confidence=0.7,
                actors=["Hormuz"],
                extracted_payload={"source": "unit"},
            )
        ],
    )

    unprocessed_structured = fetch_unprocessed_structured_events(database_url)
    append_hypotheses(
        database_url,
        [
            HypothesisInput(
                structured_event_id=unprocessed_structured[0].id,
                hypothesis_text="Shipping corridor risk may increase freight rates.",
                confidence=0.75,
                reasoning_chain=["signal", "impact"],
                model_name="deterministic_v1",
            )
        ],
    )

    pending_hypotheses = fetch_unprocessed_hypotheses(database_url)
    assert len(pending_hypotheses) == 1

    sims = [
        SimulationInput(
            hypothesis_id=pending_hypotheses[0].id,
            horizon="1wk",
            percentiles={"disruption_prob": 0.4, "price_shock_pct": 0.08, "duration_days": 9},
            distribution=None,
            metadata={"source": "test"},
        ),
        SimulationInput(
            hypothesis_id=pending_hypotheses[0].id,
            horizon="1mo",
            percentiles={"disruption_prob": 0.55, "price_shock_pct": 0.12, "duration_days": 18},
            distribution=None,
            metadata={"source": "test"},
        ),
    ]

    inserted = append_simulations(database_url, sims)
    assert inserted == 2

    inserted_again = append_simulations(database_url, sims)
    assert inserted_again == 0

    pending_after = fetch_unprocessed_hypotheses(database_url)
    assert len(pending_after) == 0


def test_hypothesis_review_queue_and_dedupe(tmp_path) -> None:
    db_path = tmp_path / "kavach.db"
    database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    ensure_tables(database_url)

    raw = [
        RawSignal.from_payload(
            source="unit",
            raw_payload={"title": "Energy security risk update"},
            timestamp=datetime(2026, 7, 10, tzinfo=timezone.utc),
            entities_hint=["Hormuz"],
            source_id="unit-r1",
        )
    ]
    append_raw_signals(database_url, raw)
    unprocessed_raw = fetch_unprocessed_raw_signals(database_url)
    append_structured_events(
        database_url,
        [
            StructuredEventInput(
                raw_signal_id=unprocessed_raw[0].id,
                event_ts=unprocessed_raw[0].signal_ts,
                action_type="supply_disruption",
                target="shipping_corridor",
                confidence=0.7,
                actors=["Hormuz"],
                extracted_payload={"source": "unit"},
            )
        ],
    )

    unprocessed_structured = fetch_unprocessed_structured_events(database_url)
    append_hypotheses(
        database_url,
        [
            HypothesisInput(
                structured_event_id=unprocessed_structured[0].id,
                hypothesis_text="Risk event may elevate import volatility.",
                confidence=0.76,
                reasoning_chain=["signal", "macro impact"],
                model_name="deterministic_v1",
            )
        ],
    )

    unreviewed = fetch_unreviewed_hypotheses(database_url)
    assert len(unreviewed) == 1

    review = HypothesisReviewInput(
        hypothesis_id=unreviewed[0].id,
        rebuttal_text="Counter evidence suggests only short-lived disruption.",
        counter_confidence=0.42,
        disproof_signals=["AIS flow stable"],
        reconciled_confidence=0.61,
        model_name="deterministic_redteam_v1",
    )

    inserted = append_hypothesis_reviews(database_url, [review])
    assert inserted == 1

    inserted_again = append_hypothesis_reviews(database_url, [review])
    assert inserted_again == 0

    unreviewed_after = fetch_unreviewed_hypotheses(database_url)
    assert len(unreviewed_after) == 0


def test_fetch_recommendations_helpers(tmp_path) -> None:
    db_path = tmp_path / "kavach.db"
    database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    ensure_tables(database_url)

    append_simulations(
        database_url,
        [
            SimulationInput(
                hypothesis_id=None,
                horizon="1wk",
                percentiles={"disruption_prob": 0.5, "price_shock_pct": 0.1, "duration_days": 10},
                distribution=None,
                metadata={"source": "helper-test"},
            )
        ],
    )
    pending = fetch_unprocessed_simulations(database_url, recommendation_type="economic_impact")
    append_recommendations(
        database_url,
        [
            RecommendationInput(
                simulation_id=pending[0].id,
                recommendation_type="economic_impact",
                recommendation_payload={"economic_impact": {"cpi_delta_pct": 0.2}},
                score=0.45,
            )
        ],
    )

    by_type = fetch_recommendations_by_type(database_url, "economic_impact")
    assert len(by_type) == 1

    mapped = fetch_recommendation_map_by_simulation(database_url, "economic_impact", [pending[0].id])
    assert pending[0].id in mapped
    assert mapped[pending[0].id].recommendation_type == "economic_impact"


def test_policy_queue_after_procurement(tmp_path) -> None:
    db_path = tmp_path / "kavach.db"
    database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    ensure_tables(database_url)

    append_simulations(
        database_url,
        [
            SimulationInput(
                hypothesis_id=None,
                horizon="1wk",
                percentiles={"disruption_prob": 0.5, "price_shock_pct": 0.1, "duration_days": 10},
                distribution=None,
                metadata={"source": "policy-test"},
            )
        ],
    )
    sims_for_procurement = fetch_unprocessed_simulations(database_url, recommendation_type="procurement_plan")
    assert len(sims_for_procurement) == 1

    append_recommendations(
        database_url,
        [
            RecommendationInput(
                simulation_id=sims_for_procurement[0].id,
                recommendation_type="procurement_plan",
                recommendation_payload={"procurement": {"demand_kbd": 1800, "secured_kbd": 1600, "gap_kbd": 200}},
                score=0.6,
            )
        ],
    )

    sims_for_policy = fetch_unprocessed_simulations(database_url, recommendation_type="policy_plan")
    assert len(sims_for_policy) == 1
