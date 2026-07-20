from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from digital_twin.graph_state import Chokepoint, Port, PricePoint
from digital_twin.providers_live import (
    AlphaVantagePriceProvider,
    CompositePriceProvider,
    OpenWeatherPortEnricher,
    StormglassChokepointEnricher,
    _port_congestion_bump,
    _weather_to_risk_bump,
)
from ingestion.connectors import acled, alpha_vantage, eia, fred, guardian, reliefweb


# ---------------------------------------------------------------------------
# Ingestion connector parsers
# ---------------------------------------------------------------------------


def test_alpha_vantage_parse_payload():
    payload = {
        "name": "Crude Oil Prices: Brent",
        "interval": "daily",
        "unit": "dollars per barrel",
        "data": [
            {"date": "2026-07-14", "value": "84.32"},
            {"date": "2026-07-13", "value": "83.90"},
            {"date": "2026-07-12", "value": "."},   # skipped
        ],
    }
    signals = alpha_vantage.parse_payload(payload, grade_label="Brent")
    assert len(signals) == 2
    assert signals[0].source == "alpha_vantage_prices"
    assert signals[0].raw_payload["value"] == 84.32
    assert signals[0].entities_hint == ["Brent"]
    assert signals[0].source_id == "alpha_vantage:brent:2026-07-14"
    assert signals[0].timestamp.year == 2026


def test_guardian_parse_payload():
    payload = {
        "response": {
            "results": [
                {
                    "id": "world/2026/jul/14/hormuz-crisis",
                    "webTitle": "Iran threatens Strait of Hormuz closure",
                    "webUrl": "https://www.theguardian.com/x",
                    "webPublicationDate": "2026-07-14T09:00:00Z",
                    "sectionName": "World news",
                }
            ]
        }
    }
    signals = guardian.parse_payload(payload)
    assert len(signals) == 1
    assert signals[0].source == "guardian"
    assert "Iran threatens" in signals[0].entities_hint[0]
    assert signals[0].source_id == "world/2026/jul/14/hormuz-crisis"
    assert signals[0].timestamp == datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)


def test_acled_parse_payload():
    payload = {
        "data": [
            {
                "event_id_cnty": "IRN12345",
                "event_date": "2026-07-10",
                "event_type": "Battles",
                "sub_event_type": "Armed clash",
                "country": "Iran",
                "actor1": "IRGC",
                "actor2": "Unknown Armed Group",
                "location": "Bandar Abbas",
                "fatalities": "0",
            }
        ]
    }
    signals = acled.parse_payload(payload)
    assert len(signals) == 1
    assert signals[0].source == "acled"
    assert signals[0].source_id == "IRN12345"
    assert "Iran" in signals[0].entities_hint
    assert signals[0].timestamp == datetime(2026, 7, 10, tzinfo=timezone.utc)


def test_reliefweb_parse_payload():
    payload = {
        "data": [
            {
                "id": "9876",
                "fields": {
                    "title": "Persian Gulf shipping disrupted",
                    "date": {"created": "2026-07-13T00:00:00+00:00"},
                    "country": [{"name": "Iran"}, {"name": "Iraq"}],
                    "source": [{"name": "OCHA"}],
                },
            }
        ]
    }
    signals = reliefweb.parse_payload(payload)
    assert len(signals) == 1
    assert signals[0].source == "reliefweb"
    assert signals[0].source_id == "9876"
    assert "Iran" in signals[0].entities_hint and "Iraq" in signals[0].entities_hint


def test_eia_parse_payload():
    payload = {
        "response": {
            "name": "Weekly Crude Stocks",
            "data": [
                {
                    "period": "2026-07-11",
                    "value": 452000,
                    "series": "WCESTUS1",
                    "series-description": "U.S. Crude Oil Ending Stocks",
                    "duoarea": "NUS",
                },
                {"period": "2026-07-04", "value": 451000, "series": "WCESTUS1"},
            ]
        }
    }
    signals = eia.parse_payload(payload)
    assert len(signals) == 2
    assert signals[0].source == "eia"
    assert signals[0].source_id == "eia:WCESTUS1:2026-07-11"


def test_fred_parse_payload():
    payload = {
        "observations": [
            {"date": "2026-07-14", "value": "84.20"},
            {"date": "2026-07-13", "value": "."},
            {"date": "2026-07-12", "value": "83.50"},
        ]
    }
    signals = fred.parse_payload(payload, series_id="DCOILBRENTEU")
    assert len(signals) == 2
    assert signals[0].source == "fred"
    assert signals[0].raw_payload["value"] == 84.20
    assert signals[0].source_id == "fred:DCOILBRENTEU:2026-07-14"


