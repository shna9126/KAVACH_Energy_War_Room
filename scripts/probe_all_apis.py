"""Comprehensive KAVACH API health check.

Runs a small live query against every external API the platform depends on
(or would benefit from) and prints a table of status + latency + payload
size. Uses each API's *cheapest* endpoint so it's safe to run frequently.

Usage:
    python -m scripts.probe_all_apis            # full run
    python -m scripts.probe_all_apis --json     # machine-readable

Notes:
    - Skips APIs whose env key is empty (marked SKIP).
    - Marks APIs whose call raised an exception (marked FAIL).
    - Corporate-proxy environments frequently fail SSL — set
      PROBE_VERIFY_TLS=false to disable verification (dev only!)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


TIMEOUT_SECONDS = 20
VERIFY_TLS = os.getenv("PROBE_VERIFY_TLS", "true").strip().lower() not in {"0", "false", "no"}


@dataclass
class ProbeResult:
    name: str
    tier: str            # "critical" | "supporting" | "enrichment"
    status: str          # "ok" | "skip" | "fail"
    latency_ms: int | None = None
    detail: str = ""
    records_hint: int | None = None


def _get(url: str, *, params=None, headers=None) -> requests.Response:
    return requests.get(url, params=params, headers=headers, timeout=TIMEOUT_SECONDS, verify=VERIFY_TLS)


def _time(fn, name: str, tier: str) -> ProbeResult:
    t0 = time.perf_counter()
    try:
        result = fn()
    except requests.exceptions.SSLError as e:
        return ProbeResult(name, tier, "fail", int((time.perf_counter() - t0) * 1000), f"SSL cert error (corp proxy?): {str(e)[:60]}")
    except requests.exceptions.Timeout:
        return ProbeResult(name, tier, "fail", int((time.perf_counter() - t0) * 1000), f"timeout after {TIMEOUT_SECONDS}s")
    except requests.exceptions.ConnectionError as e:
        return ProbeResult(name, tier, "fail", int((time.perf_counter() - t0) * 1000), f"conn: {str(e)[:60]}")
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        body = ""
        try:
            body = (e.response.text or "")[:80].replace("\n", " ") if e.response is not None else ""
        except Exception:
            body = ""
        return ProbeResult(name, tier, "fail", int((time.perf_counter() - t0) * 1000), f"HTTP {code} · {body}")
    except json.JSONDecodeError as e:
        return ProbeResult(name, tier, "fail", int((time.perf_counter() - t0) * 1000), f"non-JSON response: {str(e)[:60]}")
    except Exception as e:  # noqa: BLE001
        return ProbeResult(name, tier, "fail", int((time.perf_counter() - t0) * 1000), f"{type(e).__name__}: {str(e)[:60]}")
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if isinstance(result, ProbeResult):
        result.latency_ms = elapsed_ms
        return result
    return ProbeResult(name, tier, "ok", elapsed_ms, "")


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def probe_gemini() -> ProbeResult:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return ProbeResult("Gemini (Google AI)", "critical", "skip", None, "GEMINI_API_KEY empty")
    # models.list is the lightest key-validation call available.
    r = _get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": key})
    if r.status_code == 200:
        n = len(r.json().get("models", []))
        return ProbeResult("Gemini (Google AI)", "critical", "ok", None, f"{n} models available")
    return ProbeResult("Gemini (Google AI)", "critical", "fail", None,
                       f"HTTP {r.status_code} · {(r.text or '')[:80]}")


# ---------------------------------------------------------------------------
# Ingestion — News / Events (Layer 1)
# ---------------------------------------------------------------------------

def probe_gdelt() -> ProbeResult:
    r = _get("https://api.gdeltproject.org/api/v2/doc/doc", params={
        "query": "oil", "mode": "artlist", "format": "json", "maxrecords": 3,
    })
    r.raise_for_status()
    n = len(r.json().get("articles", []))
    return ProbeResult("GDELT Doc 2.0", "critical", "ok", None, f"{n} articles", n)


def probe_newsapi() -> ProbeResult:
    key = os.getenv("NEWSAPI_KEY", "").strip() or os.getenv("NEWS_API_KEY", "").strip()
    if not key:
        return ProbeResult("NewsAPI", "supporting", "skip", None, "NEWSAPI_KEY empty")
    r = _get("https://newsapi.org/v2/everything",
             params={"q": "oil", "pageSize": 3, "language": "en"},
             headers={"X-Api-Key": key})
    r.raise_for_status()
    n = len(r.json().get("articles", []))
    return ProbeResult("NewsAPI", "supporting", "ok", None, f"{n} articles", n)


def probe_guardian() -> ProbeResult:
    key = os.getenv("GUARDIAN_API_KEY", "").strip()
    if not key:
        return ProbeResult("The Guardian", "supporting", "skip", None, "GUARDIAN_API_KEY empty")
    r = _get("https://content.guardianapis.com/search",
             params={"q": "oil crude", "page-size": 3, "api-key": key})
    r.raise_for_status()
    n = len(r.json().get("response", {}).get("results", []))
    return ProbeResult("The Guardian", "supporting", "ok", None, f"{n} results", n)


def probe_acled() -> ProbeResult:
    key = os.getenv("ACLED_API_KEY", "").strip()
    email = os.getenv("ACLED_EMAIL", "").strip()
    if not key or not email:
        return ProbeResult("ACLED (conflict events)", "critical", "skip", None,
                           "ACLED_API_KEY / ACLED_EMAIL empty")
    r = _get("https://api.acleddata.com/acled/read",
             params={"key": key, "email": email, "country": "Iran", "limit": 3, "format": "json"})
    r.raise_for_status()
    body = r.json()
    n = len(body.get("data", []))
    return ProbeResult("ACLED (conflict events)", "critical", "ok", None, f"{n} events", n)


def probe_reliefweb() -> ProbeResult:
    base = os.getenv("RELIEFWEB_BASE_URL", "https://api.reliefweb.int/v2").rstrip("/")
    r = _get(f"{base}/reports", params={"appname": "kavach-probe", "limit": 3, "query[value]": "oil"})
    r.raise_for_status()
    n = len(r.json().get("data", []))
    return ProbeResult("ReliefWeb (UN)", "supporting", "ok", None, f"{n} reports", n)


def probe_comtrade() -> ProbeResult:
    key = os.getenv("COMTRADE_API_KEY", "").strip()
    if not key:
        return ProbeResult("UN Comtrade", "supporting", "skip", None, "COMTRADE_API_KEY empty")
    r = _get("https://comtradeapi.un.org/data/v1/get/C/A/HS",
             params={
                 "max": 3, "fmt": "json",
                 "ps": str(datetime.now(timezone.utc).year - 1),
                 "r": "356", "p": "0", "rg": "1", "cc": "2709",
             },
             headers={"Ocp-Apim-Subscription-Key": key})
    r.raise_for_status()
    n = len(r.json().get("data", []))
    return ProbeResult("UN Comtrade", "supporting", "ok", None, f"{n} rows", n)


def probe_mediastack() -> ProbeResult:
    key = os.getenv("MEDIASTACK_API_KEY", "").strip()
    if not key:
        return ProbeResult("MediaStack (backup news)", "enrichment", "skip", None, "MEDIASTACK_API_KEY empty")
    r = _get("http://api.mediastack.com/v1/news",
             params={"access_key": key, "keywords": "oil", "limit": 3})
    r.raise_for_status()
    n = len(r.json().get("data", []))
    return ProbeResult("MediaStack (backup news)", "enrichment", "ok", None, f"{n} items", n)


# ---------------------------------------------------------------------------
# Sanctions
# ---------------------------------------------------------------------------

def probe_opensanctions() -> ProbeResult:
    r = _get("https://api.opensanctions.org/search/default",
             params={"q": "tanker", "limit": 3})
    r.raise_for_status()
    n = len(r.json().get("results", []))
    return ProbeResult("OpenSanctions", "critical", "ok", None, f"{n} entities", n)


# ---------------------------------------------------------------------------
# Prices / Macro
# ---------------------------------------------------------------------------

def probe_alpha_vantage() -> ProbeResult:
    key = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
    if not key:
        return ProbeResult("Alpha Vantage (Brent/WTI)", "critical", "skip", None, "ALPHA_VANTAGE_API_KEY empty")
    r = _get("https://www.alphavantage.co/query",
             params={"function": "BRENT", "interval": "daily", "apikey": key, "datatype": "json"})
    r.raise_for_status()
    body = r.json()
    if "Note" in body or "Information" in body:
        # Rate-limited response
        return ProbeResult("Alpha Vantage (Brent/WTI)", "critical", "fail", None,
                           f"rate-limited: {body.get('Note') or body.get('Information')}"[:80])
    n = len(body.get("data", []))
    return ProbeResult("Alpha Vantage (Brent/WTI)", "critical", "ok", None, f"{n} price points", n)


def probe_eia() -> ProbeResult:
    key = os.getenv("EIA_API_KEY", "").strip()
    if not key:
        return ProbeResult("EIA (US petroleum stocks)", "supporting", "skip", None, "EIA_API_KEY empty")
    r = _get("https://api.eia.gov/v2/petroleum/stoc/wstk/data",
             params={"api_key": key, "frequency": "weekly", "data[0]": "value", "length": 3})
    r.raise_for_status()
    n = len(r.json().get("response", {}).get("data", []))
    return ProbeResult("EIA (US petroleum stocks)", "supporting", "ok", None, f"{n} rows", n)


def probe_fred() -> ProbeResult:
    key = os.getenv("FRED_API_KEY", "").strip()
    if not key:
        return ProbeResult("FRED (macro series)", "supporting", "skip", None, "FRED_API_KEY empty")
    r = _get("https://api.stlouisfed.org/fred/series/observations",
             params={"series_id": "DCOILBRENTEU", "api_key": key, "file_type": "json", "limit": 3, "sort_order": "desc"})
    r.raise_for_status()
    n = len(r.json().get("observations", []))
    return ProbeResult("FRED (macro series)", "supporting", "ok", None, f"{n} observations", n)


def probe_world_bank_prices() -> ProbeResult:
    # No key required
    r = _get("https://api.worldbank.org/v2/en/indicator/CM.MKT.PETR.CRUD.BRENT",
             params={"format": "json", "per_page": 5})
    r.raise_for_status()
    body = r.json()
    n = len(body[1]) if isinstance(body, list) and len(body) > 1 else 0
    return ProbeResult("World Bank (Brent hist)", "supporting", "ok", None, f"{n} rows", n)


def probe_polygon() -> ProbeResult:
    key = os.getenv("POLYGON_API_KEY", "").strip()
    if not key:
        return ProbeResult("Polygon.io (real-time)", "enrichment", "skip", None, "POLYGON_API_KEY empty")
    r = _get(f"https://api.polygon.io/v3/reference/tickers",
             params={"apiKey": key, "limit": 3, "market": "fx"})
    r.raise_for_status()
    n = len(r.json().get("results", []))
    return ProbeResult("Polygon.io (real-time)", "enrichment", "ok", None, f"{n} tickers", n)


# ---------------------------------------------------------------------------
# Trade / Sourcing
# ---------------------------------------------------------------------------

# probe_comtrade defined above (near the ReliefWeb section).


# ---------------------------------------------------------------------------
# Maritime / Weather
# ---------------------------------------------------------------------------

def probe_stormglass() -> ProbeResult:
    key = os.getenv("STORMGLASS_API_KEY", "").strip()
    if not key:
        return ProbeResult("Stormglass (marine weather)", "supporting", "skip", None, "STORMGLASS_API_KEY empty")
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    r = _get("https://api.stormglass.io/v2/weather/point",
             params={"lat": 26.5, "lng": 56.2, "params": "windSpeed", "start": now.isoformat(), "end": now.isoformat()},
             headers={"Authorization": key})
    r.raise_for_status()
    body = r.json()
    n = len(body.get("hours", [])) if isinstance(body, dict) else 0
    return ProbeResult("Stormglass (marine weather)", "supporting", "ok", None, f"{n} hours", n)


def probe_openweather() -> ProbeResult:
    key = os.getenv("OPENWEATHER_API_KEY", "").strip()
    if not key:
        return ProbeResult("OpenWeather (port weather)", "supporting", "skip", None, "OPENWEATHER_API_KEY empty")
    r = _get("https://api.openweathermap.org/data/2.5/weather",
             params={"lat": 22.4, "lon": 69.8, "appid": key, "units": "metric"})
    r.raise_for_status()
    body = r.json()
    return ProbeResult("OpenWeather (port weather)", "supporting", "ok", None,
                       f"{body.get('name', '?')} · wind {body.get('wind', {}).get('speed', '?')} m/s")


def probe_ais_stream() -> ProbeResult:
    key = os.getenv("AIS_STREAM_API_KEY", "").strip()
    if not key:
        return ProbeResult("AIS Stream (tankers)", "critical", "skip", None, "AIS_STREAM_API_KEY empty")
    # AIS Stream is a WebSocket API — a HEAD probe can't validate the key.
    # Confirm the WSS endpoint is at least reachable via a plain HTTPS GET.
    r = _get("https://stream.aisstream.io/")
    return ProbeResult("AIS Stream (tankers)", "critical", "ok" if r.status_code < 500 else "fail",
                       None, f"WSS host reachable · HTTP {r.status_code} · connector NOT YET wired")


# ---------------------------------------------------------------------------
# Fully missing critical APIs — surface them so the audit is honest.
# ---------------------------------------------------------------------------

_MISSING_CRITICAL = [
    ("PPAC (Petroleum Planning & Analysis Cell, India)",
     "Authoritative India crude import/export by country + refinery throughput. "
     "No REST API — publishes monthly XLSX/PDF at https://ppac.gov.in/. "
     "Fix: build a monthly scraper into ingestion/connectors/ppac.py"),
    ("MoPNG monthly bulletin (Ministry of Petroleum, India)",
     "Refinery capacity + throughput official data. PDF-only at https://mopng.gov.in/. "
     "Fix: PDF-scrape connector, low frequency (monthly)."),
    ("Baltic Exchange / VLCC index",
     "Real-time freight rates for VLCC tankers. Paid subscription only. "
     "Fix: fallback to a manual monthly override until subscription is secured."),
    ("Bunker fuel prices (ShipAndBunker / BunkerEx)",
     "IFO380/VLSFO bunker prices at Fujairah/Singapore — direct freight cost input. "
     "Free tier available at https://shipandbunker.com/. "
     "Fix: add ingestion/connectors/bunker.py (HTML-scrape or newer API)."),
    ("OPEC MOMR (Monthly Oil Market Report)",
     "Official OPEC production numbers + spare capacity. PDF-only. "
     "Fix: monthly PDF ingest, extract key tables with pdfplumber."),
    ("India RBI (imports, current account)",
     "RBI_DBIE_BASE_URL is set but no connector wired yet. Fix: implement "
     "ingestion/connectors/rbi_dbie.py hitting the DBIE REST endpoints."),
    ("IMF DataMapper",
     "IMF_BASE_URL is set but no connector wired. Fix: add "
     "ingestion/connectors/imf.py for current-account + import-cover indicators."),
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

PROBES = [
    ("Gemini (Google AI)", probe_gemini, "critical"),
    ("GDELT Doc 2.0", probe_gdelt, "critical"),
    ("OpenSanctions", probe_opensanctions, "critical"),
    ("Alpha Vantage (Brent/WTI)", probe_alpha_vantage, "critical"),
    ("ACLED (conflict events)", probe_acled, "critical"),
    ("AIS Stream (tankers)", probe_ais_stream, "critical"),
    ("NewsAPI", probe_newsapi, "supporting"),
    ("The Guardian", probe_guardian, "supporting"),
    ("ReliefWeb (UN)", probe_reliefweb, "supporting"),
    ("UN Comtrade", probe_comtrade, "supporting"),
    ("EIA (US petroleum stocks)", probe_eia, "supporting"),
    ("FRED (macro series)", probe_fred, "supporting"),
    ("World Bank (Brent hist)", probe_world_bank_prices, "supporting"),
    ("Stormglass (marine weather)", probe_stormglass, "supporting"),
    ("OpenWeather (port weather)", probe_openweather, "supporting"),
    ("Polygon.io (real-time)", probe_polygon, "enrichment"),
    ("MediaStack (backup news)", probe_mediastack, "enrichment"),
]


def _format_table(results: list[ProbeResult]) -> str:
    tier_order = {"critical": 0, "supporting": 1, "enrichment": 2}
    status_glyph = {"ok": "OK ", "skip": "-- ", "fail": "!! "}
    lines = []
    lines.append("=" * 100)
    lines.append(f"{'STATUS':<4}  {'TIER':<11}  {'API':<38}  {'LAT':<6}  DETAIL")
    lines.append("-" * 100)
    for r in sorted(results, key=lambda x: (tier_order.get(x.tier, 9), x.status != "ok", x.name)):
        lat = f"{r.latency_ms}ms" if r.latency_ms is not None else "-"
        lines.append(f"{status_glyph.get(r.status, '?  ')}  {r.tier:<11}  {r.name:<38}  {lat:<6}  {r.detail}")
    lines.append("=" * 100)
    ok = sum(1 for r in results if r.status == "ok")
    skip = sum(1 for r in results if r.status == "skip")
    fail = sum(1 for r in results if r.status == "fail")
    crit_fail = sum(1 for r in results if r.status == "fail" and r.tier == "critical")
    lines.append(f"Summary: {ok} ok · {skip} skipped · {fail} failed  (critical failures: {crit_fail})")
    return "\n".join(lines)


def _format_missing_section() -> str:
    lines = ["", "MISSING BUT CRITICAL — no API key configured or no connector wired:", "-" * 100]
    for name, desc in _MISSING_CRITICAL:
        lines.append(f"  • {name}")
        for word in desc.split(". "):
            lines.append(f"      {word.strip()}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    results = [_time(fn, name, tier) for name, fn, tier in PROBES]

    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2, default=str))
        return 0

    print(_format_table(results))
    print(_format_missing_section())

    # Exit non-zero if any *critical* API failed (skips are OK)
    return 1 if any(r.status == "fail" and r.tier == "critical" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
