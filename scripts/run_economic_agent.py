import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.economic_agent import EconomicAgentConfig, generate_economic_impact
from ingestion.storage import append_recommendations, fetch_unprocessed_simulations


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Economic Agent from simulations to recommendations.")
    parser.add_argument("--limit", type=int, default=200)
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL missing. Set it in .env or environment.")

    config = EconomicAgentConfig(
        annual_import_bill_usd_bn=float(os.getenv("ECON_ANNUAL_IMPORT_BILL_USD_BN", "220")),
        nominal_gdp_usd_bn=float(os.getenv("ECON_NOMINAL_GDP_USD_BN", "4000")),
        pass_through_to_cpi=float(os.getenv("ECON_PASS_THROUGH_TO_CPI", "0.22")),
    )

    sims = fetch_unprocessed_simulations(database_url, recommendation_type="economic_impact", limit=args.limit)
    recs = [generate_economic_impact(sim, config) for sim in sims]
    inserted = append_recommendations(database_url, recs)
    print(f"Economic agent run complete: input={len(sims)} inserted={inserted}")


if __name__ == "__main__":
    main()