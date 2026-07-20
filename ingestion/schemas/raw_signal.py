from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class RawSignal(BaseModel):
    """Canonical, source-agnostic event shape emitted by all connectors."""

    source: str = Field(..., description="Origin system name, e.g. gdelt/newsapi/sanctions")
    timestamp: datetime = Field(..., description="Event timestamp in UTC")
    raw_payload: dict[str, Any] = Field(..., description="Original source record untouched")
    entities_hint: list[str] = Field(default_factory=list, description="Shallow entity hints to aid downstream extraction")
    source_id: str | None = Field(default=None, description="Source-native identifier when available")

    @classmethod
    def from_payload(
        cls,
        *,
        source: str,
        raw_payload: dict[str, Any],
        timestamp: datetime | None = None,
        entities_hint: list[str] | None = None,
        source_id: str | None = None,
    ) -> "RawSignal":
        event_time = timestamp or datetime.now(timezone.utc)
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)
        return cls(
            source=source,
            timestamp=event_time.astimezone(timezone.utc),
            raw_payload=raw_payload,
            entities_hint=entities_hint or [],
            source_id=source_id,
        )
