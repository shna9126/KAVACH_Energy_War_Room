from __future__ import annotations

from dataclasses import dataclass

from ingestion.storage import RawSignalRecord, StructuredEventInput


@dataclass
class ExtractedEvent:
    action_type: str
    target: str
    confidence: float
    reasoning: str


def _normalize_text(record: RawSignalRecord) -> str:
    payload = record.raw_payload or {}
    parts = []
    for key in ("title", "webTitle", "headline", "description", "caption", "content", "webUrl", "url", "sectionName"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    if not parts:
        parts.append(str(payload))
    return " ".join(parts).lower()


def _classify(text: str) -> ExtractedEvent:
    if any(word in text for word in ["sanction", "blacklist", "restricted"]):
        return ExtractedEvent("sanctions", "entity_or_vessel", 0.8, "Detected sanctions-related terms")
    if any(word in text for word in ["hormuz", "disruption", "blockade", "attack", "shipping"]):
        return ExtractedEvent("supply_disruption", "shipping_corridor", 0.76, "Detected corridor disruption terms")
    if any(word in text for word in ["inventory", "ending stocks", "stock draw", "distillate", "diesel stocks"]):
        return ExtractedEvent("inventory_shift", "fuel_stocks", 0.7, "Detected inventory and stock movement terms")
    if any(word in text for word in ["brent", "price", "premium", "volatility"]):
        return ExtractedEvent("price_shock", "crude_market", 0.72, "Detected pricing stress terms")
    if any(word in text for word in ["refiner", "procurement", "imports", "cargo"]):
        return ExtractedEvent("procurement_shift", "refinery_strategy", 0.68, "Detected procurement adjustment terms")
    return ExtractedEvent("signal_watch", "general_energy_risk", 0.55, "Fallback classification")


def extract_structured_event(record: RawSignalRecord) -> StructuredEventInput:
    text = _normalize_text(record)
    classified = _classify(text)
    actors = record.entities_hint[:10]
    extracted_payload = {
        "source": record.source,
        "source_id": record.source_id,
        "reasoning": classified.reasoning,
        "keywords_text": text[:400],
    }

    return StructuredEventInput(
        raw_signal_id=record.id,
        event_ts=record.signal_ts,
        action_type=classified.action_type,
        target=classified.target,
        confidence=classified.confidence,
        actors=actors,
        extracted_payload=extracted_payload,
    )


def extract_structured_event_deterministic(record: RawSignalRecord) -> StructuredEventInput:
    return extract_structured_event(record)

