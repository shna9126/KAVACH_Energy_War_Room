from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents.reasoning_chain import (
    REASONING_MISSING_ERROR,
    assert_recommendation_explained,
    attach_and_enforce,
    attach_to_recommendation,
    build_causal_chain,
    detect_affected_entities,
)
from digital_twin import build_digital_twin
from ingestion.storage import (
    RecommendationInput,
    StructuredEventRecord,
    ensure_tables,
)


@pytest.fixture()
def database_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'reasoning.db'}"
    ensure_tables(url)
    return url


@pytest.fixture()
def twin(database_url):
    return build_digital_twin(database_url)


def _hormuz_event() -> StructuredEventRecord:
    return StructuredEventRecord(
        id=42,
        raw_signal_id=1,
        event_ts=datetime(2026, 7, 12, tzinfo=timezone.utc),
        action_type="strait_closure_threat",
        target="strait_of_hormuz",
        confidence=0.7,
        actors=["Iran", "IRGC Navy"],
        extracted_payload={"headline": "Iran threatens to close Strait of Hormuz"},
    )


def test_detect_affected_entities_hormuz_propagates(twin):
    affected = detect_affected_entities(_hormuz_event(), twin)

    assert "cp_hormuz" in affected.chokepoints
    # Hormuz routes should be flagged
    assert any(r.startswith("route_") for r in affected.routes)
    # Countries at Hormuz origin (SAU, IRQ, IRN, ARE) and India as destination
    assert "IND" in affected.countries
    assert {"SAU", "IRQ", "IRN"}.intersection(affected.countries)
    # Indian refineries should get flagged via destination ports
    assert any(r.startswith("ref_") for r in affected.refineries)
    # SPR sites become drawdown candidates when India is affected
    assert affected.spr_sites


def test_build_causal_chain_produces_ordered_steps_with_summary(twin):
    chain = build_causal_chain(_hormuz_event(), twin, confidence=0.75)

    assert chain.confidence == pytest.approx(0.75)
    assert chain.twin_branch_id == twin.branch_id
    assert len(chain.steps) >= 3
    # Step numbers are sequential
    step_nums = [s.step_no for s in chain.steps]
    assert step_nums == sorted(step_nums)
    assert step_nums[0] == 1
    # Summary mentions at least one twin entity
    assert "chokepoints" in chain.summary or "refineries" in chain.summary
    # Bullet lines are non-empty and match step count
    bullets = chain.as_bullet_lines()
    assert len(bullets) == len(chain.steps)
    assert all(b.strip() for b in bullets)


def test_build_causal_chain_off_topic_event_still_returns_valid_chain(twin):
    event = StructuredEventRecord(
        id=1,
        raw_signal_id=1,
        event_ts=datetime(2026, 7, 1, tzinfo=timezone.utc),
        action_type="policy_announcement",
        target="unrelated_domain",
        confidence=0.4,
        actors=["some_actor"],
        extracted_payload={},
    )
    chain = build_causal_chain(event, twin)
    # Even with no entity hits, we still emit the trigger step
    assert len(chain.steps) >= 1
    assert chain.steps[0].step_no == 1


def test_attach_to_recommendation_embeds_chain_without_mutating_original(twin):
    chain = build_causal_chain(_hormuz_event(), twin)
    original_payload = {"supplier": "Saudi Aramco", "score": 0.82}
    rec = RecommendationInput(
        simulation_id=7,
        recommendation_type="procurement_plan",
        recommendation_payload=original_payload,
        score=0.82,
    )

    enriched = attach_to_recommendation(rec, chain)

    # Chain present on enriched
    assert "reasoning_chain" in enriched.recommendation_payload
    assert enriched.recommendation_payload["reasoning_chain"] == chain.as_bullet_lines()
    assert enriched.recommendation_payload["causal_chain"]["steps"]
    # Original payload dict is unchanged
    assert "reasoning_chain" not in original_payload
    assert original_payload == {"supplier": "Saudi Aramco", "score": 0.82}


def test_assert_recommendation_explained_raises_when_missing():
    rec = RecommendationInput(
        simulation_id=1,
        recommendation_type="policy_plan",
        recommendation_payload={"draw_mbd": 0.3},
        score=0.5,
    )
    with pytest.raises(ValueError, match=REASONING_MISSING_ERROR):
        assert_recommendation_explained(rec)


def test_attach_and_enforce_processes_all_recommendations(twin):
    chain = build_causal_chain(_hormuz_event(), twin)
    recs = [
        RecommendationInput(
            simulation_id=i,
            recommendation_type="economic_impact",
            recommendation_payload={"delta_usd_bn": i * 1.5},
            score=0.5,
        )
        for i in range(1, 4)
    ]
    enriched = attach_and_enforce(recs, chain)
    assert len(enriched) == 3
    for rec in enriched:
        assert rec.recommendation_payload["reasoning_chain"]
        assert rec.recommendation_payload["causal_chain"]["affected"]
