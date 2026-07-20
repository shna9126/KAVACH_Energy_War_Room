from __future__ import annotations

import os
import json
import time

from fastapi import APIRouter
from sqlalchemy import select

from api.schemas import AisVesselItem, SignalItem
from ingestion.storage import RawSignalRow, StructuredEventRow, fetch_live_market_context, get_engine


router = APIRouter(prefix="/signals", tags=["signals"])

_AIS_CACHE_TTL_SECONDS = 45
_ais_cache: dict[str, object] = {
    "ts": 0.0,
    "items": [],
}


def _normalize_ais_payload(payload: dict) -> AisVesselItem | None:
    if not isinstance(payload, dict):
        return None
    mmsi = str(payload.get("mmsi") or "").strip()
    if not mmsi:
        return None
    try:
        lat = float(payload.get("lat"))
        lon = float(payload.get("lon"))
    except (TypeError, ValueError):
        return None
    try:
        sog = float(payload.get("sog")) if payload.get("sog") is not None else None
    except (TypeError, ValueError):
        sog = None
    try:
        cog = float(payload.get("cog")) if payload.get("cog") is not None else None
    except (TypeError, ValueError):
        cog = None
    try:
        heading = float(payload.get("heading")) if payload.get("heading") is not None else None
    except (TypeError, ValueError):
        heading = None

    return AisVesselItem(
        mmsi=mmsi,
        name=str(payload.get("name") or "").strip() or None,
        lat=lat,
        lon=lon,
        status=str(payload.get("status") or "").strip() or None,
        sog=sog,
        cog=cog,
        heading=heading,
        source="ais_stream",
    )


@router.get("/market-context")
def get_market_context() -> dict:
    """Return latest live market signals pulled from ingested API data."""
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return {}
    return fetch_live_market_context(database_url)


@router.get("/recent", response_model=list[SignalItem])
def get_recent_signals(limit: int = 20) -> list[SignalItem]:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return []

    engine = get_engine(database_url)
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                StructuredEventRow.id,
                StructuredEventRow.event_ts,
                StructuredEventRow.action_type,
                StructuredEventRow.target,
                StructuredEventRow.confidence,
                StructuredEventRow.actors,
                RawSignalRow.source,
                RawSignalRow.source_id,
            )
            .join(RawSignalRow, RawSignalRow.id == StructuredEventRow.raw_signal_id)
            .order_by(StructuredEventRow.event_ts.desc())
            .limit(limit)
        ).all()

    return [
        SignalItem(
            structured_event_id=row.id,
            event_ts=row.event_ts,
            action_type=row.action_type,
            target=row.target,
            confidence=row.confidence,
            actors=row.actors or [],
            source=row.source,
            source_id=row.source_id,
        )
        for row in rows
    ]


@router.get("/recent-live", response_model=list[SignalItem])
def get_recent_live_signals(limit: int = 20) -> list[SignalItem]:
    """Return recent live-news structured events, excluding fixture/sample rows."""
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return []

    engine = get_engine(database_url)
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                StructuredEventRow.id,
                StructuredEventRow.event_ts,
                StructuredEventRow.action_type,
                StructuredEventRow.target,
                StructuredEventRow.confidence,
                StructuredEventRow.actors,
                RawSignalRow.source,
                RawSignalRow.source_id,
                RawSignalRow.raw_payload,
            )
            .join(RawSignalRow, RawSignalRow.id == StructuredEventRow.raw_signal_id)
            .where(RawSignalRow.source.in_(["guardian", "newsapi", "gdelt_doc", "gdelt"]))
            .order_by(StructuredEventRow.event_ts.desc())
            .limit(max(1, min(200, limit * 5)))
        ).all()

    out: list[SignalItem] = []
    for row in rows:
        actors = row.actors or []
        payload = row.raw_payload
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        payload = payload if isinstance(payload, dict) else {}

        haystack_parts = [
            row.target or "",
            row.action_type or "",
            " ".join(str(a) for a in actors),
            str(payload.get("title") or ""),
            str(payload.get("webTitle") or ""),
            str(payload.get("caption") or ""),
        ]
        haystack = " ".join(haystack_parts).lower()
        if "example " in haystack or " example" in haystack:
            continue

        out.append(
            SignalItem(
                structured_event_id=row.id,
                event_ts=row.event_ts,
                action_type=row.action_type,
                target=row.target,
                confidence=row.confidence,
                actors=actors,
                source=row.source,
                source_id=row.source_id,
            )
        )
        if len(out) >= limit:
            break

    return out


@router.get("/ais-live", response_model=list[AisVesselItem])
def get_live_ais_points(limit: int = 600) -> list[AisVesselItem]:
    """Return live AIS vessel points for map overlays.

    Uses short-lived in-process cache to avoid reconnecting websocket on every
    UI repaint/toggle. If live key is unavailable, falls back to recent ingested
    AIS rows from DB.
    """
    bounded_limit = max(50, min(2000, int(limit)))
    now = time.time()
    cached_items = _ais_cache.get("items") if isinstance(_ais_cache.get("items"), list) else []
    cached_ts = float(_ais_cache.get("ts") or 0.0)
    if cached_items and (now - cached_ts) <= _AIS_CACHE_TTL_SECONDS:
        return cached_items[:bounded_limit]

    items: list[AisVesselItem] = []
    ais_key = os.getenv("AIS_STREAM_API_KEY", "").strip()
    if ais_key:
        try:
            from ingestion.connectors import ais_stream as ais_stream_connector

            # Focus box: East Africa -> South China Sea, matching main KAVACH theatre.
            boxes = [[[ -10.0, 35.0], [35.0, 115.0 ]]]
            signals = ais_stream_connector.fetch(
                api_key=ais_key,
                bounding_boxes=boxes,
                max_messages=bounded_limit,
                timeout_seconds=7,
            )
            for s in signals:
                payload = s.raw_payload if isinstance(s.raw_payload, dict) else {}
                item = _normalize_ais_payload(payload)
                if item is not None:
                    items.append(item)
        except Exception:
            items = []

    if not items:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            return []
        engine = get_engine(database_url)
        with engine.connect() as conn:
            rows = conn.execute(
                select(RawSignalRow.raw_payload)
                .where(RawSignalRow.source == "ais_stream")
                .order_by(RawSignalRow.signal_ts.desc())
                .limit(max(200, bounded_limit * 2))
            ).all()
        seen: set[str] = set()
        for row in rows:
            payload = row.raw_payload if isinstance(row.raw_payload, dict) else {}
            item = _normalize_ais_payload(payload)
            if item is None:
                continue
            key = f"{item.mmsi}:{item.lat:.4f}:{item.lon:.4f}"
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            if len(items) >= bounded_limit:
                break

    _ais_cache["ts"] = now
    _ais_cache["items"] = items
    return items[:bounded_limit]