# ---------------------------------------------------------------------------
# Digital-Twin live providers/enrichers
# ---------------------------------------------------------------------------


def test_alpha_vantage_price_provider_wraps_series():
    provider = AlphaVantagePriceProvider(api_key="test-key")
    with patch(
        "digital_twin.providers_live._fetch_alpha_vantage_series",
        return_value=[{"date": "2026-07-14", "value": "84.5"}, {"date": "2026-07-13", "value": "83.9"}],
    ):
        prices = provider.fetch_prices()
    assert len(prices) == 2 * len(provider.grades)
    grade_ids = {p.grade_id for p in prices}
    assert grade_ids == set(provider.grades)


def test_alpha_vantage_provider_returns_empty_without_key():
    provider = AlphaVantagePriceProvider(api_key="")
    assert provider.fetch_prices() == []


def test_composite_price_provider_concatenates():
    p1 = AlphaVantagePriceProvider(api_key="")
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)

    class _StaticProvider:
        def fetch_prices(self):
            return [PricePoint(grade_id="grade_brent", price_usd_per_bbl=80.0, as_of=now)]

    combined = CompositePriceProvider(p1, _StaticProvider())
    prices = combined.fetch_prices()
    assert len(prices) == 1
    assert prices[0].price_usd_per_bbl == 80.0


def test_weather_to_risk_bump_scales_with_severity():
    calm = _weather_to_risk_bump({"windSpeed": {"noaa": 5.0}, "waveHeight": {"noaa": 0.5}, "visibility": {"noaa": 10}})
    stormy = _weather_to_risk_bump({"windSpeed": {"noaa": 25.0}, "waveHeight": {"noaa": 5.0}, "visibility": {"noaa": 1.0}})
    assert calm == 0.0
    assert stormy > 0.2


def test_stormglass_enricher_bumps_chokepoint_risk():
    enricher = StormglassChokepointEnricher(api_key="test-key")
    cps = [Chokepoint(id="cp_hormuz", name="Hormuz", lat=26.5, lon=56.2, throughput_mbd=20.5, risk_score=0.3)]
    stormy_hour = {"windSpeed": {"noaa": 24.0}, "waveHeight": {"noaa": 4.5}, "visibility": {"noaa": 1.0}}
    with patch("digital_twin.providers_live._fetch_stormglass_point", return_value=stormy_hour):
        enriched = enricher.enrich(cps)
    assert enriched[0].risk_score > cps[0].risk_score


def test_stormglass_enricher_noop_without_key():
    enricher = StormglassChokepointEnricher(api_key="")
    cps = [Chokepoint(id="cp_hormuz", name="Hormuz", lat=26.5, lon=56.2, throughput_mbd=20.5, risk_score=0.3)]
    assert enricher.enrich(cps) == cps


def test_openweather_port_enricher_bumps_congestion_for_indian_ports():
    enricher = OpenWeatherPortEnricher(api_key="test-key", only_country_iso3="IND")
    ports = [
        Port(id="port_sikka", name="Sikka", country_iso3="IND", lat=22.4, lon=69.8, congestion_pct=10.0),
        Port(id="port_ras_tanura", name="Ras Tanura", country_iso3="SAU", lat=26.6, lon=50.1, congestion_pct=5.0),
    ]
    stormy = {
        "wind": {"speed": 20.0},
        "visibility": 2000,
        "weather": [{"id": 502, "main": "Rain"}],
    }
    with patch("digital_twin.providers_live._fetch_openweather", return_value=stormy):
        enriched = enricher.enrich(ports)

    by_id = {p.id: p for p in enriched}
    # Sikka (Indian) is enriched
    assert by_id["port_sikka"].congestion_pct > 10.0
    # Ras Tanura (SAU) is skipped when only_country_iso3="IND"
    assert by_id["port_ras_tanura"].congestion_pct == 5.0


def test_port_congestion_bump_math():
    calm = _port_congestion_bump({"wind": {"speed": 5.0}, "visibility": 10000, "weather": [{"id": 800}]})
    stormy = _port_congestion_bump({"wind": {"speed": 20.0}, "visibility": 500, "weather": [{"id": 200}]})
    assert calm == 0.0
    assert stormy > 25.0
    # Capped at 40
    assert stormy <= 40.0
