"""Curated seed data for the Digital Twin's static physical topology.

Countries, ports, chokepoints, routes, refineries, SPR sites, and crude grades
change slowly (or not at all) and have no live API. They are seeded here so the
twin has a real, queryable physical graph on Day 1 while dynamic slices
(prices, sanctions, tankers, congestion) come from live connectors.

Numbers are drawn from publicly documented industry references and are
intentionally conservative — they are directionally correct, not fiscal-year
exact. Refresh from official sources before production use.
"""
from __future__ import annotations

from digital_twin.graph_state import (
    Chokepoint,
    Country,
    CrudeGrade,
    Port,
    Refinery,
    Route,
    SPRSite,
    SupplierCapacity,
)


def seed_countries() -> list[Country]:
    return [
        Country(iso3="IND", name="India", role="consumer", consumption_kbd=5300.0),
        Country(iso3="SAU", name="Saudi Arabia", role="producer", production_capacity_kbd=12000.0),
        Country(iso3="IRQ", name="Iraq", role="producer", production_capacity_kbd=4500.0),
        Country(iso3="ARE", name="United Arab Emirates", role="producer", production_capacity_kbd=4200.0),
        Country(iso3="RUS", name="Russia", role="producer", production_capacity_kbd=10500.0),
        Country(iso3="USA", name="United States", role="producer", production_capacity_kbd=13000.0),
        Country(iso3="IRN", name="Iran", role="producer", production_capacity_kbd=3400.0),
        Country(iso3="NGA", name="Nigeria", role="producer", production_capacity_kbd=1400.0),
        Country(iso3="VEN", name="Venezuela", role="producer", production_capacity_kbd=800.0),
        Country(iso3="KWT", name="Kuwait", role="producer", production_capacity_kbd=2700.0),
        Country(iso3="OMN", name="Oman", role="hybrid", production_capacity_kbd=1050.0),
        Country(iso3="EGY", name="Egypt", role="transit"),
        Country(iso3="YEM", name="Yemen", role="transit"),
    ]


def seed_ports() -> list[Port]:
    return [
        # India — receiving ports
        Port(id="port_sikka", name="Sikka", country_iso3="IND", lat=22.4667, lon=69.8500, draft_m=32.0),
        Port(id="port_vadinar", name="Vadinar", country_iso3="IND", lat=22.4500, lon=69.7000, draft_m=32.0),
        Port(id="port_mundra", name="Mundra", country_iso3="IND", lat=22.7500, lon=69.7000, draft_m=17.5),
        Port(id="port_paradip", name="Paradip", country_iso3="IND", lat=20.3167, lon=86.6667, draft_m=18.5),
        Port(id="port_mumbai", name="Mumbai (JNPT)", country_iso3="IND", lat=18.9500, lon=72.9500, draft_m=15.0),
        Port(id="port_kochi", name="Kochi", country_iso3="IND", lat=9.9667, lon=76.2667, draft_m=14.5),
        Port(id="port_vizag", name="Visakhapatnam", country_iso3="IND", lat=17.6883, lon=83.2185, draft_m=17.0),
        Port(id="port_mangalore", name="Mangalore (NMPT)", country_iso3="IND", lat=12.9333, lon=74.8000, draft_m=15.4),
        # Exporter ports
        Port(id="port_ras_tanura", name="Ras Tanura", country_iso3="SAU", lat=26.6417, lon=50.1583, draft_m=27.0),
        Port(id="port_basrah", name="Basrah Oil Terminal", country_iso3="IRQ", lat=29.6833, lon=48.8333, draft_m=25.0),
        Port(id="port_fujairah", name="Fujairah", country_iso3="ARE", lat=25.1167, lon=56.3333, draft_m=18.0),
        Port(id="port_kharg", name="Kharg Island", country_iso3="IRN", lat=29.2333, lon=50.3167, draft_m=27.0),
        Port(id="port_novorossiysk", name="Novorossiysk", country_iso3="RUS", lat=44.7167, lon=37.7833, draft_m=24.0),
        Port(id="port_kozmino", name="Kozmino", country_iso3="RUS", lat=42.7167, lon=133.1500, draft_m=21.0),
        Port(id="port_bonny", name="Bonny", country_iso3="NGA", lat=4.4500, lon=7.1667, draft_m=17.0),
        Port(id="port_houston", name="Houston (LOOP)", country_iso3="USA", lat=29.7500, lon=-95.3500, draft_m=27.0),
    ]


