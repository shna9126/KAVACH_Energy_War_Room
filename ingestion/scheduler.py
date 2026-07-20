from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ingestion.connectors import (
    acled,
    ais_stream,
    alpha_vantage,
    eia,
    fred,
    gdelt,
    guardian,
    newsapi,
    reliefweb,
    sanctions,
)
from ingestion.schemas.raw_signal import RawSignal
from ingestion.storage import append_raw_signals, ensure_tables


ConnectorFn = Callable[[], list[RawSignal]]
SAMPLES_DIR = PROJECT_ROOT / "data" / "samples"


def _run_connector(name: str, fn: ConnectorFn, database_url: str) -> None:
    started = datetime.now(timezone.utc)
    try:
        signals = fn()
        written = append_raw_signals(database_url, signals)
        duplicates = len(signals) - written
        print(f"[{started.isoformat()}] {name}: fetched={len(signals)} inserted={written} duplicates={duplicates}")
    except Exception as exc:
        print(f"[{started.isoformat()}] {name}: error={exc}")


def _newsapi_runner() -> list[RawSignal]:
    api_key = os.getenv("NEWSAPI_KEY", "").strip()
    if not api_key:
        return []
    return newsapi.fetch(api_key=api_key, page_size=20)


def _gdelt_runner() -> list[RawSignal]:
    return gdelt.fetch(max_records=20)


def _sanctions_runner() -> list[RawSignal]:
    return sanctions.fetch(limit=20)


def _alpha_vantage_runner() -> list[RawSignal]:
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
    if not api_key:
        return []
    signals = alpha_vantage.fetch(api_key=api_key, grade="brent")
    signals.extend(alpha_vantage.fetch(api_key=api_key, grade="wti"))
    return signals


def _guardian_runner() -> list[RawSignal]:
    api_key = os.getenv("GUARDIAN_API_KEY", "").strip()
    if not api_key:
        return []
    return guardian.fetch(api_key=api_key, page_size=25)


def _acled_runner() -> list[RawSignal]:
    api_key = os.getenv("ACLED_API_KEY", "").strip()
    email = os.getenv("ACLED_EMAIL", "").strip()
    if not api_key or not email:
        return []
    return acled.fetch(api_key=api_key, email=email, limit=100)


def _reliefweb_runner() -> list[RawSignal]:
    return reliefweb.fetch(limit=40)


def _eia_runner() -> list[RawSignal]:
    api_key = os.getenv("EIA_API_KEY", "").strip()
    if not api_key:
        return []
    return eia.fetch(api_key=api_key, length=52)


def _fred_runner() -> list[RawSignal]:
    api_key = os.getenv("FRED_API_KEY", "").strip()
    if not api_key:
        return []
    signals = fred.fetch(api_key=api_key, series_id="DCOILBRENTEU", limit=120)
    signals.extend(fred.fetch(api_key=api_key, series_id="DEXINUS", limit=120))
    return signals


def _ais_stream_runner() -> list[RawSignal]:
    api_key = os.getenv("AIS_STREAM_API_KEY", "").strip()
    if not api_key:
        return []
    return ais_stream.fetch(api_key=api_key, max_messages=20, timeout_seconds=10)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sample_gdelt_runner() -> list[RawSignal]:
    return gdelt.parse_payload(_read_json(SAMPLES_DIR / "gdelt_doc_sample.json"))


def _sample_newsapi_runner() -> list[RawSignal]:
    return newsapi.parse_payload(_read_json(SAMPLES_DIR / "newsapi_sample.json"))


def _sample_sanctions_runner() -> list[RawSignal]:
    return sanctions.parse_payload(_read_json(SAMPLES_DIR / "opensanctions_sample.json"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Layer 1 ingestion scheduler.")
    parser.add_argument(
        "--mode",
        choices=["live", "sample"],
        default="live",
        help="Choose live API pulls or local sample payloads.",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run each configured connector once and exit.",
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL missing. Set it in .env")

    ensure_tables(database_url)

    if args.mode == "sample":
        jobs: list[tuple[str, ConnectorFn]] = [
            ("gdelt_sample", _sample_gdelt_runner),
            ("newsapi_sample", _sample_newsapi_runner),
            ("sanctions_sample", _sample_sanctions_runner),
        ]
    else:
        jobs = [
            ("gdelt", _gdelt_runner),
            ("newsapi", _newsapi_runner),
            ("sanctions", _sanctions_runner),
            ("ais_stream", _ais_stream_runner),
            ("alpha_vantage", _alpha_vantage_runner),
            ("guardian", _guardian_runner),
            ("acled", _acled_runner),
            ("reliefweb", _reliefweb_runner),
            ("eia", _eia_runner),
            ("fred", _fred_runner),
        ]

    if args.run_once:
        for name, fn in jobs:
            _run_connector(name, fn, database_url)
        return

    scheduler = BlockingScheduler(timezone="UTC")
    if args.mode == "sample":
        scheduler.add_job(lambda: _run_connector("gdelt_sample", _sample_gdelt_runner, database_url), "interval", minutes=15)
        scheduler.add_job(lambda: _run_connector("newsapi_sample", _sample_newsapi_runner, database_url), "interval", minutes=15)
        scheduler.add_job(lambda: _run_connector("sanctions_sample", _sample_sanctions_runner, database_url), "interval", days=1)
    else:
        scheduler.add_job(lambda: _run_connector("gdelt", _gdelt_runner, database_url), "interval", minutes=15)
        scheduler.add_job(lambda: _run_connector("newsapi", _newsapi_runner, database_url), "interval", minutes=15)
        scheduler.add_job(lambda: _run_connector("sanctions", _sanctions_runner, database_url), "interval", days=1)
        scheduler.add_job(lambda: _run_connector("ais_stream", _ais_stream_runner, database_url), "interval", minutes=20)
        # PRD v2 live-API wiring
        scheduler.add_job(lambda: _run_connector("alpha_vantage", _alpha_vantage_runner, database_url), "interval", hours=6)
        scheduler.add_job(lambda: _run_connector("guardian", _guardian_runner, database_url), "interval", minutes=30)
        scheduler.add_job(lambda: _run_connector("acled", _acled_runner, database_url), "interval", hours=1)
        scheduler.add_job(lambda: _run_connector("reliefweb", _reliefweb_runner, database_url), "interval", hours=1)
        scheduler.add_job(lambda: _run_connector("eia", _eia_runner, database_url), "interval", hours=12)
        scheduler.add_job(lambda: _run_connector("fred", _fred_runner, database_url), "interval", hours=12)

    print("Scheduler started (UTC). Press Ctrl+C to stop.")
    scheduler.start()


if __name__ == "__main__":
    main()