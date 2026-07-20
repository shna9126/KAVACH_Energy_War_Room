"""Procurement Agent (PRD v2 Upgrade 6 — Procurement Explainability).

Ranks crude-oil supplier candidates for Indian refiners. When the Digital
Twin is supplied, the candidate universe, prices, transit times, insurance
premiums, port/chokepoint status and blend compatibility are pulled from
`DigitalTwinState`, and each candidate is scored against a *constraint
checklist* — so every allocation (and every rejection) carries an
explainable rationale.

Backwards compat: when `twin` is not passed, the original hard-coded
five-supplier universe is used so pre-existing tests and scripts keep
working. That path also emits a `ranking` block with a lightweight
scorecard.

Definition of Done (per PRD v2):
    Every procurement recommendation is accompanied by a ranked explanation
    that lists Supplier Score, Risk, Transit Time, Cost, Insurance, Port
    Status, Compatibility, Confidence, and Rejected Constraints.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from digital_twin.graph_state import DigitalTwinState
from ingestion.storage import RecommendationInput, RecommendationRecord, SimulationRecord


# ---------------------------------------------------------------------------
# Config + candidate model
# ---------------------------------------------------------------------------


@dataclass
class ProcurementAgentConfig:
    demand_kbd: float = 1800.0
    consumer_country_iso3: str = "IND"
    default_transit_days: float = 10.0
    default_insurance_multiplier: float = 1.0
    mission_objective: str = "balanced_resilience"


@dataclass
class SupplierCandidate:
    supplier_country_iso3: str
    supplier_country_name: str | None
    grade_id: str | None
    grade_name: str | None
    spare_capacity_kbd: float
    cost_index: float                 # normalized: 1.00 ≈ Brent baseline
    reference_price_usd_bbl: float | None
    transit_days: float
    insurance_multiplier: float
    port_status: str                  # "open" | "restricted" | "closed" | "unknown"
    origin_port_id: str | None
    destination_port_id: str | None
    route_id: str | None
    chokepoint_ids: list[str] = field(default_factory=list)
    chokepoint_risk_max: float = 0.0
    blend_compatibility: float = 0.9  # 0..1
    compatible_refineries: list[str] = field(default_factory=list)
    sanctions_risk: float = 0.0       # 0..1
    sanctions_reasons: list[str] = field(default_factory=list)
    geopolitical_risk: float = 0.20   # 0..1
    source: str = "twin"


# ---------------------------------------------------------------------------
# Twin-backed candidate building
# ---------------------------------------------------------------------------


def _sanctioned_countries(twin: DigitalTwinState) -> dict[str, list[str]]:
    """Return iso3 → list of sanction reason strings."""
    reasons: dict[str, list[str]] = {}
    country_names = {c.name.lower(): c.iso3 for c in twin.countries}
    country_iso = {c.iso3.upper() for c in twin.countries}
    for entry in twin.sanctions:
        low = entry.entity.lower()
        matched_iso: set[str] = set()
        for name, iso in country_names.items():
            if name and name in low:
                matched_iso.add(iso)
        for iso in country_iso:
            if iso.lower() in low.split():
                matched_iso.add(iso)
        if not matched_iso:
            continue
        label = entry.entity
        if entry.imposed_by:
            label += f" (imposed by {', '.join(entry.imposed_by[:3])})"
        for iso in matched_iso:
            reasons.setdefault(iso, []).append(label)
    return reasons


def _best_route_for(
    twin: DigitalTwinState,
    source_iso3: str,
    dest_iso3: str,
) -> tuple[Any, Any, Any] | None:
    """Return (route, origin_port, dest_port) with the shortest transit_days.

    None if no route exists between the two countries in the twin.
    """
    source_ports = {p.id: p for p in twin.ports if p.country_iso3.upper() == source_iso3.upper()}
    dest_ports = {p.id: p for p in twin.ports if p.country_iso3.upper() == dest_iso3.upper()}
    if not source_ports or not dest_ports:
        return None

    best = None
    best_days = math.inf
    for route in twin.routes:
        if route.origin_port_id in source_ports and route.destination_port_id in dest_ports:
            if route.transit_days < best_days:
                best_days = route.transit_days
                best = (route, source_ports[route.origin_port_id], dest_ports[route.destination_port_id])
    return best


def _grade_price_map(twin: DigitalTwinState) -> dict[str, float]:
    out: dict[str, float] = {}
    for pp in twin.prices:
        if pp.grade_id not in out:
            out[pp.grade_id] = pp.price_usd_per_bbl
    return out


def _refineries_accepting_grade(twin: DigitalTwinState, grade_id: str) -> list[str]:
    return [r.id for r in twin.refineries if grade_id in (r.compatible_grade_ids or [])]


def _blend_compatibility(twin: DigitalTwinState, grade_id: str, consumer_iso3: str) -> float:
    """Fraction of consumer-country refineries that can process this grade."""
    consumer_refs = [r for r in twin.refineries if r.country_iso3.upper() == consumer_iso3.upper()]
    if not consumer_refs:
        return 0.5
    matches = sum(1 for r in consumer_refs if grade_id in (r.compatible_grade_ids or []))
    return matches / len(consumer_refs)


def _chokepoint_geo_risk(twin: DigitalTwinState, chokepoint_ids: list[str]) -> tuple[float, float]:
    """Return (max_risk, avg_risk) across the route's chokepoints."""
    if not chokepoint_ids:
        return (0.10, 0.10)
    risks = []
    for cp_id in chokepoint_ids:
        cp = twin.chokepoint_by_id(cp_id)
        if cp is None:
            continue
        # Closed chokepoint is effectively risk = 1.0
        r = 1.0 if cp.status == "closed" else float(cp.risk_score)
        risks.append(r)
    if not risks:
        return (0.10, 0.10)
    return (max(risks), sum(risks) / len(risks))


