from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from ingestion.connectors._session import get as _get, post as _post

from ingestion.schemas.raw_signal import RawSignal


URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def _parse_published_at(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def _extract_entities(article: dict[str, Any]) -> list[str]:
    hints: list[str] = []
    for key in ("sourcecountry", "seendate", "domain"):
        val = article.get(key)
        if isinstance(val, str) and val.strip():
            hints.append(val.strip())
    return hints


def parse_payload(payload: dict[str, Any]) -> list[RawSignal]:
    articles = payload.get("articles", [])
    if not isinstance(articles, list):
        return []

    signals: list[RawSignal] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        source_id = article.get("url") if isinstance(article.get("url"), str) else None
        published = article.get("seendate") if isinstance(article.get("seendate"), str) else None
        signals.append(
            RawSignal.from_payload(
                source="gdelt_doc",
                raw_payload=article,
                timestamp=_parse_published_at(published),
                entities_hint=_extract_entities(article),
                source_id=source_id,
            )
        )
    return signals


def fetch(query: str = "(Hormuz OR oil OR tanker) lang:english", max_records: int = 50, timeout: int = 30) -> list[RawSignal]:
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": max_records,
    }
    response = _get(URL, params=params, timeout=timeout)
    response.raise_for_status()
    return parse_payload(response.json())
