from datetime import datetime, timezone

from agents.hypothesis_agent import HypothesisAgentConfig, generate_hypothesis
from ingestion.storage import StructuredEventRecord


def _sample_event() -> StructuredEventRecord:
    return StructuredEventRecord(
        id=99,
        raw_signal_id=10,
        event_ts=datetime(2026, 7, 10, tzinfo=timezone.utc),
        action_type="supply_disruption",
        target="shipping_corridor",
        confidence=0.74,
        actors=["Iran", "Hormuz"],
        extracted_payload={"source": "newsapi"},
    )


def test_generate_hypothesis_deterministic() -> None:
    event = _sample_event()
    cfg = HypothesisAgentConfig(mode="deterministic", api_key="")
    out = generate_hypothesis(event, cfg)
    assert out.structured_event_id == event.id
    assert out.model_name == "deterministic_v1"
    assert 0.0 <= (out.confidence or 0) <= 1.0
    assert len(out.reasoning_chain) >= 1


def test_generate_hypothesis_auto_without_key_uses_deterministic() -> None:
    event = _sample_event()
    cfg = HypothesisAgentConfig(mode="auto", api_key="")
    out = generate_hypothesis(event, cfg)
    assert out.model_name == "deterministic_v1"