def _build_twin_universe(twin: DigitalTwinState, cfg: ProcurementAgentConfig) -> list[SupplierCandidate]:
    price_map = _grade_price_map(twin)
    brent = price_map.get("grade_brent")
    sanctions = _sanctioned_countries(twin)
    country_names = {c.iso3.upper(): c.name for c in twin.countries}

    candidates: list[SupplierCandidate] = []
    for cap in twin.supplier_capacities:
        if cap.spare_capacity_kbd <= 0:
            continue
        grade_id = cap.grade_id
        grade = next((g for g in twin.crude_grades if g.id == grade_id), None) if grade_id else None
        price = price_map.get(grade_id or "") if grade_id else brent
        cost_index = round(price / brent, 4) if (price and brent) else 1.0

        route_bundle = _best_route_for(twin, cap.country_iso3, cfg.consumer_country_iso3)
        if route_bundle is not None:
            route, origin_port, dest_port = route_bundle
            transit_days = float(route.transit_days)
            insurance = float(route.insurance_premium_multiplier)
            port_status = dest_port.status
            origin_port_id = origin_port.id
            dest_port_id = dest_port.id
            route_id = route.id
            chokepoint_ids = list(route.chokepoint_ids)
        else:
            transit_days = cfg.default_transit_days
            insurance = cfg.default_insurance_multiplier
            port_status = "unknown"
            origin_port_id = None
            dest_port_id = None
            route_id = None
            chokepoint_ids = []

        cp_max, _cp_avg = _chokepoint_geo_risk(twin, chokepoint_ids)
        sanctions_reasons = sanctions.get(cap.country_iso3.upper(), [])
        sanctions_risk = 1.0 if sanctions_reasons else 0.0

        blend = _blend_compatibility(twin, grade_id, cfg.consumer_country_iso3) if grade_id else 0.5
        compatible_refs = _refineries_accepting_grade(twin, grade_id) if grade_id else []

        candidates.append(
            SupplierCandidate(
                supplier_country_iso3=cap.country_iso3,
                supplier_country_name=country_names.get(cap.country_iso3.upper()),
                grade_id=grade_id,
                grade_name=grade.name if grade else None,
                spare_capacity_kbd=float(cap.spare_capacity_kbd),
                cost_index=cost_index,
                reference_price_usd_bbl=round(price, 2) if price is not None else None,
                transit_days=transit_days,
                insurance_multiplier=insurance,
                port_status=port_status,
                origin_port_id=origin_port_id,
                destination_port_id=dest_port_id,
                route_id=route_id,
                chokepoint_ids=chokepoint_ids,
                chokepoint_risk_max=round(cp_max, 4),
                blend_compatibility=round(blend, 4),
                compatible_refineries=compatible_refs,
                sanctions_risk=sanctions_risk,
                sanctions_reasons=sanctions_reasons,
                geopolitical_risk=round(cp_max, 4),
                source="twin",
            )
        )
    return candidates


