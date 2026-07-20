import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ingestion.storage import fetch_unprocessed_structured_events
from orchestration.graph import run_pipeline_for_structured_event


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Layer 5 orchestration graph for one structured event.")
    parser.add_argument("--structured-event-id", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL missing. Set it in .env or environment.")

    structured_event_id = args.structured_event_id
    if structured_event_id is None:
        events = fetch_unprocessed_structured_events(database_url, limit=1)
        if not events:
            raise SystemExit("No unprocessed structured event found. Pass --structured-event-id explicitly.")
        structured_event_id = events[0].id

    state = run_pipeline_for_structured_event(database_url, structured_event_id)
    print(json.dumps(state.model_dump(), indent=2))


if __name__ == "__main__":
    main()
