import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.procurement_agent import ProcurementAgentConfig, generate_procurement_plan
from digital_twin import build_digital_twin
from ingestion.storage import (
    append_recommendations,
    fetch_recommendation_map_by_simulation,
    fetch_unprocessed_simulations,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Procurement Agent from simulations.")
    parser.add_argument("--limit", type=int, default=500)
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL missing. Set it in .env or environment.")

    cfg = ProcurementAgentConfig(demand_kbd=float(os.getenv("PROCUREMENT_DEMAND_KBD", "1800")))

    sims = fetch_unprocessed_simulations(database_url, recommendation_type="procurement_plan", limit=args.limit)
    sim_ids = [s.id for s in sims]
    econ_map = fetch_recommendation_map_by_simulation(database_url, "economic_impact", sim_ids)
    twin = build_digital_twin(database_url)

    recs = [generate_procurement_plan(sim, econ_map.get(sim.id), cfg, twin) for sim in sims]
    inserted = append_recommendations(database_url, recs)
    print(f"Procurement run complete: input={len(sims)} inserted={inserted}")


if __name__ == "__main__":
    main()
