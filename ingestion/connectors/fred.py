"""FRED — Federal Reserve Economic Data.

Free key required. Endpoint:
    https://api.stlouisfed.org/fred/series/observations?series_id=&api_key=&file_type=json

Default series pulled here (macro overlays for the economic agent):
    DCOILBRENTEU  — Brent (Europe) $/bbl daily
    DTWEXBGS      — Nominal Trade-Weighted USD index
    CPIAUCSL      — US CPI monthly
    DEXINUS       — INR / USD daily
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from ingestion.connectors._session import get as _get, post as _post

from ingestion.schemas.raw_signal import RawSignal


URL = "https://api.stlouisfed.org/fred/series/observations"


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def parse_payload(payload: dict[str, Any], series_id: str) -> list[RawSignal]:
    obs = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(obs, list):
        return []
    signals: list[RawSignal] = []
    for row in obs:
        if not isinstance(row, dict):
            continue
        raw = row.get("value")
        if raw in (None, "", "."):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        date_str = row.get("date") if isinstance(row.get("date"), str) else None
        signals.append(
            RawSignal.from_payload(
                source="fred",
                raw_payload={"series_id": series_id, "date": date_str, "value": value},
                timestamp=_parse_date(date_str),
                entities_hint=[series_id],
                source_id=f"fred:{series_id}:{date_str}" if date_str else None,
            )
        )
    return signals


def fetch(
    api_key: str,
    series_id: str = "DCOILBRENTEU",
    observation_start: str | None = None,
    limit: int = 120,
    timeout: int = 30,
) -> list[RawSignal]:
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }
    if observation_start:
        params["observation_start"] = observation_start
    response = _get(URL, params=params, timeout=timeout)
    response.raise_for_status()
    return parse_payload(response.json(), series_id=series_id)
