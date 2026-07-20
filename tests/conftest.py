"""Test-suite defaults.

Autouse fixture that scrubs *live-API* env vars so no test accidentally
issues real network calls. Individual tests can opt back in by setting the
variables explicitly via `monkeypatch.setenv(...)`.
"""
from __future__ import annotations

import os

import pytest


_LIVE_API_ENV_VARS = (
    "ALPHA_VANTAGE_API_KEY",
    "STORMGLASS_API_KEY",
    "OPENWEATHER_API_KEY",
    "AIS_STREAM_API_KEY",
    "AIS_AISHUB_USERNAME",
    "AIS_MARINETRAFFIC_API_KEY",
    "GUARDIAN_API_KEY",
    "ACLED_API_KEY",
    "ACLED_EMAIL",
    "EIA_API_KEY",
    "FRED_API_KEY",
    "POLYGON_API_KEY",
    "NASDAQ_DATA_LINK_API_KEY",
    "MEDIASTACK_API_KEY",
    "OPENAQ_API_KEY",
    "FX_API_KEY",
)


@pytest.fixture(autouse=True)
def _no_live_api_calls(monkeypatch):
    """Clear all live-API env vars so build_digital_twin doesn't fan out."""
    for key in _LIVE_API_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    yield
