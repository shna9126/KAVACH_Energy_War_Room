import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.hypothesis_agent import HypothesisAgentConfig, generate_hypothesis
from ingestion.storage import append_hypotheses, fetch_unprocessed_structured_events


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run hypothesis agent over structured events.")
    parser.add_argument("--mode", choices=["auto", "gemini", "deterministic"], default=os.getenv("HYPOTHESIS_MODE", "auto"))
    parser.add_argument("--limit", type=int, default=200)
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL missing. Set it in .env or environment.")

    config = HypothesisAgentConfig(
        mode=args.mode,
        api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        model=os.getenv("HYPOTHESIS_MODEL", "gemini-2.5-pro").strip() or "gemini-2.5-pro",
        timeout_seconds=int(os.getenv("HYPOTHESIS_TIMEOUT_SECONDS", "45")),
    )

    events = fetch_unprocessed_structured_events(database_url, limit=args.limit)
    hypotheses = [generate_hypothesis(e, config) for e in events]
    inserted = append_hypotheses(database_url, hypotheses)
    print(f"Hypothesis run complete: input={len(events)} inserted={inserted} mode={args.mode}")


if __name__ == "__main__":
    main()