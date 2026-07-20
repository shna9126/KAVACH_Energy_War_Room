"""Policy Agent (PRD v2 Upgrade 3 — Strategic Reserve Intelligence).

Extends the original SPR drawdown planner with:

    - Supply gap forecast (driven by simulation + procurement gap)
    - Drawdown optimization (tapered schedule across SPR sites, weighted by
      site capacity)
    - Replenishment optimization (WHEN — trigger price, HOW MUCH — volume,
      FROM WHOM — cheapest supplier from the Digital Twin, AT WHAT PRICE —
      target refill price and cost)
    - Reserve health forecast (per-site fill before/after, utilization %,
      import cover in days)

Definition of Done (per PRD v2): every policy recommendation includes WHEN,
HOW MUCH, FROM WHOM, AT WHAT PRICE. Enforced at the end of
`generate_policy_plan` by `_assert_replenishment_complete`.

The `twin` parameter is optional so existing callers (tests, scripts that
predate the Digital Twin) keep working — they simply skip the new sections.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from digital_twin.graph_state import DigitalTwinState, PricePoint
from ingestion.storage import (
    RecommendationInput,
    RecommendationRecord,
    SimulationRecord,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class PolicyAgentConfig:
    max_spr_draw_mbd: float = 4.5
    strategic_reserve_days: int = 30
    mission_objective: str = "balanced_resilience"
    # Refill parameters (PRD v2 additions)
    refill_trigger_discount_pct: float = 0.15  # wait until price is X% below current
    refill_window_days: int = 30                # spread refill purchases over N days
    refill_cool_off_days: int = 5               # wait this many days after drawdown ends
    default_import_demand_kbd: float = 1800.0
    default_spot_price_usd_bbl: float = 82.0


# ---------------------------------------------------------------------------
# Helpers — pricing & suppliers from the Digital Twin
# ---------------------------------------------------------------------------


def _latest_reference_price(twin: DigitalTwinState | None) -> float:
    if twin is None:
        return 0.0
    brent = twin.latest_price("grade_brent")
    if brent is not None:
        return float(brent.price_usd_per_bbl)
    if twin.prices:
        latest: PricePoint = max(twin.prices, key=lambda p: p.as_of)
        return float(latest.price_usd_per_bbl)
    return 0.0


def _rank_refill_suppliers(
    twin: DigitalTwinState | None,
    excluded_countries: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return supplier candidates sorted by (price ascending, spare capacity descending).

    Uses `twin.supplier_capacities` for supply and `twin.prices` for reference
    price (matched by grade). Sanctioned or excluded suppliers are dropped.
    """
    if twin is None:
        return []

    excluded = {c.upper() for c in (excluded_countries or set())}
    grade_price: dict[str, float] = {}
    for pp in twin.prices:
        if pp.grade_id not in grade_price:
            grade_price[pp.grade_id] = pp.price_usd_per_bbl

    ranked: list[dict[str, Any]] = []
    for cap in twin.supplier_capacities:
        if cap.country_iso3.upper() in excluded:
            continue
        if cap.spare_capacity_kbd <= 0:
            continue
        price = grade_price.get(cap.grade_id or "") if cap.grade_id else None
        if price is None and grade_price:
            price = sum(grade_price.values()) / len(grade_price)
        ranked.append(
            {
                "country_iso3": cap.country_iso3,
                "grade_id": cap.grade_id,
                "spare_capacity_kbd": cap.spare_capacity_kbd,
                "contract_ceiling_kbd": cap.contract_ceiling_kbd,
                "reference_price_usd_bbl": round(price, 2) if price else None,
            }
        )

    def _sort_key(entry: dict[str, Any]) -> tuple[float, float]:
        price = entry["reference_price_usd_bbl"]
        price_key = price if price is not None else math.inf
        return (price_key, -entry["spare_capacity_kbd"])

    ranked.sort(key=_sort_key)
    return ranked


