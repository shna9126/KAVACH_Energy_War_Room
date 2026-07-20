import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ingestion.connectors.gdelt import parse_payload as parse_gdelt
from ingestion.connectors.newsapi import parse_payload as parse_newsapi
from ingestion.connectors.sanctions import parse_payload as parse_sanctions
from ingestion.storage import append_raw_signals, ensure_tables


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    load_dotenv()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL missing. Set it in .env or environment.")

    ensure_tables(database_url)

    gdelt_payload = _read_json(PROJECT_ROOT / "data" / "samples" / "gdelt_doc_sample.json")
    news_payload = _read_json(PROJECT_ROOT / "data" / "samples" / "newsapi_sample.json")
    sanctions_payload = _read_json(PROJECT_ROOT / "data" / "samples" / "opensanctions_sample.json")

    gdelt_signals = parse_gdelt(gdelt_payload)
    news_signals = parse_newsapi(news_payload)
    sanctions_signals = parse_sanctions(sanctions_payload)

    inserted = 0
    inserted += append_raw_signals(database_url, gdelt_signals)
    inserted += append_raw_signals(database_url, news_signals)
    inserted += append_raw_signals(database_url, sanctions_signals)

    print(
        f"Inserted rows: {inserted} "
        f"(gdelt={len(gdelt_signals)}, newsapi={len(news_signals)}, sanctions={len(sanctions_signals)})"
    )


if __name__ == "__main__":
    main()