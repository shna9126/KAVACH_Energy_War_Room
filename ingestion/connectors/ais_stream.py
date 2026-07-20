from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from ingestion.schemas.raw_signal import RawSignal


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_payload(message: dict[str, Any]) -> dict[str, Any]:
    metadata = message.get("MetaData") if isinstance(message.get("MetaData"), dict) else {}
    body = message.get("Message") if isinstance(message.get("Message"), dict) else {}

    report = None
    if isinstance(body.get("PositionReport"), dict):
        report = body.get("PositionReport")
    elif isinstance(body.get("StandardClassBPositionReport"), dict):
        report = body.get("StandardClassBPositionReport")
    elif isinstance(body.get("LongRangeAisBroadcastMessage"), dict):
        report = body.get("LongRangeAisBroadcastMessage")
    else:
        report = {}

    lat = _to_float(metadata.get("latitude") if metadata else None)
    lon = _to_float(metadata.get("longitude") if metadata else None)
    if lat is None:
        lat = _to_float(report.get("Latitude"))
    if lon is None:
        lon = _to_float(report.get("Longitude"))

    return {
        "mmsi": str(metadata.get("MMSI") or report.get("UserID") or "").strip(),
        "name": metadata.get("ShipName") or metadata.get("shipname") or "",
        "lat": lat,
        "lon": lon,
        "status": report.get("NavigationalStatus"),
        "sog": report.get("Sog"),
        "cog": report.get("Cog"),
        "heading": report.get("TrueHeading"),
        "raw_message": message,
    }


async def _collect(
    api_key: str,
    bounding_boxes: list[list[list[float]]],
    *,
    max_messages: int,
    timeout_seconds: int,
) -> list[RawSignal]:
    import websockets

    uri = "wss://stream.aisstream.io/v0/stream"
    subscription = {
        "APIKey": api_key,
        "BoundingBoxes": bounding_boxes,
        "FilterMessageTypes": ["PositionReport", "StandardClassBPositionReport", "LongRangeAisBroadcastMessage"],
    }

    out: list[RawSignal] = []
    seen: set[str] = set()

    async with websockets.connect(uri, open_timeout=15, close_timeout=5, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps(subscription))

        started = asyncio.get_running_loop().time()
        while len(out) < max_messages:
            elapsed = asyncio.get_running_loop().time() - started
            remaining = timeout_seconds - elapsed
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(3.0, remaining))
            except asyncio.TimeoutError:
                continue

            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="ignore")
            if not isinstance(raw, str):
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            payload = _extract_payload(msg)
            mmsi = payload.get("mmsi")
            lat = payload.get("lat")
            lon = payload.get("lon")
            if not mmsi or lat is None or lon is None:
                continue

            dedupe = f"{mmsi}:{lat:.4f}:{lon:.4f}"
            if dedupe in seen:
                continue
            seen.add(dedupe)

            out.append(
                RawSignal.from_payload(
                    source="ais_stream",
                    raw_payload=payload,
                    timestamp=datetime.now(timezone.utc),
                    entities_hint=[str(payload.get("name") or "AIS Vessel"), str(mmsi)],
                    source_id=mmsi,
                )
            )

    return out


def fetch(
    api_key: str,
    *,
    bounding_boxes: list[list[list[float]]] | None = None,
    max_messages: int = 20,
    timeout_seconds: int = 10,
) -> list[RawSignal]:
    """Fetch live AIS position reports from AIS Stream websocket API."""
    if not api_key.strip():
        return []
    # Default to global coverage to avoid zero-result bursts from a narrow box.
    boxes = bounding_boxes or [[[-90.0, -180.0], [90.0, 180.0]]]
    return asyncio.run(
        _collect(
            api_key=api_key,
            bounding_boxes=boxes,
            max_messages=max_messages,
            timeout_seconds=timeout_seconds,
        )
    )