def _sanction_excluded_countries(twin: DigitalTwinState | None) -> set[str]:
    """Extract country codes appearing as sanctioned entities in the twin."""
    if twin is None or not twin.sanctions:
        return set()

    country_names = {c.name.lower(): c.iso3 for c in twin.countries}
    country_iso = {c.iso3.upper() for c in twin.countries}
    excluded: set[str] = set()
    for entry in twin.sanctions:
        entity_lower = entry.entity.lower()
        for name, iso in country_names.items():
            if name and name in entity_lower:
                excluded.add(iso)
        for iso in country_iso:
            if iso.lower() in entity_lower.split():
                excluded.add(iso)
    return excluded


# ---------------------------------------------------------------------------
# Drawdown scheduling
# ---------------------------------------------------------------------------


def _drawdown_schedule(
    spr_draw_mbd: float,
    schedule_days: int,
    twin: DigitalTwinState | None,
) -> list[dict[str, Any]]:
    """Tapered per-day drawdown, weighted across SPR sites by capacity."""
    schedule: list[dict[str, Any]] = []
    sites = list(twin.spr_sites) if twin and twin.spr_sites else []
    total_site_capacity = sum(s.max_drawdown_mbd for s in sites) or 1.0

    for day in range(1, schedule_days + 1):
        taper = 1.0 - ((day - 1) / max(1, schedule_days - 1)) * 0.35
        draw_total = round(spr_draw_mbd * taper, 3)
        per_site: list[dict[str, Any]] = []
        if sites:
            for site in sites:
                weight = site.max_drawdown_mbd / total_site_capacity
                per_site.append(
                    {
                        "spr_site_id": site.id,
                        "spr_site_name": site.name,
                        "draw_mbd": round(min(draw_total * weight, site.max_drawdown_mbd), 3),
                    }
                )
        schedule.append(
            {
                "day": day,
                "spr_draw_mbd": draw_total,
                "per_site": per_site,
            }
        )
    return schedule


# ---------------------------------------------------------------------------
# Reserve health & import cover
# ---------------------------------------------------------------------------


def _reserve_health(
    twin: DigitalTwinState | None,
    total_drawdown_mbbl: float,
    refill_volume_mbbl: float,
    demand_kbd: float,
) -> dict[str, Any]:
    if twin is None or not twin.spr_sites:
        return {
            "total_capacity_mbbl": None,
            "before_fill_mbbl": None,
            "after_drawdown_mbbl": None,
            "after_refill_mbbl": None,
            "sites": [],
            "days_of_import_cover_before": None,
            "days_of_import_cover_after_drawdown": None,
            "days_of_import_cover_after_refill": None,
        }

    total_capacity_mbbl = sum(s.capacity_mbbl for s in twin.spr_sites)
    total_capacity_mbbl = max(total_capacity_mbbl, 1e-6)
    before = sum(s.current_fill_mbbl for s in twin.spr_sites)
    after_drawdown = max(0.0, before - total_drawdown_mbbl)
    after_refill = min(total_capacity_mbbl, after_drawdown + refill_volume_mbbl)

    demand_mbd = max(demand_kbd / 1000.0, 1e-6)

    sites_out: list[dict[str, Any]] = []
    drawdown_share = total_drawdown_mbbl / before if before > 0 and total_drawdown_mbbl > 0 else 0.0
    capacity_gap_total = max(0.0, total_capacity_mbbl - after_drawdown)
    refill_share = (
        refill_volume_mbbl / capacity_gap_total if capacity_gap_total > 0 and refill_volume_mbbl > 0 else 0.0
    )

    for site in twin.spr_sites:
        site_draw = round(site.current_fill_mbbl * drawdown_share, 3)
        site_after_draw = max(0.0, site.current_fill_mbbl - site_draw)
        site_capacity_gap = max(0.0, site.capacity_mbbl - site_after_draw)
        site_refill = round(site_capacity_gap * refill_share, 3)
        site_after_refill = min(site.capacity_mbbl, site_after_draw + site_refill)
        sites_out.append(
            {
                "spr_site_id": site.id,
                "spr_site_name": site.name,
                "capacity_mbbl": site.capacity_mbbl,
                "fill_before_mbbl": round(site.current_fill_mbbl, 3),
                "fill_after_drawdown_mbbl": round(site_after_draw, 3),
                "fill_after_refill_mbbl": round(site_after_refill, 3),
                "utilization_pct_after_drawdown": round(site_after_draw / site.capacity_mbbl * 100.0, 2),
                "utilization_pct_after_refill": round(site_after_refill / site.capacity_mbbl * 100.0, 2),
            }
        )

    return {
        "total_capacity_mbbl": round(total_capacity_mbbl, 3),
        "before_fill_mbbl": round(before, 3),
        "after_drawdown_mbbl": round(after_drawdown, 3),
        "after_refill_mbbl": round(after_refill, 3),
        "sites": sites_out,
        "days_of_import_cover_before": round(before / demand_mbd, 2),
        "days_of_import_cover_after_drawdown": round(after_drawdown / demand_mbd, 2),
        "days_of_import_cover_after_refill": round(after_refill / demand_mbd, 2),
    }