# ---------------------------------------------------------------------------
# Scoring + constraint checklist
# ---------------------------------------------------------------------------


def _objective_profile(mission_objective: str) -> dict[str, float]:
    key = str(mission_objective or "").strip().lower()
    if key in {"minimize_import_cost", "cost", "optimize_cost"}:
        return {
            "w_geo": 0.20,
            "w_san": 0.22,
            "w_shock": 0.48,
            "w_blend": 0.08,
            "w_ins": 0.04,
            "w_transit": 0.01,
            "w_capacity_bonus": 0.03,
        }
    if key in {"maximize_supply_resilience", "resilience", "security"}:
        return {
            "w_geo": 0.58,
            "w_san": 0.60,
            "w_shock": 0.24,
            "w_blend": 0.18,
            "w_ins": 0.10,
            "w_transit": 0.06,
            "w_capacity_bonus": 0.12,
        }
    return {
        "w_geo": 0.35,
        "w_san": 0.35,
        "w_shock": 0.40,
        "w_blend": 0.10,
        "w_ins": 0.05,
        "w_transit": 0.02,
        "w_capacity_bonus": 0.07,
    }


def _risk_adjusted_cost(c: SupplierCandidate, disruption_prob: float, price_shock_pct: float, cfg: ProcurementAgentConfig) -> float:
    w = _objective_profile(cfg.mission_objective)
    capacity_ratio = min(1.0, max(0.0, c.spare_capacity_kbd / max(cfg.demand_kbd, 1.0)))
    return (
        c.cost_index
        + w["w_geo"] * disruption_prob * c.geopolitical_risk
        + w["w_san"] * c.sanctions_risk
        + w["w_shock"] * price_shock_pct
        + w["w_blend"] * (1.0 - c.blend_compatibility)
        + w["w_ins"] * (c.insurance_multiplier - 1.0)
        + w["w_transit"] * (c.transit_days / 7.0)
        - w["w_capacity_bonus"] * capacity_ratio
    )


