"""Digital Twin builder + synchronizer.

`build_digital_twin` hydrates a fresh `DigitalTwinState` from seed topology +
SQL-backed dynamic providers. `refresh_digital_twin` re-runs only the dynamic
slices in-place, cheap enough for a periodic scheduler.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from digital_twin import seed_data
from digital_twin.graph_state import DigitalTwinState, ProvenanceEntry
from digital_twin.providers import (
    EmptyTankerProvider,
    PriceProvider,
    SanctionsProvider,
    SqlPriceProvider,
    SqlSanctionsProvider,
    SqlTankerProvider,
    SqlTradeFlowProvider,
    TankerProvider,
    TradeFlowProvider,
)
from digital_twin.providers_live import (
    AlphaVantagePriceProvider,
    ChokepointEnricher,
    CompositePriceProvider,
    OpenWeatherPortEnricher,
    PortEnricher,
    StormglassChokepointEnricher,
)


def _prov(name: str, source: str, count: int) -> ProvenanceEntry:
    return ProvenanceEntry(slice_name=name, source=source, row_count=count)


def _default_live_enrichers() -> tuple[list[ChokepointEnricher], list[PortEnricher]]:
    """Instantiate live enrichers only when the relevant env vars are set."""
    chokepoint_enrichers: list[ChokepointEnricher] = []
    port_enrichers: list[PortEnricher] = []

    stormglass_key = (os.getenv("STORMGLASS_API_KEY") or "").strip()
    if stormglass_key:
        chokepoint_enrichers.append(StormglassChokepointEnricher(api_key=stormglass_key))

    openweather_key = (os.getenv("OPENWEATHER_API_KEY") or "").strip()
    if openweather_key:
        port_enrichers.append(OpenWeatherPortEnricher(api_key=openweather_key))

    return chokepoint_enrichers, port_enrichers


def _default_price_provider(database_url: str | None) -> PriceProvider | None:
    alpha_key = (os.getenv("ALPHA_VANTAGE_API_KEY") or "").strip()
    providers = []
    if database_url:
        providers.append(SqlPriceProvider(database_url=database_url))
    if alpha_key:
        # Live provider runs *after* the SQL one so its rows override for
        # matching grades (grade_brent / grade_wti).
        providers.append(AlphaVantagePriceProvider(api_key=alpha_key))
    if not providers:
        return None
    if len(providers) == 1:
        return providers[0]
    return CompositePriceProvider(*providers)


def build_digital_twin(
    database_url: str | None,
    *,
    price_provider: PriceProvider | None = None,
    sanctions_provider: SanctionsProvider | None = None,
    trade_flow_provider: TradeFlowProvider | None = None,
    tanker_provider: TankerProvider | None = None,
    chokepoint_enrichers: list[ChokepointEnricher] | None = None,
    port_enrichers: list[PortEnricher] | None = None,
    enable_live_enrichers: bool = True,
) -> DigitalTwinState:
    """Assemble a live `DigitalTwinState`.

    Static topology is always seeded. Dynamic slices come from injected
    providers; when `enable_live_enrichers=True` and the relevant env vars
    (STORMGLASS_API_KEY, OPENWEATHER_API_KEY, ALPHA_VANTAGE_API_KEY) are
    set, live enrichers are auto-wired.
    """
    if price_provider is None:
        price_provider = _default_price_provider(database_url)
    if sanctions_provider is None and database_url:
        sanctions_provider = SqlSanctionsProvider(database_url=database_url)
    if trade_flow_provider is None and database_url:
        trade_flow_provider = SqlTradeFlowProvider(database_url=database_url)
    if tanker_provider is None:
        tanker_provider = SqlTankerProvider(database_url=database_url) if database_url else EmptyTankerProvider()

    auto_cp, auto_ports = ([], [])
    if enable_live_enrichers:
        auto_cp, auto_ports = _default_live_enrichers()
    chokepoint_enrichers = (chokepoint_enrichers or []) + auto_cp
    port_enrichers = (port_enrichers or []) + auto_ports

    countries = seed_data.seed_countries()
    ports = seed_data.seed_ports()
    chokepoints = seed_data.seed_chokepoints()
    routes = seed_data.seed_routes()
    grades = seed_data.seed_crude_grades()
    refineries = seed_data.seed_refineries()
    spr_sites = seed_data.seed_spr_sites()
    supplier_caps = seed_data.seed_supplier_capacities()

    for enricher in chokepoint_enrichers:
        try:
            chokepoints = enricher.enrich(chokepoints)
        except Exception:
            continue
    for enricher in port_enrichers:
        try:
            ports = enricher.enrich(ports)
        except Exception:
            continue

    prices = price_provider.fetch_prices() if price_provider else []
    sanctions = sanctions_provider.fetch_sanctions() if sanctions_provider else []
    trade_flows = trade_flow_provider.fetch_trade_flows() if trade_flow_provider else []
    tankers = tanker_provider.fetch_tankers() if tanker_provider else []

    provenance = [
        _prov("countries", "seed", len(countries)),
        _prov("ports", "seed+enrichers", len(ports)) if port_enrichers else _prov("ports", "seed", len(ports)),
        _prov("chokepoints", "seed+enrichers", len(chokepoints)) if chokepoint_enrichers else _prov("chokepoints", "seed", len(chokepoints)),
        _prov("routes", "seed", len(routes)),
        _prov("crude_grades", "seed", len(grades)),
        _prov("refineries", "seed", len(refineries)),
        _prov("spr_sites", "seed", len(spr_sites)),
        _prov("supplier_capacities", "seed", len(supplier_caps)),
        _prov(
            "prices",
            price_provider.__class__.__name__ if price_provider else "empty",
            len(prices),
        ),
        _prov(
            "sanctions",
            f"sql:raw_signals[opensanctions]" if database_url else "empty",
            len(sanctions),
        ),
        _prov(
            "trade_flows",
            f"sql:raw_signals[comtrade]" if database_url else "empty",
            len(trade_flows),
        ),
        _prov("tankers", tanker_provider.__class__.__name__, len(tankers)),
    ]

    return DigitalTwinState(
        as_of_utc=datetime.now(timezone.utc),
        branch_id="live",
        countries=countries,
        ports=ports,
        chokepoints=chokepoints,
        routes=routes,
        tankers=tankers,
        crude_grades=grades,
        refineries=refineries,
        spr_sites=spr_sites,
        sanctions=sanctions,
        prices=prices,
        supplier_capacities=supplier_caps,
        trade_flows=trade_flows,
        provenance=provenance,
    )


def refresh_digital_twin(
    state: DigitalTwinState,
    database_url: str | None,
    *,
    price_provider: PriceProvider | None = None,
    sanctions_provider: SanctionsProvider | None = None,
    trade_flow_provider: TradeFlowProvider | None = None,
    tanker_provider: TankerProvider | None = None,
) -> DigitalTwinState:
    """Refresh only the dynamic slices of an existing twin (in a new object).

    Static topology is carried over verbatim; live slices are re-fetched.
    Keeps `branch_id` and `parent_branch_id` intact so scenario branches can
    also be refreshed against evolving live data.
    """
    if price_provider is None:
        price_provider = _default_price_provider(database_url)
    if sanctions_provider is None and database_url:
        sanctions_provider = SqlSanctionsProvider(database_url=database_url)
    if trade_flow_provider is None and database_url:
        trade_flow_provider = SqlTradeFlowProvider(database_url=database_url)
    if tanker_provider is None:
        tanker_provider = SqlTankerProvider(database_url=database_url) if database_url else EmptyTankerProvider()

    prices = price_provider.fetch_prices() if price_provider else state.prices
    sanctions = sanctions_provider.fetch_sanctions() if sanctions_provider else state.sanctions
    trade_flows = trade_flow_provider.fetch_trade_flows() if trade_flow_provider else state.trade_flows
    tankers = tanker_provider.fetch_tankers() if tanker_provider else state.tankers

    provenance = [
        _prov("countries", "seed", len(state.countries)),
        _prov("ports", "seed", len(state.ports)),
        _prov("chokepoints", "seed", len(state.chokepoints)),
        _prov("routes", "seed", len(state.routes)),
        _prov("crude_grades", "seed", len(state.crude_grades)),
        _prov("refineries", "seed", len(state.refineries)),
        _prov("spr_sites", "seed", len(state.spr_sites)),
        _prov("supplier_capacities", "seed", len(state.supplier_capacities)),
        _prov(
            "prices",
            price_provider.__class__.__name__ if price_provider else "empty",
            len(prices),
        ),
        _prov(
            "sanctions",
            "sql:raw_signals[opensanctions]" if database_url else "empty",
            len(sanctions),
        ),
        _prov(
            "trade_flows",
            "sql:raw_signals[comtrade]" if database_url else "empty",
            len(trade_flows),
        ),
        _prov("tankers", tanker_provider.__class__.__name__, len(tankers)),
    ]

    return state.model_copy(
        update={
            "as_of_utc": datetime.now(timezone.utc),
            "prices": prices,
            "sanctions": sanctions,
            "trade_flows": trade_flows,
            "tankers": tankers,
            "provenance": provenance,
        }
    )
