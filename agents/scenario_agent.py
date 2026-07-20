from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ingestion.storage import HypothesisRecord, SimulationInput


HORIZON_DAYS = {
    "24h": 1,
    "72h": 3,
    "1wk": 7,
    "1mo": 30,
}


@dataclass
class ScenarioAgentConfig:
    num_simulations: int = 10000
    base_seed: int = 42
    mission_objective: str = "balanced_resilience"


def _objective_multipliers(mission_objective: str) -> tuple[float, float, float]:
    key = str(mission_objective or "").strip().lower()
    if key in {"maximize_supply_resilience", "resilience", "security"}:
        return 1.08, 1.03, 1.08
    if key in {"minimize_import_cost", "cost", "optimize_cost"}:
        return 0.95, 1.10, 0.95
    if key in {"maintain_import_coverage", "coverage"}:
        return 1.02, 1.00, 1.05
    return 1.00, 1.00, 1.00


def _clip01(arr: np.ndarray) -> np.ndarray:
    return np.clip(arr, 0.0, 1.0)


def _simulate_for_horizon(hypothesis: HypothesisRecord, horizon: str, days: int, cfg: ScenarioAgentConfig) -> SimulationInput:
    conf = float(hypothesis.confidence) if isinstance(hypothesis.confidence, (int, float)) else 0.6
    conf = float(np.clip(conf, 0.05, 0.95))

    # Deterministic seed per hypothesis/horizon for reproducibility.
    seed = cfg.base_seed + (hypothesis.id * 37) + (days * 11)
    rng = np.random.default_rng(seed)

    horizon_scale = float(np.sqrt(days / 7.0))
    risk_mult, price_mult, duration_mult = _objective_multipliers(cfg.mission_objective)

    base_disruption_prob = np.clip((0.15 + 0.65 * conf * horizon_scale) * risk_mult, 0.05, 0.95)
    base_price_shock = np.clip((0.03 + 0.12 * conf * horizon_scale) * price_mult, 0.01, 0.45)
    base_duration = max(1.0, days * (0.5 + conf) * duration_mult)

    disruption_flags = rng.binomial(1, base_disruption_prob, size=cfg.num_simulations)
    price_shocks = _clip01(rng.normal(base_price_shock, 0.03 + 0.02 * horizon_scale, size=cfg.num_simulations))
    duration_days = np.clip(rng.gamma(shape=2.5, scale=base_duration / 2.5, size=cfg.num_simulations), 1.0, 120.0)

    # Couple severe disruption scenarios with larger price impact.
    price_shocks = _clip01(price_shocks + (disruption_flags * 0.015))

    p10_price, p50_price, p90_price = np.percentile(price_shocks, [10, 50, 90])
    p10_dur, p50_dur, p90_dur = np.percentile(duration_days, [10, 50, 90])
    disruption_prob = float(np.mean(disruption_flags))

    percentiles = {
        "disruption_prob": round(disruption_prob, 4),
        "price_shock_pct": round(float(p50_price), 4),
        "duration_days": round(float(p50_dur), 2),
        "p10_price_shock_pct": round(float(p10_price), 4),
        "p90_price_shock_pct": round(float(p90_price), 4),
        "p10_duration_days": round(float(p10_dur), 2),
        "p90_duration_days": round(float(p90_dur), 2),
    }

    distribution = {
        "price_shock_pct_samples": [round(float(x), 4) for x in price_shocks[:200]],
        "duration_days_samples": [round(float(x), 2) for x in duration_days[:200]],
    }

    metadata = {
        "source_hypothesis_model": hypothesis.model_name,
        "hypothesis_confidence": round(conf, 4),
        "num_simulations": cfg.num_simulations,
        "horizon_days": days,
        "mission_objective": cfg.mission_objective,
    }

    return SimulationInput(
        hypothesis_id=hypothesis.id,
        horizon=horizon,
        percentiles=percentiles,
        distribution=distribution,
        metadata=metadata,
    )


def generate_simulations(hypothesis: HypothesisRecord, cfg: ScenarioAgentConfig) -> list[SimulationInput]:
    return [
        _simulate_for_horizon(hypothesis, horizon, days, cfg)
        for horizon, days in HORIZON_DAYS.items()
    ]
