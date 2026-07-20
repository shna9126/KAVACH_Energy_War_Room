from datetime import datetime, timezone

from ingestion.storage import RawSignalRecord
from processing.extraction.deterministic_extractor import extract_structured_event


def test_extract_structured_event_sanctions() -> None:
    record = RawSignalRecord(
        id=1,
        source="opensanctions",
        source_id="Q123",
        signal_ts=datetime(2026, 7, 10, tzinfo=timezone.utc),
        entities_hint=["Example Tanker LLC"],
        raw_payload={"caption": "Sanctioned tanker listed"},
    )
    out = extract_structured_event(record)
    assert out.action_type == "sanctions"
    assert out.target == "entity_or_vessel"
    assert out.raw_signal_id == 1
