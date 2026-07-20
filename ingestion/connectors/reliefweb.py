"""ReliefWeb — UN humanitarian & conflict alerts (no key required).

Endpoint: https://api.reliefweb.int/v1/reports
Returns:
    {
        "data": [
            {"id": "...", "fields": {"title": "...", "date": {"created": "..."},
             "country": [{"name": "..."}], "source": [{"name": "..."}]}},
        ]
    }
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import requests
from ingestion.connectors._session import get as _get, post as _post

from ingestion.schemas.raw_signal import RawSignal


DEFAULT_BASE_URL = "https://api.reliefweb.int/v2"


def _parse_iso_utc(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def parse_payload(payload: dict[str, Any]) -> list[RawSignal]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    signals: list[RawSignal] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
        source_id = row.get("id") if isinstance(row.get("id"), (str, int)) else None
        title = fields.get("title") if isinstance(fields.get("title"), str) else None
        entities: list[str] = []
        if title:
            entities.append(title)
        for country in fields.get("country") or []:
            if isinstance(country, dict) and isinstance(country.get("name"), str):
                entities.append(country["name"])
        for src in fields.get("source") or []:
            if isinstance(src, dict) and isinstance(src.get("name"), str):
                entities.append(src["name"])

        date_created = None
        if isinstance(fields.get("date"), dict):
            date_created = fields["date"].get("created")

        signals.append(
            RawSignal.from_payload(
                source="reliefweb",
                raw_payload=row,
                timestamp=_parse_iso_utc(date_created if isinstance(date_created, str) else None),
                entities_hint=entities,
                source_id=str(source_id) if source_id is not None else None,
            )
        )
    return signals


def fetch(
    base_url: str | None = None,
    query: str = "oil OR tanker OR Hormuz OR refinery",
    limit: int = 40,
    timeout: int = 30,
) -> list[RawSignal]:
    base = (base_url or os.getenv("RELIEFWEB_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    params = {
        "appname": "kavach-hackathon",
        "limit": limit,
        "query[value]": query,
        "sort[]": "date:desc",
        "fields[include][]": ["title", "date.created", "country.name", "source.name", "url"],
    }
    response = _get(f"{base}/reports", params=params, timeout=timeout)
    response.raise_for_status()
    return parse_payload(response.json())
