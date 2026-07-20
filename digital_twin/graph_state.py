"""Pydantic models for the Digital Twin state graph.

`DigitalTwinState` is the canonical world-model snapshot consumed by every
downstream agent in Layer 4 (hypothesis, scenario, procurement, policy,
refinery). Everything here is data-only; no I/O.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


CountryRole = Literal["producer", "consumer", "transit", "hybrid"]


class Country(BaseModel):
    iso3: str
    name: str
    role: CountryRole
    production_capacity_kbd: float | None = None
    consumption_kbd: float | None = None


class Port(BaseModel):
    id: str
    name: str
    country_iso3: str
    lat: float
    lon: float
    draft_m: float | None = None
    congestion_pct: float = 0.0
    status: Literal["open", "restricted", "closed"] = "open"


class Chokepoint(BaseModel):
    id: str
    name: str
    lat: float
    lon: float
    throughput_mbd: float
    risk_score: float = 0.0  # 0..1
    status: Literal["open", "restricted", "closed"] = "open"


class Route(BaseModel):
    id: str
    origin_port_id: str
    destination_port_id: str
    chokepoint_ids: list[str] = Field(default_factory=list)
    distance_nm: float
    transit_days: float
    insurance_zone: str | None = None
    insurance_premium_multiplier: float = 1.0
    risk_score: float = 0.0  # 0..1


class Tanker(BaseModel):
    mmsi: str
    name: str
    lat: float | None = None
    lon: float | None = None
    destination_port_id: str | None = None
    cargo_grade_id: str | None = None
    dwt: float | None = None
    status: Literal["laden", "ballast", "anchored", "unknown"] = "unknown"


class CrudeGrade(BaseModel):
    id: str
    name: str
    source_country_iso3: str
    api_gravity: float | None = None
    sulphur_pct: float | None = None


class Refinery(BaseModel):
    id: str
    name: str
    operator: str
    country_iso3: str
    capacity_kbd: float
    lat: float | None = None
    lon: float | None = None
    compatible_grade_ids: list[str] = Field(default_factory=list)
    utilization_pct: float = 0.85
    status: Literal["online", "reduced", "offline"] = "online"


class SPRSite(BaseModel):
    id: str
    name: str
    country_iso3: str
    capacity_mbbl: float
    current_fill_mbbl: float
    max_drawdown_mbd: float


class SanctionEntry(BaseModel):
    entity: str
    schema_type: str | None = None
    imposed_by: list[str] = Field(default_factory=list)
    effective_from: datetime | None = None
    datasets: list[str] = Field(default_factory=list)


class PricePoint(BaseModel):
    grade_id: str
    price_usd_per_bbl: float
    as_of: datetime


class SupplierCapacity(BaseModel):
    country_iso3: str
    grade_id: str | None = None
    spare_capacity_kbd: float
    contract_ceiling_kbd: float | None = None


class TradeFlow(BaseModel):
    reporter_iso3: str
    partner_iso3: str
    commodity_code: str
    period: str  # YYYY or YYYY-MM
    trade_value_usd: float | None = None
    net_weight_kg: float | None = None


class ProvenanceEntry(BaseModel):
    slice_name: str
    source: str  # e.g. "sql:raw_signals[world_bank_prices]", "seed", "override"
    row_count: int
    fetched_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DigitalTwinState(BaseModel):
    """Canonical snapshot of India's crude oil supply chain."""

    as_of_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    branch_id: str = "live"
    parent_branch_id: str | None = None

    countries: list[Country] = Field(default_factory=list)
    ports: list[Port] = Field(default_factory=list)
    chokepoints: list[Chokepoint] = Field(default_factory=list)
    routes: list[Route] = Field(default_factory=list)
    tankers: list[Tanker] = Field(default_factory=list)
    crude_grades: list[CrudeGrade] = Field(default_factory=list)
    refineries: list[Refinery] = Field(default_factory=list)
    spr_sites: list[SPRSite] = Field(default_factory=list)
    sanctions: list[SanctionEntry] = Field(default_factory=list)
    prices: list[PricePoint] = Field(default_factory=list)
    supplier_capacities: list[SupplierCapacity] = Field(default_factory=list)
    trade_flows: list[TradeFlow] = Field(default_factory=list)

    provenance: list[ProvenanceEntry] = Field(default_factory=list)

    # ---- convenience lookups (not persisted, computed on demand) ------------

    def port_by_id(self, port_id: str) -> Port | None:
        return next((p for p in self.ports if p.id == port_id), None)

    def chokepoint_by_id(self, cp_id: str) -> Chokepoint | None:
        return next((c for c in self.chokepoints if c.id == cp_id), None)

    def refinery_by_id(self, refinery_id: str) -> Refinery | None:
        return next((r for r in self.refineries if r.id == refinery_id), None)

    def grade_by_id(self, grade_id: str) -> CrudeGrade | None:
        return next((g for g in self.crude_grades if g.id == grade_id), None)

    def latest_price(self, grade_id: str) -> PricePoint | None:
        matches = [p for p in self.prices if p.grade_id == grade_id]
        if not matches:
            return None
        return max(matches, key=lambda p: p.as_of)

    def summary(self) -> dict[str, int]:
        return {
            "countries": len(self.countries),
            "ports": len(self.ports),
            "chokepoints": len(self.chokepoints),
            "routes": len(self.routes),
            "tankers": len(self.tankers),
            "crude_grades": len(self.crude_grades),
            "refineries": len(self.refineries),
            "spr_sites": len(self.spr_sites),
            "sanctions": len(self.sanctions),
            "prices": len(self.prices),
            "supplier_capacities": len(self.supplier_capacities),
            "trade_flows": len(self.trade_flows),
        }
