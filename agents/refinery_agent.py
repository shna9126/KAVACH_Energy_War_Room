"""Refinery Impact Intelligence (PRD v2 Upgrade 5).

Assesses simulation-driven impact on each Indian refinery in the Digital
Twin: expected utilization, throughput drop, feedstock gap in days, downtime
probability, and the cheapest still-available compatible crude grade.

Definition of Done (per PRD v2):
    Every disruption simulation reports refinery-specific impact.

Feeds: procurement agent (via `context.refinery_impact` on the recommendation
payload, for later blending), Frontend war-room panel, what-if engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from digital_twin.graph_state import DigitalTwinState
from ingestion.storage import RecommendationInput, SimulationRecord


@dataclass
class RefineryAgentConfig:
    country_iso3: str = "IND"
    # If a refinery has zero available compatible grades, floor its
    # utilization at this minimum viable steady-state.
    min_utilization_when_starved: float = 0.20
    # Baseline downtime probability floor even when compatibility is full.
    baseline_downtime_prob: float = 0.05
    # Weight the disruption-prob channel vs the compatibility-headroom
    # channel when computing throughput drop.
    disruption_weight: float = 0.55
    compatibility_weight: float = 0.45
    mission_objective: str = "balanced_resilience"


def _objective_refinery_adjustments(mission_objective: str) -> tuple[float, float]:
    key = str(mission_objective or "").strip().lower()
    if key in {"maximize_supply_resilience", "resilience", "security"}:
        return 0.92, -0.03
    if key in {"minimize_import_cost", "cost", "optimize_cost"}:
        return 1.06, 0.03
    if key in {"maintain_import_coverage", "coverage"}:
        return 0.97, -0.01
    return 1.0, 0.0


# ---------------------------------------------------------------------------
# Twin helpers
# ---------------------------------------------------------------------------


def _sanctioned_countries(twin: DigitalTwinState) -> set[str]:
    country_names = {c.name.lower(): c.iso3 for c in twin.countries}
    country_iso = {c.iso3.upper() for c in twin.countries}
    excluded: set[str] = set()
    for entry in twin.sanctions:
        low = entry.entity.lower()
        for name, iso in country_names.items():
            if name and name in low:
                excluded.add(iso)
        for iso in country_iso:
            if iso.lower() in low.split():
                excluded.add(iso)
    return excluded


def _grade_available_supply(twin: DigitalTwinState, excluded: set[str]) -> dict[str, float]:
    """Return grade_id → total spare capacity across non-excluded suppliers."""
    out: dict[str, float] = {}
    for cap in twin.supplier_capacities:
        if not cap.grade_id:
            continue
        if cap.country_iso3.upper() in excluded:
            continue
        out[cap.grade_id] = out.get(cap.grade_id, 0.0) + max(0.0, cap.spare_capacity_kbd)
    return out


def _grade_reference_price(twin: DigitalTwinState) -> dict[str, float]:
    grade_price: dict[str, float] = {}
    for pp in twin.prices:
        if pp.grade_id not in grade_price:
            grade_price[pp.grade_id] = pp.price_usd_per_bbl
    return grade_price


def _country_transit_blocked(twin: DigitalTwinState, source_iso3: str, dest_country_iso3: str) -> bool:
    """True if every route between the source country's ports and the
    destination country's ports has a closed chokepoint or a closed port.

    Conservative — if any single route is still viable, we treat the corridor
    as open.
    """
    source_ports = {p.id for p in twin.ports if p.country_iso3.upper() == source_iso3.upper()}
    dest_ports = {p.id for p in twin.ports if p.country_iso3.upper() == dest_country_iso3.upper()}
    if not source_ports or not dest_ports:
        return False

    closed_cps = {c.id for c in twin.chokepoints if c.status == "closed"}
    closed_ports = {p.id for p in twin.ports if p.status == "closed"}

    candidate_routes = [
        r
        for r in twin.routes
        if r.origin_port_id in source_ports and r.destination_port_id in dest_ports
    ]
    if not candidate_routes:
        # No known direct corridor — don't over-claim disruption.
        return False

    for route in candidate_routes:
        if route.origin_port_id in closed_ports or route.destination_port_id in closed_ports:
            continue
        if any(cp in closed_cps for cp in route.chokepoint_ids):
            continue
        return False  # Found a viable route → not blocked
    return True


# ---------------------------------------------------------------------------
# Core assessment
# ---------------------------------------------------------------------------


def _assess_one_refinery(
    refinery,
    twin: DigitalTwinState,
    sim_percentiles: dict[str, Any],
    grade_supply: dict[str, float],
    grade_price: dict[str, float],
    excluded_countries: set[str],
    cfg: RefineryAgentConfig,
) -> dict[str, Any]:
    disruption_prob = float(sim_percentiles.get("disruption_prob", 0.4))
    duration_days = float(sim_percentiles.get("duration_days", 10.0))
    price_shock_pct = float(sim_percentiles.get("price_shock_pct", 0.08))

    compatible_ids = list(refinery.compatible_grade_ids or [])
    grade_lookup = {g.id: g for g in twin.crude_grades}

    available: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for gid in compatible_ids:
        grade = grade_lookup.get(gid)
        if grade is None:
            continue
        source_iso = grade.source_country_iso3
        supply_kbd = grade_supply.get(gid, 0.0)
        sanctioned = source_iso.upper() in excluded_countries
        transit_blocked = _country_transit_blocked(twin, source_iso, refinery.country_iso3)
        reason: str | None = None
        if sanctioned:
            reason = f"source country {source_iso} sanctioned"
        elif supply_kbd <= 0:
            reason = "no spare capacity"
        elif transit_blocked:
            reason = "transit corridor blocked"

        entry = {
            "grade_id": gid,
            "grade_name": grade.name,
            "source_country_iso3": source_iso,
            "spare_capacity_kbd": round(supply_kbd, 2),
            "reference_price_usd_bbl": round(grade_price.get(gid), 2) if grade_price.get(gid) is not None else None,
        }
        if reason is None:
            available.append(entry)
        else:
            entry["blocked_reason"] = reason
            unavailable.append(entry)

    compat_total = max(1, len(compatible_ids))
    compat_headroom = len(available) / compat_total  # 0..1
    starved = len(available) == 0

    # Throughput drop: two channels combined.
    disruption_channel = disruption_prob * (1.0 - compat_headroom)
    compatibility_channel = (1.0 - compat_headroom) ** 2
    drop_mult, downtime_shift = _objective_refinery_adjustments(cfg.mission_objective)
    raw_drop = (
        cfg.disruption_weight * disruption_channel
        + cfg.compatibility_weight * compatibility_channel
    )
    drop_pct = min(1.0 - cfg.min_utilization_when_starved, max(0.0, raw_drop * drop_mult))
    if starved:
        drop_pct = max(drop_pct, 1.0 - cfg.min_utilization_when_starved)

    baseline_utilization = float(refinery.utilization_pct)
    new_utilization = max(cfg.min_utilization_when_starved, baseline_utilization * (1.0 - drop_pct))
    baseline_throughput = refinery.capacity_kbd * baseline_utilization
    expected_throughput = refinery.capacity_kbd * new_utilization
    throughput_loss = max(0.0, baseline_throughput - expected_throughput)

    downtime_prob = min(
        1.0,
        max(
            0.0,
            cfg.baseline_downtime_prob
            + disruption_prob * (1.0 - compat_headroom)
            + (0.20 if starved else 0.0)
            + downtime_shift,
        ),
    )

    feedstock_gap_days = round(duration_days * (1.0 - compat_headroom), 2)

    # Recommended crude: cheapest still-available compatible grade
    recommended: dict[str, Any] | None = None
    if available:
        by_price = sorted(
            available,
            key=lambda e: (
                e["reference_price_usd_bbl"] if e["reference_price_usd_bbl"] is not None else float("inf"),
                -e["spare_capacity_kbd"],
            ),
        )
        recommended = by_price[0]

    return {
        "refinery_id": refinery.id,
        "refinery_name": refinery.name,
        "operator": refinery.operator,
        "country_iso3": refinery.country_iso3,
        "capacity_kbd": refinery.capacity_kbd,
        "baseline_utilization_pct": round(baseline_utilization * 100.0, 2),
        "expected_utilization_pct": round(new_utilization * 100.0, 2),
        "baseline_throughput_kbd": round(baseline_throughput, 2),
        "expected_throughput_kbd": round(expected_throughput, 2),
        "throughput_loss_kbd": round(throughput_loss, 2),
        "feedstock_gap_days": feedstock_gap_days,
        "downtime_probability": round(downtime_prob, 4),
        "compatibility_headroom_pct": round(compat_headroom * 100.0, 2),
        "starved": starved,
        "compatible_grades_total": len(compatible_ids),
        "compatible_grades_available": len(available),
        "recommended_crude": recommended,
        "available_grades": available,
        "unavailable_grades": unavailable,
        "context": {
            "disruption_prob": round(disruption_prob, 4),
            "duration_days": round(duration_days, 2),
            "price_shock_pct": round(price_shock_pct, 4),
        },
    }


def assess_refinery_impact(
    sim: SimulationRecord,
    twin: DigitalTwinState | None,
    cfg: RefineryAgentConfig | None = None,
) -> RecommendationInput:
    """Produce one `refinery_impact` recommendation per simulation.

    If `twin` is None or has no refineries, emits an empty payload so the DoD
    ("every disruption simulation reports refinery-specific impact") is still
    honoured with a clear diagnostic.
    """
    cfg = cfg or RefineryAgentConfig()
    sim_percentiles = sim.percentiles or {}

    if twin is None or not twin.refineries:
        payload = {
            "refinery_impact": {
                "horizon": sim.horizon,
                "country_iso3": cfg.country_iso3,
                "refineries": [],
                "aggregate": {
                    "refinery_count": 0,
                    "baseline_throughput_kbd": 0.0,
                    "expected_throughput_kbd": 0.0,
                    "throughput_loss_kbd": 0.0,
                    "avg_utilization_pct_after": None,
                    "worst_hit_refinery_id": None,
                },
                "notes": "Digital Twin not supplied — refinery impact deferred.",
            },
            "context": dict(sim_percentiles),
        }
        return RecommendationInput(
            simulation_id=sim.id if sim.id and sim.id > 0 else None,
            recommendation_type="refinery_impact",
            recommendation_payload=payload,
            score=0.0,
        )

    excluded = _sanctioned_countries(twin)
    grade_supply = _grade_available_supply(twin, excluded)
    grade_price = _grade_reference_price(twin)

    targeted = [r for r in twin.refineries if r.country_iso3.upper() == cfg.country_iso3.upper()]
    if not targeted:
        targeted = list(twin.refineries)

    entries = [
        _assess_one_refinery(r, twin, sim_percentiles, grade_supply, grade_price, excluded, cfg)
        for r in targeted
    ]

    baseline_total = sum(e["baseline_throughput_kbd"] for e in entries)
    expected_total = sum(e["expected_throughput_kbd"] for e in entries)
    total_loss = max(0.0, baseline_total - expected_total)
    avg_util = (
        sum(e["expected_utilization_pct"] for e in entries) / len(entries)
        if entries
        else None
    )

    def _util_drop_pct(entry: dict[str, Any]) -> float:
        base = entry["baseline_utilization_pct"]
        return (base - entry["expected_utilization_pct"]) / base if base > 0 else 0.0

    worst_by_loss = max(entries, key=lambda e: e["throughput_loss_kbd"]) if entries else None
    worst_by_util = max(entries, key=_util_drop_pct) if entries else None

    payload = {
        "refinery_impact": {
            "horizon": sim.horizon,
            "country_iso3": cfg.country_iso3,
            "mission_objective": cfg.mission_objective,
            "refineries": entries,
            "aggregate": {
                "refinery_count": len(entries),
                "baseline_throughput_kbd": round(baseline_total, 2),
                "expected_throughput_kbd": round(expected_total, 2),
                "throughput_loss_kbd": round(total_loss, 2),
                "avg_utilization_pct_after": round(avg_util, 2) if avg_util is not None else None,
                "worst_hit_refinery_id": worst_by_util["refinery_id"] if worst_by_util else None,
                "worst_hit_refinery_name": worst_by_util["refinery_name"] if worst_by_util else None,
                "worst_by_absolute_loss_refinery_id": worst_by_loss["refinery_id"] if worst_by_loss else None,
                "worst_by_absolute_loss_refinery_name": worst_by_loss["refinery_name"] if worst_by_loss else None,
                "sanctioned_countries_excluded": sorted(excluded),
            },
        },
        "context": dict(sim_percentiles),
    }

    # Score: 1 - (loss / baseline), high = healthy sector
    if baseline_total > 0:
        health = max(0.0, min(1.0, 1.0 - total_loss / baseline_total))
    else:
        health = 1.0

    return RecommendationInput(
        simulation_id=sim.id if sim.id and sim.id > 0 else None,
        recommendation_type="refinery_impact",
        recommendation_payload=payload,
        score=round(health, 4),
    )


__all__ = [
    "RefineryAgentConfig",
    "assess_refinery_impact",
]
