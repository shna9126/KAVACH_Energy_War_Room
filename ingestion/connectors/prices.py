from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from ingestion.connectors._session import get as _get, post as _post

from ingestion.schemas.raw_signal import RawSignal


URL = "https://api.worldbank.org/v2/en/indicator/CM.MKT.PETR.CRUD.BRENT?format=json&per_page=120"


def _parse_year(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime(int(value), 1, 1, tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def parse_payload(payload: list[Any]) -> list[RawSignal]:
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
        return []

    rows = payload[1]
    signals: list[RawSignal] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        series = row.get("indicator")
        entities: list[str] = []
        if isinstance(series, dict):
            name = series.get("value")
            if isinstance(name, str) and name.strip():
                entities.append(name.strip())

        source_id = f"worldbank:{row.get('date')}" if row.get("date") else None
        signals.append(
            RawSignal.from_payload(
                source="world_bank_prices",
                raw_payload=row,
                timestamp=_parse_year(row.get("date") if isinstance(row.get("date"), str) else None),
                entities_hint=entities,
                source_id=source_id,
            )
        )
    return signals


def fetch(timeout: int = 30) -> list[RawSignal]:
    response = _get(URL, timeout=timeout)
    response.raise_for_status()
    return parse_payload(response.json())
