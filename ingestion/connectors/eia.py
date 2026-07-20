"""EIA — U.S. Energy Information Administration open data.

Free key required. Endpoint pattern:
    https://api.eia.gov/v2/{route}/data?api_key=&frequency=&data[0]=value

Series pulled here (crude/petroleum context):
    PET.WCESTUS1.W — Weekly U.S. Ending Stocks of Crude Oil (thousand bbl)
    PET.WGIRIUS2.W — Weekly U.S. Gross Inputs into Refineries (thousand bbl/day)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from ingestion.connectors._session import get as _get, post as _post

from ingestion.schemas.raw_signal import RawSignal


BASE_URL = "https://api.eia.gov/v2"

DEFAULT_ROUTE = "petroleum/stoc/wstk"


def _parse_period(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def parse_payload(payload: dict[str, Any]) -> list[RawSignal]:
    response = payload.get("response") if isinstance(payload, dict) else None
    if not isinstance(response, dict):
        return []
    rows = response.get("data")
    if not isinstance(rows, list):
        return []
    series_name = response.get("name") or "eia_series"
    signals: list[RawSignal] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        period = row.get("period") if isinstance(row.get("period"), str) else None
        entities: list[str] = []
        for key in ("series-description", "product-name", "process-name", "duoarea"):
            val = row.get(key)
            if isinstance(val, str) and val.strip():
                entities.append(val.strip())
        series_id = row.get("series") if isinstance(row.get("series"), str) else None
        signals.append(
            RawSignal.from_payload(
                source="eia",
                raw_payload={**row, "_series_name": series_name},
                timestamp=_parse_period(period),
                entities_hint=entities,
                source_id=f"eia:{series_id}:{period}" if series_id and period else None,
            )
        )
    return signals


def fetch(
    api_key: str,
    route: str = DEFAULT_ROUTE,
    frequency: str = "weekly",
    length: int = 52,
    timeout: int = 30,
) -> list[RawSignal]:
    url = f"{BASE_URL}/{route.strip('/')}/data"
    params = {
        "api_key": api_key,
        "frequency": frequency,
        "data[0]": "value",
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": length,
    }
    response = _get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return parse_payload(response.json())
