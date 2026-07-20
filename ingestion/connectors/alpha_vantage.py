"""Alpha Vantage — real-time Brent/WTI daily prices.

Free tier: 25 API calls/day / 500 per month.
Endpoint returns:
    {
        "name": "Crude Oil Prices: Brent",
        "interval": "daily",
        "unit": "dollars per barrel",
        "data": [ {"date": "2026-07-14", "value": "84.32"}, ... ]
    }

Emits one `RawSignal` per data point with source="alpha_vantage_prices" and
`grade` embedded in `entities_hint` so the SqlPriceProvider can pick it up.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from ingestion.connectors._session import get as _get, post as _post

from ingestion.schemas.raw_signal import RawSignal


BASE_URL = "https://www.alphavantage.co/query"

FUNCTION_BY_GRADE = {
    "brent": ("BRENT", "Brent"),
    "wti": ("WTI", "WTI"),
    "natural_gas": ("NATURAL_GAS", "Natural Gas"),
}


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def parse_payload(payload: dict[str, Any], grade_label: str = "Brent") -> list[RawSignal]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    unit = payload.get("unit") or "dollars per barrel"
    signals: list[RawSignal] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_val = row.get("value")
        if raw_val in (None, "", "."):
            continue
        try:
            value = float(raw_val)
        except (TypeError, ValueError):
            continue
        date_str = row.get("date") if isinstance(row.get("date"), str) else None
        signals.append(
            RawSignal.from_payload(
                source="alpha_vantage_prices",
                raw_payload={
                    "value": value,
                    "date": date_str,
                    "unit": unit,
                    "grade": grade_label,
                    "series": payload.get("name"),
                },
                timestamp=_parse_date(date_str),
                entities_hint=[grade_label],
                source_id=f"alpha_vantage:{grade_label.lower()}:{date_str}" if date_str else None,
            )
        )
    return signals


def fetch(
    api_key: str,
    grade: str = "brent",
    interval: str = "daily",
    timeout: int = 30,
) -> list[RawSignal]:
    grade_key = grade.strip().lower()
    if grade_key not in FUNCTION_BY_GRADE:
        raise ValueError(f"Unsupported grade '{grade}'. Options: {sorted(FUNCTION_BY_GRADE)}")
    function, label = FUNCTION_BY_GRADE[grade_key]
    params = {"function": function, "interval": interval, "apikey": api_key, "datatype": "json"}
    response = _get(BASE_URL, params=params, timeout=timeout)
    response.raise_for_status()
    return parse_payload(response.json(), grade_label=label)
