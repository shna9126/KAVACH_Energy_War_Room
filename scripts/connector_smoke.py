import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ingestion.connectors.prices import parse_payload as parse_worldbank_prices


def main() -> None:
    sample_path = Path("data/samples/world_bank_prices.json")
    if not sample_path.exists():
        raise SystemExit("Missing sample: data/samples/world_bank_prices.json")

    payload = json.loads(sample_path.read_text(encoding="utf-8"))
    signals = parse_worldbank_prices(payload)
    print(f"Parsed RawSignals: {len(signals)}")
    if signals:
        print(signals[0].model_dump_json(indent=2))


if __name__ == "__main__":
    main()