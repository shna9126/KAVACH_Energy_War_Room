import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.policy_agent import PolicyAgentConfig, generate_policy_plan
from ingestion.storage import (
    append_recommendations,
    fetch_recommendation_map_by_simulation,
    fetch_unprocessed_simulations,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Policy Agent from simulations and procurement outputs.")
    parser.add_argument("--limit", type=int, default=500)
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL missing. Set it in .env or environment.")

    cfg = PolicyAgentConfig(
        max_spr_draw_mbd=float(os.getenv("POLICY_MAX_SPR_DRAW_MBD", "4.5")),
        strategic_reserve_days=int(os.getenv("POLICY_STRATEGIC_RESERVE_DAYS", "30")),
    )

    sims = fetch_unprocessed_simulations(database_url, recommendation_type="policy_plan", limit=args.limit)
    sim_ids = [s.id for s in sims]
    procurement_map = fetch_recommendation_map_by_simulation(database_url, "procurement_plan", sim_ids)

    recs = [generate_policy_plan(sim, procurement_map.get(sim.id), cfg) for sim in sims]
    inserted = append_recommendations(database_url, recs)
    print(f"Policy run complete: input={len(sims)} inserted={inserted}")


if __name__ == "__main__":
    main()