def seed_chokepoints() -> list[Chokepoint]:
    return [
        Chokepoint(id="cp_hormuz", name="Strait of Hormuz", lat=26.5667, lon=56.2500, throughput_mbd=20.5, risk_score=0.35),
        Chokepoint(id="cp_bab_el_mandeb", name="Bab-el-Mandeb", lat=12.5833, lon=43.3333, throughput_mbd=8.8, risk_score=0.45),
        Chokepoint(id="cp_suez", name="Suez Canal", lat=30.5833, lon=32.2667, throughput_mbd=9.2, risk_score=0.25),
        Chokepoint(id="cp_malacca", name="Strait of Malacca", lat=2.5000, lon=101.5000, throughput_mbd=17.0, risk_score=0.15),
        Chokepoint(id="cp_bosphorus", name="Bosphorus Strait", lat=41.1167, lon=29.0667, throughput_mbd=3.2, risk_score=0.20),
    ]


def seed_crude_grades() -> list[CrudeGrade]:
    return [
        CrudeGrade(id="grade_arab_light", name="Arab Light", source_country_iso3="SAU", api_gravity=33.0, sulphur_pct=1.8),
        CrudeGrade(id="grade_arab_medium", name="Arab Medium", source_country_iso3="SAU", api_gravity=30.0, sulphur_pct=2.5),
        CrudeGrade(id="grade_basrah_medium", name="Basrah Medium", source_country_iso3="IRQ", api_gravity=29.0, sulphur_pct=2.7),
        CrudeGrade(id="grade_basrah_heavy", name="Basrah Heavy", source_country_iso3="IRQ", api_gravity=23.5, sulphur_pct=4.0),
        CrudeGrade(id="grade_murban", name="Murban", source_country_iso3="ARE", api_gravity=40.0, sulphur_pct=0.8),
        CrudeGrade(id="grade_urals", name="Urals", source_country_iso3="RUS", api_gravity=31.7, sulphur_pct=1.35),
        CrudeGrade(id="grade_espo", name="ESPO", source_country_iso3="RUS", api_gravity=34.8, sulphur_pct=0.62),
        CrudeGrade(id="grade_wti", name="WTI", source_country_iso3="USA", api_gravity=39.6, sulphur_pct=0.24),
        CrudeGrade(id="grade_brent", name="Brent", source_country_iso3="USA", api_gravity=38.3, sulphur_pct=0.37),
        CrudeGrade(id="grade_bonny_light", name="Bonny Light", source_country_iso3="NGA", api_gravity=35.4, sulphur_pct=0.15),
        CrudeGrade(id="grade_iran_heavy", name="Iranian Heavy", source_country_iso3="IRN", api_gravity=30.0, sulphur_pct=1.7),
    ]


def seed_refineries() -> list[Refinery]:
    return [
        Refinery(
            id="ref_jamnagar",
            name="Jamnagar",
            operator="Reliance Industries",
            country_iso3="IND",
            capacity_kbd=1240.0,
            lat=22.4707,
            lon=70.0577,
            compatible_grade_ids=[
                "grade_arab_light",
                "grade_arab_medium",
                "grade_basrah_medium",
                "grade_urals",
                "grade_murban",
            ],
        ),
        Refinery(
            id="ref_vadinar",
            name="Vadinar",
            operator="Nayara Energy",
            country_iso3="IND",
            capacity_kbd=405.0,
            lat=22.3600,
            lon=69.6980,
            compatible_grade_ids=["grade_urals", "grade_basrah_medium", "grade_arab_medium"],
        ),
        Refinery(
            id="ref_panipat",
            name="Panipat",
            operator="Indian Oil Corporation",
            country_iso3="IND",
            capacity_kbd=300.0,
            lat=29.3900,
            lon=76.9700,
            compatible_grade_ids=["grade_arab_light", "grade_basrah_medium", "grade_urals"],
        ),
        Refinery(
            id="ref_mathura",
            name="Mathura",
            operator="Indian Oil Corporation",
            country_iso3="IND",
            capacity_kbd=160.0,
            lat=27.4924,
            lon=77.6737,
            compatible_grade_ids=["grade_arab_light", "grade_basrah_medium"],
        ),
        Refinery(
            id="ref_vizag",
            name="Visakhapatnam",
            operator="Hindustan Petroleum",
            country_iso3="IND",
            capacity_kbd=166.0,
            lat=17.7286,
            lon=83.2185,
            compatible_grade_ids=["grade_basrah_medium", "grade_arab_medium", "grade_iran_heavy"],
        ),
        Refinery(
            id="ref_kochi",
            name="Kochi",
            operator="Bharat Petroleum",
            country_iso3="IND",
            capacity_kbd=310.0,
            lat=9.9310,
            lon=76.2673,
            compatible_grade_ids=["grade_arab_light", "grade_murban", "grade_bonny_light"],
        ),
        Refinery(
            id="ref_mangalore",
            name="Mangalore",
            operator="MRPL",
            country_iso3="IND",
            capacity_kbd=300.0,
            lat=12.8783,
            lon=74.8817,
            compatible_grade_ids=["grade_arab_medium", "grade_basrah_medium", "grade_iran_heavy"],
        ),
        Refinery(
            id="ref_paradip",
            name="Paradip",
            operator="Indian Oil Corporation",
            country_iso3="IND",
            capacity_kbd=300.0,
            lat=20.2648,
            lon=86.6099,
            compatible_grade_ids=["grade_basrah_heavy", "grade_arab_medium", "grade_urals"],
        ),
    ]


