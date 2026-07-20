from __future__ import annotations

from dataclasses import dataclass

from ingestion.storage import RecommendationInput, SimulationRecord


@dataclass
class EconomicAgentConfig:
    annual_import_bill_usd_bn: float = 220.0
    nominal_gdp_usd_bn: float = 4000.0
    pass_through_to_cpi: float = 0.22
    mission_objective: str = "balanced_resilience"


def _objective_multipliers(mission_objective: str) -> tuple[float, float]:
    key = str(mission_objective or "").strip().lower()
    if key in {"maximize_supply_resilience", "resilience", "security"}:
        return 0.92, 0.90
    if key in {"minimize_import_cost", "cost", "optimize_cost"}:
        return 1.12, 1.08
    if key in {"maintain_import_coverage", "coverage"}:
        return 1.03, 1.00
    return 1.00, 1.00


def _pick_number(obj: dict, keys: list[str], default: float) -> float:
    for key in keys:
        val = obj.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return default


def generate_economic_impact(sim: SimulationRecord, config: EconomicAgentConfig) -> RecommendationInput:
    percentiles = sim.percentiles or {}
    metadata = sim.metadata or {}

    disruption_prob = _pick_number(percentiles, ["disruption_prob", "p50_disruption_prob"], _pick_number(metadata, ["disruption_prob"], 0.35))
    disruption_prob = max(0.0, min(1.0, disruption_prob))

    price_shock_pct = _pick_number(percentiles, ["price_shock_pct", "p50_price_shock_pct"], _pick_number(metadata, ["price_shock_pct"], 0.08))
    price_shock_pct = max(0.0, min(1.0, price_shock_pct))

    duration_days = _pick_number(percentiles, ["duration_days", "p50_duration_days"], _pick_number(metadata, ["duration_days"], 10.0))
    duration_days = max(1.0, duration_days)

    exposure_mult, pass_mult = _objective_multipliers(config.mission_objective)
    exposure_factor = (duration_days / 365.0) * (0.5 + 0.5 * disruption_prob) * exposure_mult
    import_bill_delta_usd_bn = config.annual_import_bill_usd_bn * price_shock_pct * exposure_factor

    # Approximate CAD impact as import bill delta over GDP.
    cad_delta_pct_of_gdp = (import_bill_delta_usd_bn / config.nominal_gdp_usd_bn) * 100.0

    # Simple pass-through model from energy price shock to CPI delta.
    cpi_delta_pct = price_shock_pct * config.pass_through_to_cpi * pass_mult * 100.0

    recommendation_payload = {
        "economic_impact": {
            "import_bill_delta_usd_bn": round(import_bill_delta_usd_bn, 3),
            "cad_delta_pct_of_gdp": round(cad_delta_pct_of_gdp, 4),
            "cpi_delta_pct": round(cpi_delta_pct, 4),
            "horizon": sim.horizon,
        },
        "drivers": {
            "disruption_prob": round(disruption_prob, 4),
            "price_shock_pct": round(price_shock_pct, 4),
            "duration_days": round(duration_days, 2),
        },
        "assumptions": {
            "annual_import_bill_usd_bn": config.annual_import_bill_usd_bn,
            "nominal_gdp_usd_bn": config.nominal_gdp_usd_bn,
            "pass_through_to_cpi": config.pass_through_to_cpi,
            "mission_objective": config.mission_objective,
            "objective_exposure_multiplier": round(exposure_mult, 4),
            "objective_pass_through_multiplier": round(pass_mult, 4),
        },
    }

    # Higher score means stronger macro impact.
    impact_score = min(1.0, (cpi_delta_pct / 2.0) + (cad_delta_pct_of_gdp / 0.5))

    return RecommendationInput(
        simulation_id=sim.id,
        recommendation_type="economic_impact",
        recommendation_payload=recommendation_payload,
        score=round(impact_score, 4),
    )
