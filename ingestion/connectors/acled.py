"""ACLED — Armed Conflict Location & Event Data.

Free for academic/research/hackathon use. Requires key + registered email.
Endpoint: https://api.acleddata.com/acled/read
Returns:
    {
        "data": [
            {"event_id_cnty": "...", "event_date": "2026-07-10",
             "event_type": "Battles", "country": "Iran",
             "actor1": "...", "actor2": "...", "location": "...",
             "latitude": "...", "longitude": "...", "fatalities": "0",
             "notes": "...", ...},
        ]
    }
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from ingestion.connectors._session import get as _get, post as _post

from ingestion.schemas.raw_signal import RawSignal


URL = "https://api.acleddata.com/acled/read"

DEFAULT_COUNTRIES = "Iran|Iraq|Saudi Arabia|Yemen|United Arab Emirates|Oman|Qatar|Bahrain|Kuwait"


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(value).astimezone(timezone.utc)
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
        source_id = row.get("event_id_cnty") if isinstance(row.get("event_id_cnty"), str) else None
        entities: list[str] = []
        for key in ("country", "event_type", "sub_event_type", "actor1", "actor2", "location"):
            val = row.get(key)
            if isinstance(val, str) and val.strip():
                entities.append(val.strip())
        signals.append(
            RawSignal.from_payload(
                source="acled",
                raw_payload=row,
                timestamp=_parse_date(row.get("event_date") if isinstance(row.get("event_date"), str) else None),
                entities_hint=entities,
                source_id=source_id,
            )
        )
    return signals


def fetch(
    api_key: str,
    email: str,
    country: str = DEFAULT_COUNTRIES,
    limit: int = 100,
    timeout: int = 30,
) -> list[RawSignal]:
    params = {
        "key": api_key,
        "email": email,
        "country": country,
        "limit": limit,
        "format": "json",
    }
    response = _get(URL, params=params, timeout=timeout)
    response.raise_for_status()
    return parse_payload(response.json())
