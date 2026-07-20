import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.scenario_agent import ScenarioAgentConfig, generate_simulations
from ingestion.storage import append_simulations, fetch_unprocessed_hypotheses


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Scenario Agent (Monte Carlo) from hypotheses to simulations.")
    parser.add_argument("--limit", type=int, default=100)
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL missing. Set it in .env or environment.")

    cfg = ScenarioAgentConfig(
        num_simulations=int(os.getenv("SCENARIO_NUM_SIMULATIONS", "10000")),
        base_seed=int(os.getenv("SCENARIO_RANDOM_SEED", "42")),
    )

    hypotheses = fetch_unprocessed_hypotheses(database_url, limit=args.limit)
    simulations = []
    for hypothesis in hypotheses:
        simulations.extend(generate_simulations(hypothesis, cfg))

    inserted = append_simulations(database_url, simulations)
    print(
        f"Scenario agent run complete: hypotheses={len(hypotheses)} "
        f"simulations_generated={len(simulations)} inserted={inserted}"
    )


if __name__ == "__main__":
    main()