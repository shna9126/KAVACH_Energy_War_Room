from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from ingestion.connectors._session import get as _get, post as _post

from ingestion.schemas.raw_signal import RawSignal


URL = "https://newsapi.org/v2/everything"


def _parse_iso_utc(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def parse_payload(payload: dict[str, Any]) -> list[RawSignal]:
    articles = payload.get("articles", [])
    if not isinstance(articles, list):
        return []

    signals: list[RawSignal] = []
    for article in articles:
        if not isinstance(article, dict):
            continue

        source_obj = article.get("source")
        entities: list[str] = []
        if isinstance(source_obj, dict):
            source_name = source_obj.get("name")
            if isinstance(source_name, str) and source_name.strip():
                entities.append(source_name.strip())

        title = article.get("title")
        if isinstance(title, str) and title.strip():
            entities.append(title.strip())

        published_at = article.get("publishedAt") if isinstance(article.get("publishedAt"), str) else None
        source_id = article.get("url") if isinstance(article.get("url"), str) else None
        signals.append(
            RawSignal.from_payload(
                source="newsapi",
                raw_payload=article,
                timestamp=_parse_iso_utc(published_at),
                entities_hint=entities,
                source_id=source_id,
            )
        )
    return signals


def fetch(api_key: str, query: str = "Hormuz oil tanker", page_size: int = 50, timeout: int = 30) -> list[RawSignal]:
    headers = {"X-Api-Key": api_key}
    params = {
        "q": query,
        "language": "en",
        "pageSize": page_size,
        "sortBy": "publishedAt",
    }
    response = _get(URL, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return parse_payload(response.json())
