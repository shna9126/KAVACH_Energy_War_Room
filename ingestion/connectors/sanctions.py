from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from ingestion.connectors._session import get as _get, post as _post

from ingestion.schemas.raw_signal import RawSignal


URL = "https://api.opensanctions.org/search/default"


def _extract_entities(record: dict[str, Any]) -> list[str]:
    hints: list[str] = []
    for key in ("caption", "schema"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            hints.append(value.strip())
    datasets = record.get("datasets")
    if isinstance(datasets, list):
        hints.extend([d for d in datasets if isinstance(d, str)])
    return hints


def parse_payload(payload: dict[str, Any]) -> list[RawSignal]:
    results = payload.get("results", [])
    if not isinstance(results, list):
        return []

    now_utc = datetime.now(timezone.utc)
    signals: list[RawSignal] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        source_id = item.get("id") if isinstance(item.get("id"), str) else None
        signals.append(
            RawSignal.from_payload(
                source="opensanctions",
                raw_payload=item,
                timestamp=now_utc,
                entities_hint=_extract_entities(item),
                source_id=source_id,
            )
        )
    return signals


def fetch(query: str = "tanker", limit: int = 50, timeout: int = 30) -> list[RawSignal]:
    params = {"q": query, "limit": limit}
    response = _get(URL, params=params, timeout=timeout)
    response.raise_for_status()
    return parse_payload(response.json())