# ---------------------------------------------------------------------------
# Replenishment optimization
# ---------------------------------------------------------------------------


def _plan_replenishment(
    twin: DigitalTwinState | None,
    cfg: PolicyAgentConfig,
    total_drawdown_mbbl: float,
    drawdown_end_day: int,
    disruption_prob: float,
) -> dict[str, Any]:
    """Choose refill supplier, trigger price, schedule, cost, and savings."""
    spot_price = _latest_reference_price(twin) or cfg.default_spot_price_usd_bbl
    excluded = _sanction_excluded_countries(twin)
    candidates = _rank_refill_suppliers(twin, excluded)

    if total_drawdown_mbbl <= 0:
        return {
            "when_day": None,
            "trigger_price_usd_bbl": None,
            "target_supplier_iso3": None,
            "target_grade_id": None,
            "refill_volume_mbbl": 0.0,
            "refill_window_days": cfg.refill_window_days,
            "refill_schedule": [],
            "estimated_cost_usd_bn": 0.0,
            "estimated_savings_vs_spot_usd_bn": 0.0,
            "spot_price_usd_bbl": round(spot_price, 2),
            "rationale": "No SPR drawdown planned; replenishment not required.",
            "excluded_countries": sorted(excluded),
            "candidate_suppliers": candidates[:5],
        }

    if not candidates:
        # No supplier universe (typically: twin not supplied). Emit an honest
        # empty plan so the DoD assertion still holds — the pipeline must call
        # this agent with a Digital Twin to get a fully specified refill plan.
        return {
            "when_day": None,
            "trigger_price_usd_bbl": None,
            "target_supplier_iso3": None,
            "target_grade_id": None,
            "refill_volume_mbbl": 0.0,
            "refill_window_days": cfg.refill_window_days,
            "refill_schedule": [],
            "estimated_cost_usd_bn": 0.0,
            "estimated_savings_vs_spot_usd_bn": 0.0,
            "spot_price_usd_bbl": round(spot_price, 2),
            "rationale": (
                "Drawdown planned but supplier universe unavailable "
                "(Digital Twin not provided) — refill plan deferred."
            ),
            "excluded_countries": sorted(excluded),
            "candidate_suppliers": [],
        }

    # Trigger price = spot × (1 - discount); scaled down when disruption is
    # severe (larger disruptions accept a smaller discount to avoid missing
    # the window entirely).
    discount = cfg.refill_trigger_discount_pct * (1.0 - min(0.5, disruption_prob))
    trigger_price = round(spot_price * (1.0 - discount), 2)

    supplier = candidates[0] if candidates else None
    when_day = drawdown_end_day + cfg.refill_cool_off_days

    volume_mbbl = round(total_drawdown_mbbl, 3)
    # volume_mbbl (million bbl) × 1000 → kbbl total, ÷ window_days → kbd
    daily_kbd = round((volume_mbbl * 1000.0) / max(1, cfg.refill_window_days), 3)
    if supplier is not None:
        daily_kbd = min(daily_kbd, float(supplier["spare_capacity_kbd"]))
    effective_days = min(
        cfg.refill_window_days,
        max(1, int(math.ceil(volume_mbbl * 1000.0 / max(daily_kbd, 1e-6)))),
    )

    refill_schedule = [
        {
            "day": when_day + i,
            "refill_kbd": daily_kbd,
            "supplier_iso3": supplier["country_iso3"] if supplier else None,
        }
        for i in range(effective_days)
    ]

    # mbbl × $/bbl → $M; ÷ 1000 → $B
    cost_usd_bn = round(volume_mbbl * trigger_price / 1000.0, 3)
    savings_usd_bn = round(volume_mbbl * (spot_price - trigger_price) / 1000.0, 3)

    supplier_iso = supplier["country_iso3"] if supplier else None
    supplier_grade = supplier["grade_id"] if supplier else None
    ref_price = supplier["reference_price_usd_bbl"] if supplier else None
    rationale = (
        f"Refill starts Day {when_day} (cool-off {cfg.refill_cool_off_days}d after drawdown ends) "
        f"once Brent trades ≤ ${trigger_price}/bbl; source {supplier_iso or 'TBD'} "
        f"({supplier_grade or 'grade TBD'}) at reference "
        f"{ref_price if ref_price is not None else 'n/a'} $/bbl — spans {effective_days} days at "
        f"{daily_kbd} kbd."
    )

    return {
        "when_day": when_day,
        "trigger_price_usd_bbl": trigger_price,
        "target_supplier_iso3": supplier_iso,
        "target_grade_id": supplier_grade,
        "refill_volume_mbbl": volume_mbbl,
        "refill_window_days": effective_days,
        "refill_schedule": refill_schedule,
        "estimated_cost_usd_bn": cost_usd_bn,
        "estimated_savings_vs_spot_usd_bn": savings_usd_bn,
        "spot_price_usd_bbl": round(spot_price, 2),
        "rationale": rationale,
        "excluded_countries": sorted(excluded),
        "candidate_suppliers": candidates[:5],
    }


