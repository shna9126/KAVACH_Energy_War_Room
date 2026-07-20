import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


SAMPLES_DIR = Path("data/samples")
TIMEOUT_SECONDS = 30
VERIFY_TLS = os.getenv("PROBE_VERIFY_TLS", "true").strip().lower() not in {"0", "false", "no"}
CA_BUNDLE = os.getenv("PROBE_CA_BUNDLE", "").strip() or None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_sample(name: str, payload: Any) -> None:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SAMPLES_DIR / f"{name}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"[ok] wrote {out_path}")


def _request_json(url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
    verify: bool | str = CA_BUNDLE if CA_BUNDLE else VERIFY_TLS
    response = requests.get(url, params=params, headers=headers, timeout=TIMEOUT_SECONDS, verify=verify)
    response.raise_for_status()
    return response.json()


def probe_gdelt_doc() -> dict[str, Any]:
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": "(Hormuz OR oil OR tanker) lang:english",
        "mode": "artlist",
        "format": "json",
        "maxrecords": 10,
    }
    data = _request_json(url, params=params)
    _write_sample("gdelt_doc", data)
    return {"source": "gdelt_doc", "status": "ok", "records_hint": len(data.get("articles", []))}


def probe_newsapi() -> dict[str, Any]:
    api_key = os.getenv("NEWSAPI_KEY", "").strip()
    if not api_key:
        return {"source": "newsapi", "status": "skipped", "reason": "NEWSAPI_KEY missing"}

    url = "https://newsapi.org/v2/everything"
    params = {
        "q": "Hormuz oil tanker",
        "language": "en",
        "pageSize": 10,
        "sortBy": "publishedAt",
    }
    headers = {"X-Api-Key": api_key}
    data = _request_json(url, params=params, headers=headers)
    _write_sample("newsapi", data)
    return {"source": "newsapi", "status": "ok", "records_hint": len(data.get("articles", []))}


def probe_opensanctions() -> dict[str, Any]:
    url = "https://api.opensanctions.org/search/default"
    params = {
        "q": "tanker",
        "limit": 10,
    }
    data = _request_json(url, params=params)
    _write_sample("opensanctions", data)
    return {"source": "opensanctions", "status": "ok", "records_hint": len(data.get("results", []))}


def probe_comtrade() -> dict[str, Any]:
    api_key = os.getenv("COMTRADE_API_KEY", "").strip()
    if not api_key:
        return {"source": "comtrade", "status": "skipped", "reason": "COMTRADE_API_KEY missing"}

    url = "https://comtradeapi.worldbank.org/data/v1/get/C/A/HS"
    params = {
        "max": 50,
        "fmt": "json",
        "ps": str(datetime.now(timezone.utc).year - 1),
        "r": "356",
        "p": "0",
        "rg": "1",
        "cc": "2709",
    }
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    data = _request_json(url, params=params, headers=headers)
    _write_sample("comtrade", data)
    dataset = data.get("data") if isinstance(data, dict) else None
    count = len(dataset) if isinstance(dataset, list) else 0
    return {"source": "comtrade", "status": "ok", "records_hint": count}


def probe_world_bank_prices() -> dict[str, Any]:
    url = "https://api.worldbank.org/v2/en/indicator/CM.MKT.PETR.CRUD.WTI?format=json&per_page=60"
    data = _request_json(url)
    _write_sample("world_bank_prices", data)
    count = len(data[1]) if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list) else 0
    return {"source": "world_bank_prices", "status": "ok", "records_hint": count}


def probe_gemini() -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return {"source": "gemini", "status": "skipped", "reason": "GEMINI_API_KEY missing"}

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    params = {"key": api_key}
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": "Return compact JSON with keys model and status only."
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 50,
        },
    }
    verify: bool | str = CA_BUNDLE if CA_BUNDLE else VERIFY_TLS
    response = requests.post(url, params=params, json=payload, timeout=TIMEOUT_SECONDS, verify=verify)
    response.raise_for_status()
    data = response.json()
    _write_sample("gemini", data)
    return {"source": "gemini", "status": "ok", "records_hint": 1}


def probe_ais_mock_placeholder() -> dict[str, Any]:
    api_key = os.getenv("AIS_API_KEY", "").strip()
    payload = {
        "source": "ais",
        "status": "not_implemented",
        "reason": "Provider-specific free-tier APIs vary; integrate AISHub or MarineTraffic endpoint when key/provider is finalized.",
        "has_key": bool(api_key),
        "timestamp_utc": _utc_now_iso(),
    }
    _write_sample("ais_placeholder", payload)
    return {"source": "ais", "status": "placeholder", "records_hint": 0}


def probe_ppac_placeholder() -> dict[str, Any]:
    payload = {
        "source": "ppac",
        "status": "manual_load_required",
        "reason": "PPAC has no stable public API. Plan a one-time CSV/XLSX download and loader.",
        "timestamp_utc": _utc_now_iso(),
    }
    _write_sample("ppac_placeholder", payload)
    return {"source": "ppac", "status": "placeholder", "records_hint": 0}


PROBES = {
    "gdelt_doc": probe_gdelt_doc,
    "newsapi": probe_newsapi,
    "opensanctions": probe_opensanctions,
    "comtrade": probe_comtrade,
    "world_bank_prices": probe_world_bank_prices,
    "gemini": probe_gemini,
    "ais": probe_ais_mock_placeholder,
    "ppac": probe_ppac_placeholder,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe external sources once and dump raw JSON samples.")
    parser.add_argument(
        "--sources",
        nargs="*",
        default=list(PROBES.keys()),
        help=f"Subset of sources to probe. Options: {', '.join(PROBES.keys())}",
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()

    unknown = [s for s in args.sources if s not in PROBES]
    if unknown:
        raise SystemExit(f"Unknown source(s): {', '.join(unknown)}")

    summary: list[dict[str, Any]] = []
    for source in args.sources:
        fn = PROBES[source]
        try:
            result = fn()
            summary.append(result)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            summary.append({"source": source, "status": "error", "reason": f"http {status}"})
        except requests.RequestException as exc:
            summary.append({"source": source, "status": "error", "reason": str(exc)})
        except Exception as exc:
            summary.append({"source": source, "status": "error", "reason": str(exc)})

    _write_sample("probe_summary", {
        "ran_at_utc": _utc_now_iso(),
        "summary": summary,
    })
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()