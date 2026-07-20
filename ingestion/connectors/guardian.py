"""The Guardian — supplementary editorial news signals for oil/energy geopolitics.

Free developer key: unlimited access to open content.
Endpoint: https://content.guardianapis.com/search
Returns:
    {
        "response": {
            "results": [
                {"id": "...", "webTitle": "...", "webUrl": "...",
                 "webPublicationDate": "2026-07-14T09:00:00Z",
                 "sectionName": "World news", ...},
            ]
        }
    }
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from ingestion.connectors._session import get as _get, post as _post

from ingestion.schemas.raw_signal import RawSignal


URL = "https://content.guardianapis.com/search"


def _parse_iso_utc(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def parse_payload(payload: dict[str, Any]) -> list[RawSignal]:
    response = payload.get("response") if isinstance(payload, dict) else None
    if not isinstance(response, dict):
        return []
    results = response.get("results")
    if not isinstance(results, list):
        return []
    signals: list[RawSignal] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        source_id = item.get("id") if isinstance(item.get("id"), str) else None
        entities: list[str] = []
        title = item.get("webTitle")
        if isinstance(title, str) and title.strip():
            entities.append(title.strip())
        section = item.get("sectionName")
        if isinstance(section, str) and section.strip():
            entities.append(section.strip())
        signals.append(
            RawSignal.from_payload(
                source="guardian",
                raw_payload=item,
                timestamp=_parse_iso_utc(
                    item.get("webPublicationDate") if isinstance(item.get("webPublicationDate"), str) else None
                ),
                entities_hint=entities,
                source_id=source_id,
            )
        )
    return signals


def fetch(
    api_key: str,
    query: str = "Hormuz OR crude OR OPEC OR tanker OR sanctions",
    page_size: int = 25,
    section: str | None = "world",
    timeout: int = 30,
) -> list[RawSignal]:
    params = {"q": query, "page-size": page_size, "api-key": api_key, "order-by": "newest"}
    if section:
        params["section"] = section
    response = _get(URL, params=params, timeout=timeout)
    response.raise_for_status()
    return parse_payload(response.json())