def _assert_replenishment_complete(replenishment: dict[str, Any]) -> None:
    """DoD guard: every non-empty replenishment plan must state WHEN, HOW MUCH,
    FROM WHOM, AT WHAT PRICE. Empty (no drawdown) is allowed.
    """
    if replenishment.get("refill_volume_mbbl", 0.0) <= 0:
        return
    for key in ("when_day", "trigger_price_usd_bbl", "target_supplier_iso3", "refill_volume_mbbl"):
        if replenishment.get(key) in (None, 0, 0.0):
            raise ValueError(
                f"Policy replenishment plan missing required field '{key}' — "
                "PRD v2 DoD requires WHEN, HOW MUCH, FROM WHOM, AT WHAT PRICE."
            )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_policy_plan(
    sim: SimulationRecord,
    procurement: RecommendationRecord | None,
    cfg: PolicyAgentConfig,
    twin: DigitalTwinState | None = None,
) -> RecommendationInput:
    p = sim.percentiles or {}
    disruption_prob = float(p.get("disruption_prob", 0.4))
    duration_days = float(p.get("duration_days", 10.0))

    demand_kbd = cfg.default_import_demand_kbd
    secured_kbd = 0.0
    gap_kbd = demand_kbd * disruption_prob * 0.2
    if procurement:
        proc = procurement.recommendation_payload.get("procurement", {})
        demand_kbd = float(proc.get("demand_kbd", demand_kbd))
        secured_kbd = float(proc.get("secured_kbd", secured_kbd))
        gap_kbd = float(proc.get("gap_kbd", max(0.0, demand_kbd - secured_kbd)))

    required_mbd = max(0.0, gap_kbd / 1000.0)
    mission_key = str(cfg.mission_objective or "").strip().lower()
    resilience_buffer_mbd = 0.0
    if mission_key in {"maximize_supply_resilience", "resilience", "security"}:
        resilience_buffer_mbd = max(0.0, disruption_prob - 0.35) * (demand_kbd / 1000.0) * 0.08
    elif mission_key in {"balanced_resilience", "maintain_import_coverage", "balanced"}:
        resilience_buffer_mbd = max(0.0, disruption_prob - 0.55) * (demand_kbd / 1000.0) * 0.04
    effective_required_mbd = required_mbd + resilience_buffer_mbd

    strategic_floor_mbd = 0.0
    if mission_key in {"maximize_supply_resilience", "resilience", "security"} and disruption_prob >= 0.35:
        strategic_floor_mbd = 0.12
    elif mission_key in {"balanced_resilience", "maintain_import_coverage", "balanced"} and disruption_prob >= 0.60:
        strategic_floor_mbd = 0.15

    spr_draw_mbd = min(
        cfg.max_spr_draw_mbd,
        max(strategic_floor_mbd, effective_required_mbd * (0.5 + 0.5 * disruption_prob)),
    )

    schedule_days = max(1, min(int(round(duration_days)), cfg.strategic_reserve_days))
    schedule = _drawdown_schedule(spr_draw_mbd, schedule_days, twin)
    total_draw_million_barrels = round(sum(x["spr_draw_mbd"] for x in schedule), 3)

    replenishment = _plan_replenishment(
        twin,
        cfg,
        total_drawdown_mbbl=total_draw_million_barrels,
        drawdown_end_day=schedule_days,
        disruption_prob=disruption_prob,
    )
    _assert_replenishment_complete(replenishment)

    reserve_health = _reserve_health(
        twin,
        total_drawdown_mbbl=total_draw_million_barrels,
        refill_volume_mbbl=replenishment.get("refill_volume_mbbl", 0.0),
        demand_kbd=demand_kbd,
    )

    reserve_utilization_pct = None
    total_cap = reserve_health.get("total_capacity_mbbl")
    if total_cap:
        used_after_draw = total_cap - reserve_health["after_drawdown_mbbl"]
        reserve_utilization_pct = round(used_after_draw / total_cap * 100.0, 2)

    payload = {
        "policy": {
            "horizon": sim.horizon,
            "spr_draw_cap_mbd": cfg.max_spr_draw_mbd,
            "recommended_spr_draw_mbd_day1": round(spr_draw_mbd, 3),
            "schedule_days": schedule_days,
            "schedule": schedule,
            "total_draw_million_barrels": total_draw_million_barrels,
            "reserve_utilization_pct_after_drawdown": reserve_utilization_pct,
            "replenishment": replenishment,
        },
        "reserve_health": reserve_health,
        "drivers": {
            "disruption_prob": round(disruption_prob, 4),
            "gap_kbd": round(gap_kbd, 2),
            "duration_days": round(duration_days, 2),
            "demand_kbd": round(demand_kbd, 2),
            "mission_objective": cfg.mission_objective,
            "strategic_floor_mbd": round(strategic_floor_mbd, 3),
            "resilience_buffer_mbd": round(resilience_buffer_mbd, 3),
            "effective_required_mbd": round(effective_required_mbd, 3),
        },
    }

    mitigation = 0.0 if required_mbd <= 0 else min(1.0, spr_draw_mbd / required_mbd)
    refill_bonus = 0.0
    if replenishment.get("estimated_savings_vs_spot_usd_bn"):
        refill_bonus = min(0.15, replenishment["estimated_savings_vs_spot_usd_bn"] / 2.0)
    score = max(0.0, min(1.0, 0.55 * mitigation + 0.30 * disruption_prob + refill_bonus))

    return RecommendationInput(
        simulation_id=sim.id,
        recommendation_type="policy_plan",
        recommendation_payload=payload,
        score=round(score, 4),
    )
