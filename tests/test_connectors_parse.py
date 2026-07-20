from datetime import timezone

from ingestion.connectors.comtrade import parse_payload as parse_comtrade
from ingestion.connectors.gdelt import parse_payload as parse_gdelt
from ingestion.connectors.newsapi import parse_payload as parse_newsapi
from ingestion.connectors.prices import parse_payload as parse_prices
from ingestion.connectors.sanctions import parse_payload as parse_sanctions
from ingestion.schemas.raw_signal import RawSignal


def _assert_common(signal: RawSignal, expected_source: str) -> None:
    assert signal.source == expected_source
    assert signal.timestamp.tzinfo == timezone.utc
    assert isinstance(signal.raw_payload, dict)
    assert isinstance(signal.entities_hint, list)


def test_parse_gdelt_payload() -> None:
    payload = {
        "articles": [
            {
                "url": "https://example.com/a",
                "seendate": "2026-07-10T09:30:00Z",
                "sourcecountry": "India",
                "domain": "example.com",
            }
        ]
    }
    out = parse_gdelt(payload)
    assert len(out) == 1
    _assert_common(out[0], "gdelt_doc")
    assert out[0].source_id == "https://example.com/a"


def test_parse_newsapi_payload() -> None:
    payload = {
        "articles": [
            {
                "source": {"name": "News Desk"},
                "title": "Shipping risk rises",
                "publishedAt": "2026-07-10T10:10:10Z",
                "url": "https://news.example.com/1",
            }
        ]
    }
    out = parse_newsapi(payload)
    assert len(out) == 1
    _assert_common(out[0], "newsapi")
    assert out[0].source_id == "https://news.example.com/1"


def test_parse_sanctions_payload() -> None:
    payload = {
        "results": [
            {
                "id": "Q123",
                "caption": "Example Tanker",
                "schema": "Vessel",
                "datasets": ["sanctions"],
            }
        ]
    }
    out = parse_sanctions(payload)
    assert len(out) == 1
    _assert_common(out[0], "opensanctions")
    assert out[0].source_id == "Q123"


def test_parse_comtrade_payload() -> None:
    payload = {
        "data": [
            {
                "cmdCode": "2709",
                "period": "2025",
                "reporterCode": 356,
                "partnerCode": 0,
                "reporterDesc": "India",
                "partnerDesc": "World",
                "cmdDesc": "Petroleum oils",
            }
        ]
    }
    out = parse_comtrade(payload)
    assert len(out) == 1
    _assert_common(out[0], "comtrade")
    assert out[0].source_id is not None


def test_parse_prices_payload() -> None:
    payload = [
        {"page": 1},
        [
            {
                "date": "2025",
                "value": 78.1,
                "indicator": {"value": "Crude oil, Brent"},
            }
        ],
    ]
    out = parse_prices(payload)
    assert len(out) == 1
    _assert_common(out[0], "world_bank_prices")
    assert out[0].source_id == "worldbank:2025"