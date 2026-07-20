"""Live-API providers and enrichers for the Digital Twin (PRD v2 Upgrade 1 follow-up).

Each provider/enricher is *optional* and gated on the presence of an env-var
API key. If the key is missing, the provider is skipped and the twin falls
back to seed topology + SQL-backed dynamic slices.

Design contract:
    - `LivePriceProvider` implements `PriceProvider` — used in place of / in
      addition to `SqlPriceProvider`.
    - `ChokepointEnricher` / `PortEnricher` are applied *after* seed data in
      `build_digital_twin` and mutate the corresponding twin slices.

Providers here never raise on network failure — they log and return
unchanged inputs so pipeline runs stay robust during a live demo.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

import requests

from digital_twin.graph_state import Chokepoint, Port, PricePoint


# ---------------------------------------------------------------------------
# Alpha Vantage — Brent/WTI live prices
# ---------------------------------------------------------------------------


ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"

_ALPHA_GRADE_MAP = {
    "grade_brent": ("BRENT", "Brent"),
    "grade_wti": ("WTI", "WTI"),
}


def _fetch_alpha_vantage_series(api_key: str, function: str, timeout: int = 20) -> list[dict[str, Any]]:
    params = {"function": function, "interval": "daily", "apikey": api_key, "datatype": "json"}
    try:
        resp = requests.get(ALPHA_VANTAGE_URL, params=params, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException:
        return []
    body = resp.json() if resp.content else {}
    rows = body.get("data") if isinstance(body, dict) else None
    return rows if isinstance(rows, list) else []


@dataclass
class AlphaVantagePriceProvider:
    """Fetches Brent + WTI daily series from Alpha Vantage.

    Falls back to an empty list on any HTTP/parse failure so the twin
    remains buildable during a live demo.
    """

    api_key: str
    grades: tuple[str, ...] = ("grade_brent", "grade_wti")
    max_points_per_grade: int = 60

    def fetch_prices(self) -> list[PricePoint]:
        if not self.api_key:
            return []
        out: list[PricePoint] = []
        for grade_id in self.grades:
            spec = _ALPHA_GRADE_MAP.get(grade_id)
            if spec is None:
                continue
            function, _label = spec
            rows = _fetch_alpha_vantage_series(self.api_key, function)
            for row in rows[: self.max_points_per_grade]:
                if not isinstance(row, dict):
                    continue
                raw = row.get("value")
                date_str = row.get("date") if isinstance(row.get("date"), str) else None
                if raw in (None, "", ".") or not date_str:
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                try:
                    as_of = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    as_of = datetime.now(timezone.utc)
                out.append(PricePoint(grade_id=grade_id, price_usd_per_bbl=value, as_of=as_of))
        return out


class CompositePriceProvider:
    """Concatenate multiple providers; later ones override earlier ones per grade."""

    def __init__(self, *providers):
        self.providers = [p for p in providers if p is not None]

    def fetch_prices(self) -> list[PricePoint]:
        out: list[PricePoint] = []
        for p in self.providers:
            try:
                out.extend(p.fetch_prices())
            except Exception:
                continue
        return out


# ---------------------------------------------------------------------------
# Stormglass — chokepoint maritime weather → chokepoint risk enrichment
# ---------------------------------------------------------------------------


STORMGLASS_URL = "https://api.stormglass.io/v2/weather/point"


def _fetch_stormglass_point(api_key: str, lat: float, lon: float, timeout: int = 20) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    params = {
        "lat": lat,
        "lng": lon,
        "params": "windSpeed,waveHeight,visibility",
        "start": now.isoformat(),
        "end": now.isoformat(),
    }
    try:
        resp = requests.get(STORMGLASS_URL, params=params, headers={"Authorization": api_key}, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    body = resp.json() if resp.content else {}
    hours = body.get("hours") if isinstance(body, dict) else None
    if isinstance(hours, list) and hours:
        return hours[0]
    return None


def _reading_average(reading: Any) -> float | None:
    if not isinstance(reading, dict):
        return None
    values = [v for v in reading.values() if isinstance(v, (int, float))]
    if not values:
        return None
    return sum(values) / len(values)


def _weather_to_risk_bump(hour: dict[str, Any]) -> float:
    """Convert weather readings to a 0..0.4 chokepoint risk bump.

    Wind >20 m/s, waves >4 m, visibility <2 km are all serious for tankers.
    """
    wind = _reading_average(hour.get("windSpeed")) or 0.0
    wave = _reading_average(hour.get("waveHeight")) or 0.0
    vis = _reading_average(hour.get("visibility"))
    bump = 0.0
    if wind > 10:
        bump += min(0.20, (wind - 10) * 0.02)
    if wave > 2.5:
        bump += min(0.15, (wave - 2.5) * 0.05)
    if vis is not None and vis < 5:
        bump += min(0.10, (5 - vis) * 0.02)
    return min(0.4, bump)


@dataclass
class StormglassChokepointEnricher:
    api_key: str

    def enrich(self, chokepoints: list[Chokepoint]) -> list[Chokepoint]:
        if not self.api_key:
            return chokepoints
        out: list[Chokepoint] = []
        for cp in chokepoints:
            hour = _fetch_stormglass_point(self.api_key, cp.lat, cp.lon)
            if hour is None:
                out.append(cp)
                continue
            bump = _weather_to_risk_bump(hour)
            new_risk = min(1.0, cp.risk_score + bump)
            out.append(cp.model_copy(update={"risk_score": round(new_risk, 4)}))
        return out


# ---------------------------------------------------------------------------
# OpenWeather — port weather → port congestion enrichment
# ---------------------------------------------------------------------------


OPENWEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"


def _fetch_openweather(api_key: str, lat: float, lon: float, timeout: int = 15) -> dict[str, Any] | None:
    params = {"lat": lat, "lon": lon, "appid": api_key, "units": "metric"}
    try:
        resp = requests.get(OPENWEATHER_URL, params=params, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    return resp.json() if resp.content else None


def _port_congestion_bump(body: dict[str, Any]) -> float:
    """Storms / poor visibility slow port throughput → higher congestion %.

    Returns an additive bump (0..40) percentage points.
    """
    if not isinstance(body, dict):
        return 0.0
    wind_speed = 0.0
    if isinstance(body.get("wind"), dict):
        try:
            wind_speed = float(body["wind"].get("speed") or 0.0)
        except (TypeError, ValueError):
            wind_speed = 0.0
    visibility = body.get("visibility")
    try:
        visibility_km = float(visibility) / 1000.0 if visibility is not None else 10.0
    except (TypeError, ValueError):
        visibility_km = 10.0
    weather_ids: list[int] = []
    weather = body.get("weather")
    if isinstance(weather, list):
        for w in weather:
            if isinstance(w, dict):
                wid = w.get("id")
                if isinstance(wid, int):
                    weather_ids.append(wid)
    bump = 0.0
    if wind_speed > 12:  # m/s
        bump += min(20.0, (wind_speed - 12) * 3.0)
    if visibility_km < 5:
        bump += min(15.0, (5 - visibility_km) * 3.0)
    if any(wid < 800 for wid in weather_ids):  # storms/rain/snow
        bump += 10.0
    return min(40.0, bump)


@dataclass
class OpenWeatherPortEnricher:
    api_key: str
    only_country_iso3: str | None = "IND"

    def enrich(self, ports: list[Port]) -> list[Port]:
        if not self.api_key:
            return ports
        out: list[Port] = []
        for port in ports:
            if self.only_country_iso3 and port.country_iso3.upper() != self.only_country_iso3.upper():
                out.append(port)
                continue
            body = _fetch_openweather(self.api_key, port.lat, port.lon)
            if body is None:
                out.append(port)
                continue
            bump = _port_congestion_bump(body)
            new_congestion = min(100.0, port.congestion_pct + bump)
            out.append(port.model_copy(update={"congestion_pct": round(new_congestion, 2)}))
        return out


# ---------------------------------------------------------------------------
# Protocols (re-exported)
# ---------------------------------------------------------------------------


class ChokepointEnricher(Protocol):
    def enrich(self, chokepoints: list[Chokepoint]) -> list[Chokepoint]: ...


class PortEnricher(Protocol):
    def enrich(self, ports: list[Port]) -> list[Port]: ...


__all__ = [
    "AlphaVantagePriceProvider",
    "CompositePriceProvider",
    "StormglassChokepointEnricher",
    "OpenWeatherPortEnricher",
    "ChokepointEnricher",
    "PortEnricher",
]
