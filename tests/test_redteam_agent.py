from agents.redteam_agent import RedTeamAgentConfig, generate_redteam_review
from ingestion.storage import HypothesisRecord


def _sample_hypothesis() -> HypothesisRecord:
    return HypothesisRecord(
        id=5,
        structured_event_id=1,
        hypothesis_text="Supply disruption likely to raise freight costs.",
        confidence=0.8,
        reasoning_chain=["signal", "impact"],
        model_name="deterministic_v1",
    )


def test_generate_redteam_review_deterministic() -> None:
    cfg = RedTeamAgentConfig(mode="deterministic", api_key="")
    out = generate_redteam_review(_sample_hypothesis(), cfg)
    assert out.hypothesis_id == 5
    assert out.model_name == "deterministic_redteam_v2"
    assert 0.0 <= (out.counter_confidence or 0) <= 1.0
    assert 0.0 <= (out.reconciled_confidence or 0) <= 1.0
    assert len(out.disproof_signals) >= 1
