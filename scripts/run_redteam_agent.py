import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.redteam_agent import RedTeamAgentConfig, generate_redteam_review
from ingestion.storage import append_hypothesis_reviews, fetch_unreviewed_hypotheses


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Red-team Agent over hypotheses.")
    parser.add_argument("--mode", choices=["auto", "gemini", "deterministic"], default=os.getenv("REDTEAM_MODE", "auto"))
    parser.add_argument("--limit", type=int, default=200)
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL missing. Set it in .env or environment.")

    config = RedTeamAgentConfig(
        mode=args.mode,
        api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        model=os.getenv("REDTEAM_MODEL", "gemini-2.5-pro").strip() or "gemini-2.5-pro",
        timeout_seconds=int(os.getenv("REDTEAM_TIMEOUT_SECONDS", "45")),
    )

    hypotheses = fetch_unreviewed_hypotheses(database_url, limit=args.limit)
    reviews = [generate_redteam_review(h, config) for h in hypotheses]
    inserted = append_hypothesis_reviews(database_url, reviews)
    print(f"Red-team run complete: input={len(hypotheses)} inserted={inserted} mode={args.mode}")


if __name__ == "__main__":
    main()