"""Provider protocols and SQL-backed implementations for dynamic Digital Twin
slices.

Static topology (countries, ports, chokepoints, routes, refineries, SPR,
grades) comes from `digital_twin.seed_data`. Dynamic slices (prices,
sanctions, trade flows) are read from the existing Postgres/SQLite
`raw_signals` table so we don't couple the twin to any single connector.

Live-API providers (Alpha Vantage, AIS Stream, Stormglass, OpenWeather) can
be added later by implementing the same protocol without changing callers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Iterable, Protocol

from sqlalchemy import select

from digital_twin.graph_state import (
    PricePoint,
    SanctionEntry,
    Tanker,
    TradeFlow,
)
from ingestion.storage import RawSignalRow, get_engine


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class PriceProvider(Protocol):
    def fetch_prices(self) -> list[PricePoint]: ...


class SanctionsProvider(Protocol):
    def fetch_sanctions(self) -> list[SanctionEntry]: ...


class TradeFlowProvider(Protocol):
    def fetch_trade_flows(self) -> list[TradeFlow]: ...


class TankerProvider(Protocol):
    def fetch_tankers(self) -> list[Tanker]: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_GRADE_ALIASES: dict[str, str] = {
    "brent": "grade_brent",
    "wti": "grade_wti",
    "arab light": "grade_arab_light",
    "arab medium": "grade_arab_medium",
    "basrah medium": "grade_basrah_medium",
    "basrah heavy": "grade_basrah_heavy",
    "murban": "grade_murban",
    "urals": "grade_urals",
    "espo": "grade_espo",
    "bonny light": "grade_bonny_light",
    "iranian heavy": "grade_iran_heavy",
    "iran heavy": "grade_iran_heavy",
}


def _normalize_grade_id(hint: str | None, source: str) -> str:
    if hint:
        key = hint.strip().lower()
        for alias, grade_id in _GRADE_ALIASES.items():
            if alias in key:
                return grade_id
    if source in ("world_bank_prices", "alpha_vantage_prices"):
        return "grade_brent"
    return "grade_brent"


def _iter_raw_signals(database_url: str, source: str, limit: int) -> Iterable[Any]:
    engine = get_engine(database_url)
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                RawSignalRow.id,
                RawSignalRow.source,
                RawSignalRow.source_id,
                RawSignalRow.signal_ts,
                RawSignalRow.entities_hint,
                RawSignalRow.raw_payload,
            )
            .where(RawSignalRow.source == source)
            .order_by(RawSignalRow.signal_ts.desc())
            .limit(limit)
        ).all()
    return rows


# ---------------------------------------------------------------------------
# SQL-backed default implementations
# ---------------------------------------------------------------------------


@dataclass
class SqlPriceProvider:
    database_url: str
    limit: int = 240
    # Sources are polled in order; the *first* row seen per grade wins because
    # each source's rows come back time-desc. Live sources go first.
    sources: tuple[str, ...] = ("alpha_vantage_prices", "world_bank_prices")

    def fetch_prices(self) -> list[PricePoint]:
        prices: list[PricePoint] = []
        for source in self.sources:
            for row in _iter_raw_signals(self.database_url, source, self.limit):
                payload = row.raw_payload or {}
                value = payload.get("value")
                if value is None:
                    continue
                try:
                    price = float(value)
                except (TypeError, ValueError):
                    continue
                hint = None
                if isinstance(row.entities_hint, list) and row.entities_hint:
                    hint = str(row.entities_hint[0])
                grade_id = _normalize_grade_id(hint, row.source)
                as_of = row.signal_ts if isinstance(row.signal_ts, datetime) else datetime.now(timezone.utc)
                prices.append(PricePoint(grade_id=grade_id, price_usd_per_bbl=price, as_of=as_of))
        return prices


@dataclass
class SqlSanctionsProvider:
    database_url: str
    limit: int = 200

    def fetch_sanctions(self) -> list[SanctionEntry]:
        out: list[SanctionEntry] = []
        for row in _iter_raw_signals(self.database_url, "opensanctions", self.limit):
            payload = row.raw_payload or {}
            entity = payload.get("caption") or payload.get("id")
            if not isinstance(entity, str) or not entity.strip():
                continue

            imposed_by: list[str] = []
            countries = payload.get("countries")
            if isinstance(countries, list):
                imposed_by = [c for c in countries if isinstance(c, str)]

            datasets = payload.get("datasets")
            datasets_list = [d for d in datasets if isinstance(d, str)] if isinstance(datasets, list) else []

            schema_type = payload.get("schema") if isinstance(payload.get("schema"), str) else None

            effective_from = row.signal_ts if isinstance(row.signal_ts, datetime) else None

            out.append(
                SanctionEntry(
                    entity=entity.strip(),
                    schema_type=schema_type,
                    imposed_by=imposed_by,
                    effective_from=effective_from,
                    datasets=datasets_list,
                )
            )
        return out


@dataclass
class SqlTradeFlowProvider:
    database_url: str
    limit: int = 500

    def fetch_trade_flows(self) -> list[TradeFlow]:
        out: list[TradeFlow] = []
        for row in _iter_raw_signals(self.database_url, "comtrade", self.limit):
            payload = row.raw_payload or {}
            reporter = payload.get("reporterISO") or payload.get("reporterCode")
            partner = payload.get("partnerISO") or payload.get("partnerCode")
            if not reporter or not partner:
                continue
            try:
                trade_value = float(payload.get("primaryValue")) if payload.get("primaryValue") is not None else None
            except (TypeError, ValueError):
                trade_value = None
            try:
                net_weight = float(payload.get("netWgt")) if payload.get("netWgt") is not None else None
            except (TypeError, ValueError):
                net_weight = None
            out.append(
                TradeFlow(
                    reporter_iso3=str(reporter),
                    partner_iso3=str(partner),
                    commodity_code=str(payload.get("cmdCode") or ""),
                    period=str(payload.get("period") or ""),
                    trade_value_usd=trade_value,
                    net_weight_kg=net_weight,
                )
            )
        return out


@dataclass
class SqlTankerProvider:
    """Hydrate tanker positions from ingested AIS-style raw signals.

    Accepts multiple source names so connector naming can evolve without
    breaking the Digital Twin contract.
    """

    database_url: str
    limit: int = 400
    sources: tuple[str, ...] = ("ais_stream", "aisstream", "ais", "aishub", "marinetraffic")

    def _iter_rows(self) -> Iterable[Any]:
        engine = get_engine(self.database_url)
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    RawSignalRow.id,
                    RawSignalRow.source,
                    RawSignalRow.source_id,
                    RawSignalRow.signal_ts,
                    RawSignalRow.entities_hint,
                    RawSignalRow.raw_payload,
                )
                .where(RawSignalRow.source.in_(self.sources))
                .order_by(RawSignalRow.signal_ts.desc())
                .limit(self.limit)
            ).all()
        return rows

    @staticmethod
    def _as_dict(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str):
            try:
                out = json.loads(payload)
                if isinstance(out, dict):
                    return out
            except Exception:
                return {}
        return {}

    @staticmethod
    def _to_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def fetch_tankers(self) -> list[Tanker]:
        out: list[Tanker] = []
        seen: set[str] = set()
        for row in self._iter_rows():
            payload = self._as_dict(row.raw_payload)

            mmsi = str(
                payload.get("mmsi")
                or payload.get("MMSI")
                or payload.get("vesselMMSI")
                or row.source_id
                or ""
            ).strip()
            if not mmsi or mmsi in seen:
                continue

            lat = self._to_float(payload.get("lat") or payload.get("latitude"))
            lon = self._to_float(payload.get("lon") or payload.get("lng") or payload.get("longitude"))
            if lat is None or lon is None:
                continue

            name = str(
                payload.get("name")
                or payload.get("vessel_name")
                or payload.get("shipname")
                or payload.get("shipName")
                or mmsi
            ).strip()

            status_raw = str(payload.get("status") or payload.get("navigation_status") or "unknown").lower()
            if any(s in status_raw for s in ("anchor", "anchored")):
                status = "anchored"
            elif any(s in status_raw for s in ("laden", "loaded")):
                status = "laden"
            elif any(s in status_raw for s in ("ballast", "empty")):
                status = "ballast"
            else:
                status = "unknown"

            dwt = self._to_float(payload.get("dwt") or payload.get("deadweight") or payload.get("deadweight_tonnage"))
            destination = payload.get("destination_port_id") or payload.get("destination")
            cargo_grade_id = payload.get("cargo_grade_id") or payload.get("cargo_grade")

            out.append(
                Tanker(
                    mmsi=mmsi,
                    name=name,
                    lat=lat,
                    lon=lon,
                    destination_port_id=str(destination) if destination else None,
                    cargo_grade_id=str(cargo_grade_id) if cargo_grade_id else None,
                    dwt=dwt,
                    status=status,
                )
            )
            seen.add(mmsi)

        return out


class EmptyTankerProvider:
    """Placeholder until AIS Stream connector lands.

    Keeps the twin's schema honest without inventing fake vessel positions.
    """

    def fetch_tankers(self) -> list[Tanker]:
        return []
