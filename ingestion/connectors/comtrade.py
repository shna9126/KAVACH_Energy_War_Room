from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from ingestion.connectors._session import get as _get, post as _post

from ingestion.schemas.raw_signal import RawSignal


URL = "https://comtradeapi.un.org/data/v1/get/C/A/HS"


def _record_year_to_utc(value: Any) -> datetime:
    try:
        year = int(value)
        return datetime(year, 1, 1, tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def parse_payload(payload: dict[str, Any]) -> list[RawSignal]:
    rows = payload.get("data", [])
    if not isinstance(rows, list):
        return []

    signals: list[RawSignal] = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        source_id = None
        if isinstance(row.get("cmdCode"), str) and isinstance(row.get("period"), (str, int)):
            source_id = f"{row.get('cmdCode')}:{row.get('period')}:{row.get('reporterCode')}:{row.get('partnerCode')}"

        entities = []
        for key in ("reporterDesc", "partnerDesc", "cmdDesc"):
            val = row.get(key)
            if isinstance(val, str) and val.strip():
                entities.append(val.strip())

        signals.append(
            RawSignal.from_payload(
                source="comtrade",
                raw_payload=row,
                timestamp=_record_year_to_utc(row.get("period")),
                entities_hint=entities,
                source_id=source_id,
            )
        )
    return signals


def fetch(api_key: str, reporter: str = "356", commodity_code: str = "2709", timeout: int = 30) -> list[RawSignal]:
    params = {
        "max": 200,
        "fmt": "json",
        "ps": str(datetime.now(timezone.utc).year - 1),
        "r": reporter,
        "p": "0",
        "rg": "1",
        "cc": commodity_code,
    }
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    response = _get(URL, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return parse_payload(response.json())