def _check_constraints(c: SupplierCandidate) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (constraint_checklist, rejected_reasons)."""
    checks: list[dict[str, Any]] = []
    rejected: list[str] = []

    port_ok = c.port_status not in ("closed",)
    checks.append(
        {
            "name": "destination_port_open",
            "satisfied": port_ok,
            "detail": f"port_status={c.port_status}"
            + (f", route={c.route_id}" if c.route_id else ""),
        }
    )
    if not port_ok:
        rejected.append(f"Destination port {c.destination_port_id or 'unknown'} is closed.")

    sanctions_ok = c.sanctions_risk < 0.5
    checks.append(
        {
            "name": "sanctions_clear",
            "satisfied": sanctions_ok,
            "detail": ", ".join(c.sanctions_reasons[:3]) if c.sanctions_reasons else "no known sanctions",
        }
    )
    if not sanctions_ok:
        rejected.append(
            f"Supplier country {c.supplier_country_iso3} appears on sanctions lists."
        )

    blend_ok = c.blend_compatibility >= 0.15
    checks.append(
        {
            "name": "blend_compatible",
            "satisfied": blend_ok,
            "detail": f"{c.blend_compatibility * 100:.0f}% of consumer refineries can process "
            + (c.grade_name or c.grade_id or "grade"),
        }
    )
    if not blend_ok:
        rejected.append(
            f"Blend mismatch — no Indian refinery accepts {c.grade_name or c.grade_id}."
        )

    chokepoint_ok = c.chokepoint_risk_max < 1.0
    checks.append(
        {
            "name": "chokepoint_open",
            "satisfied": chokepoint_ok,
            "detail": (
                f"max chokepoint risk={c.chokepoint_risk_max:.2f} across {c.chokepoint_ids or 'none'}"
            ),
        }
    )
    if not chokepoint_ok:
        rejected.append("Chokepoint on route is closed.")

    capacity_ok = c.spare_capacity_kbd > 0
    checks.append(
        {
            "name": "spare_capacity_available",
            "satisfied": capacity_ok,
            "detail": f"{c.spare_capacity_kbd:.0f} kbd spare",
        }
    )
    if not capacity_ok:
        rejected.append("No spare capacity.")

    return checks, rejected


def _scorecard(c: SupplierCandidate, risk_adjusted_cost: float) -> dict[str, Any]:
    return {
        "cost_index": round(c.cost_index, 4),
        "reference_price_usd_bbl": c.reference_price_usd_bbl,
        "risk_adjusted_cost": round(risk_adjusted_cost, 4),
        "transit_days": round(c.transit_days, 2),
        "insurance_multiplier": round(c.insurance_multiplier, 3),
        "port_status": c.port_status,
        "geopolitical_risk": round(c.geopolitical_risk, 4),
        "sanctions_risk": round(c.sanctions_risk, 4),
        "blend_compatibility_pct": round(c.blend_compatibility * 100.0, 2),
        "compatible_refineries": c.compatible_refineries,
        "spare_capacity_kbd": round(c.spare_capacity_kbd, 2),
        "chokepoint_ids": c.chokepoint_ids,
        "chokepoint_risk_max": round(c.chokepoint_risk_max, 4),
    }


def _confidence(c: SupplierCandidate, checks: list[dict[str, Any]]) -> float:
    satisfied = sum(1 for chk in checks if chk["satisfied"])
    base = satisfied / max(1, len(checks))
    penalty = 0.15 * c.geopolitical_risk + 0.25 * c.sanctions_risk
    return round(max(0.0, min(1.0, base - penalty)), 4)


def _rank_and_allocate(
    candidates: list[SupplierCandidate],
    demand_kbd: float,
    disruption_prob: float,
    price_shock_pct: float,
    cfg: ProcurementAgentConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float, float, float]:
    """Compute per-candidate ranking + greedy allocation over eligible ones."""
    scored: list[dict[str, Any]] = []
    for c in candidates:
        rac = _risk_adjusted_cost(c, disruption_prob, price_shock_pct, cfg)
        checks, rejected_reasons = _check_constraints(c)
        rejected = bool(rejected_reasons)
        scorecard = _scorecard(c, rac)
        confidence = _confidence(c, checks)
        scored.append(
            {
                "candidate": c,
                "risk_adjusted_cost": rac,
                "constraints": checks,
                "rejected_reasons": rejected_reasons,
                "rejected": rejected,
                "scorecard": scorecard,
                "confidence": confidence,
            }
        )
    scored.sort(key=lambda x: (x["rejected"], x["risk_adjusted_cost"]))

    remaining = demand_kbd
    allocations: list[dict[str, Any]] = []
    total_weighted_cost = 0.0
    total_secured = 0.0

    ranking: list[dict[str, Any]] = []
    for rank_idx, row in enumerate(scored, start=1):
        c: SupplierCandidate = row["candidate"]
        allocated = 0.0
        status = "rejected" if row["rejected"] else "candidate"
        if not row["rejected"] and remaining > 0:
            allocated = min(c.spare_capacity_kbd, remaining)
            if allocated > 0:
                total_secured += allocated
                remaining -= allocated
                total_weighted_cost += allocated * row["risk_adjusted_cost"]
                allocations.append(
                    {
                        "supplier_country_iso3": c.supplier_country_iso3,
                        "supplier_country_name": c.supplier_country_name,
                        "grade_id": c.grade_id,
                        "grade_name": c.grade_name,
                        "allocated_kbd": round(allocated, 2),
                        "risk_adjusted_cost": round(row["risk_adjusted_cost"], 4),
                        "confidence": row["confidence"],
                    }
                )
                status = "selected"

        # Composite normalized score (higher = better). Simple monotone
        # transform of the risk-adjusted cost with sanctions/reject penalty.
        raw = 1.0 / (1.0 + max(0.0, row["risk_adjusted_cost"]))
        score = max(0.0, min(1.0, raw - (0.35 if row["rejected"] else 0.0)))

        why_ranked = _explain_rank(status, row, rank_idx)

        ranking.append(
            {
                "rank": rank_idx,
                "status": status,
                "score": round(score, 4),
                "supplier_country_iso3": c.supplier_country_iso3,
                "supplier_country_name": c.supplier_country_name,
                "grade_id": c.grade_id,
                "grade_name": c.grade_name,
                "allocated_kbd": round(allocated, 2),
                "scorecard": row["scorecard"],
                "constraints": row["constraints"],
                "rejected_reasons": row["rejected_reasons"],
                "confidence": row["confidence"],
                "why_ranked": why_ranked,
                "source": c.source,
            }
        )

    return ranking, allocations, total_weighted_cost, total_secured, remaining


def _explain_rank(status: str, row: dict[str, Any], rank_idx: int) -> str:
    c: SupplierCandidate = row["candidate"]
    grade = c.grade_name or c.grade_id or "unspecified grade"
    if status == "selected":
        return (
            f"Selected at rank #{rank_idx} — lowest eligible risk-adjusted cost "
            f"({row['risk_adjusted_cost']:.3f}) with {c.blend_compatibility * 100:.0f}% blend fit, "
            f"transit {c.transit_days:.0f} days, no blocking constraints."
        )
    if status == "candidate":
        return (
            f"Ranked #{rank_idx} — eligible but demand already covered by cheaper suppliers. "
            f"Risk-adjusted cost {row['risk_adjusted_cost']:.3f}."
        )
    reasons = "; ".join(row["rejected_reasons"]) or "hard constraint failed"
    return (
        f"Rejected at rank #{rank_idx} for {c.supplier_country_iso3} ({grade}): {reasons}"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_procurement_plan(
    sim: SimulationRecord,
    economic: RecommendationRecord | None,
    cfg: ProcurementAgentConfig,
    twin: DigitalTwinState,
) -> RecommendationInput:
    """Build a ranked procurement plan against the current Digital Twin.

    The `twin` argument is required — production procurement must be scored
    against real supplier capacities, live prices, actual routes, and current
    port/chokepoint status. There is no synthetic-supplier fallback.
    """
    if twin is None or not twin.supplier_capacities:
        raise ValueError(
            "generate_procurement_plan requires a Digital Twin with populated "
            "supplier_capacities. Build one with `digital_twin.build_digital_twin(...)`."
        )
    p = sim.percentiles or {}
    disruption_prob = float(p.get("disruption_prob", 0.4))
    price_shock_pct = float(p.get("price_shock_pct", 0.08))

    candidates = _build_twin_universe(twin, cfg)

    ranking, allocations, total_weighted_cost, total_secured, remaining = _rank_and_allocate(
        candidates, cfg.demand_kbd, disruption_prob, price_shock_pct, cfg
    )

    avg_cost = total_weighted_cost / total_secured if total_secured > 0 else 0.0
    gap_kbd = max(0.0, cfg.demand_kbd - total_secured)

    eco = economic.recommendation_payload.get("economic_impact", {}) if economic else {}
    cpi_delta = float(eco.get("cpi_delta_pct", 0.0))
    cad_delta = float(eco.get("cad_delta_pct_of_gdp", 0.0))

    selected = [r for r in ranking if r["status"] == "selected"]
    rejected = [r for r in ranking if r["status"] == "rejected"]

    payload = {
        "procurement": {
            "horizon": sim.horizon,
            "demand_kbd": cfg.demand_kbd,
            "secured_kbd": round(total_secured, 2),
            "gap_kbd": round(gap_kbd, 2),
            "avg_risk_adjusted_cost": round(avg_cost, 4),
            "allocations": allocations,
            "universe_source": "digital_twin",
            "candidate_count": len(candidates),
            "selected_count": len(selected),
            "rejected_count": len(rejected),
        },
        "ranking": ranking,
        "context": {
            "disruption_prob": round(disruption_prob, 4),
            "price_shock_pct": round(price_shock_pct, 4),
            "economic_cpi_delta_pct": round(cpi_delta, 4),
            "economic_cad_delta_pct_of_gdp": round(cad_delta, 4),
            "mission_objective": cfg.mission_objective,
            "objective_profile": _objective_profile(cfg.mission_objective),
        },
    }

    coverage = total_secured / cfg.demand_kbd if cfg.demand_kbd > 0 else 0.0
    score = max(0.0, min(1.0, 0.65 * coverage + 0.35 * (1.0 - min(1.5, avg_cost) / 1.5)))

    return RecommendationInput(
        simulation_id=sim.id if sim.id and sim.id > 0 else None,
        recommendation_type="procurement_plan",
        recommendation_payload=payload,
        score=round(score, 4),
    )


__all__ = [
    "ProcurementAgentConfig",
    "SupplierCandidate",
    "generate_procurement_plan",
]