def seed_spr_sites() -> list[SPRSite]:
    return [
        SPRSite(id="spr_vizag", name="Visakhapatnam Cavern", country_iso3="IND", capacity_mbbl=9.77, current_fill_mbbl=9.77, max_drawdown_mbd=0.30),
        SPRSite(id="spr_mangalore", name="Mangalore Cavern", country_iso3="IND", capacity_mbbl=11.00, current_fill_mbbl=11.00, max_drawdown_mbd=0.35),
        SPRSite(id="spr_padur", name="Padur Cavern", country_iso3="IND", capacity_mbbl=17.00, current_fill_mbbl=17.00, max_drawdown_mbd=0.50),
    ]


def seed_routes() -> list[Route]:
    return [
        Route(
            id="route_rastanura_sikka",
            origin_port_id="port_ras_tanura",
            destination_port_id="port_sikka",
            chokepoint_ids=["cp_hormuz"],
            distance_nm=1550.0,
            transit_days=6.0,
            insurance_zone="gulf_hormuz",
        ),
        Route(
            id="route_basrah_vadinar",
            origin_port_id="port_basrah",
            destination_port_id="port_vadinar",
            chokepoint_ids=["cp_hormuz"],
            distance_nm=1650.0,
            transit_days=6.5,
            insurance_zone="gulf_hormuz",
        ),
        Route(
            id="route_fujairah_mundra",
            origin_port_id="port_fujairah",
            destination_port_id="port_mundra",
            chokepoint_ids=[],  # Fujairah bypasses Hormuz
            distance_nm=1100.0,
            transit_days=4.5,
            insurance_zone="arabian_sea",
        ),
        Route(
            id="route_kharg_mangalore",
            origin_port_id="port_kharg",
            destination_port_id="port_mangalore",
            chokepoint_ids=["cp_hormuz"],
            distance_nm=1800.0,
            transit_days=7.0,
            insurance_zone="gulf_hormuz",
        ),
        Route(
            id="route_novorossiysk_paradip",
            origin_port_id="port_novorossiysk",
            destination_port_id="port_paradip",
            chokepoint_ids=["cp_bosphorus", "cp_suez", "cp_bab_el_mandeb"],
            distance_nm=6200.0,
            transit_days=22.0,
            insurance_zone="red_sea",
        ),
        Route(
            id="route_kozmino_paradip",
            origin_port_id="port_kozmino",
            destination_port_id="port_paradip",
            chokepoint_ids=["cp_malacca"],
            distance_nm=4300.0,
            transit_days=15.0,
            insurance_zone="pacific",
        ),
        Route(
            id="route_bonny_kochi",
            origin_port_id="port_bonny",
            destination_port_id="port_kochi",
            chokepoint_ids=[],
            distance_nm=5100.0,
            transit_days=18.0,
            insurance_zone="atlantic",
        ),
        Route(
            id="route_houston_vizag",
            origin_port_id="port_houston",
            destination_port_id="port_vizag",
            chokepoint_ids=["cp_suez"],
            distance_nm=9800.0,
            transit_days=34.0,
            insurance_zone="global",
        ),
    ]


def seed_supplier_capacities() -> list[SupplierCapacity]:
    return [
        SupplierCapacity(country_iso3="SAU", grade_id="grade_arab_light", spare_capacity_kbd=1500.0, contract_ceiling_kbd=1000.0),
        SupplierCapacity(country_iso3="SAU", grade_id="grade_arab_medium", spare_capacity_kbd=800.0, contract_ceiling_kbd=600.0),
        SupplierCapacity(country_iso3="IRQ", grade_id="grade_basrah_medium", spare_capacity_kbd=700.0, contract_ceiling_kbd=900.0),
        SupplierCapacity(country_iso3="ARE", grade_id="grade_murban", spare_capacity_kbd=600.0, contract_ceiling_kbd=500.0),
        SupplierCapacity(country_iso3="RUS", grade_id="grade_urals", spare_capacity_kbd=1200.0, contract_ceiling_kbd=1800.0),
        SupplierCapacity(country_iso3="RUS", grade_id="grade_espo", spare_capacity_kbd=400.0, contract_ceiling_kbd=500.0),
        SupplierCapacity(country_iso3="USA", grade_id="grade_wti", spare_capacity_kbd=500.0, contract_ceiling_kbd=400.0),
        SupplierCapacity(country_iso3="NGA", grade_id="grade_bonny_light", spare_capacity_kbd=250.0, contract_ceiling_kbd=200.0),
    ]
