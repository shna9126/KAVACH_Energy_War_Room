"""Explicit Geopolitical → Economic Reasoning Engine (PRD v2 Upgrade 2).

First-class component that turns a structured event + Digital Twin context
into an explainable causal chain with the entities it touches. Elevated out
of `hypothesis_agent` so every downstream recommendation can inherit an
explanation.

Definition of Done (per PRD v2):
    - Every hypothesis carries a structured causal chain.
    - Every recommendation persisted through the pipeline is enforced to
      carry `reasoning_chain` in its payload.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field

from digital_twin.graph_state import DigitalTwinState
from ingestion.storage import HypothesisRecord, RecommendationInput, StructuredEventRecord


EntityKind = Literal[
    "country",
    "chokepoint",
    "port",
    "route",
    "refinery",
    "grade",
    "spr_site",
    "policy",
    "commodity",
]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class CausalStep(BaseModel):
    step_no: int
    claim: str
    entity_kind: EntityKind | None = None
    entity_id: str | None = None
    entity_name: str | None = None
    mechanism: str | None = None  # short causal verb: "closes", "reroutes", "raises"
    evidence_refs: list[str] = Field(default_factory=list)


class AffectedEntities(BaseModel):
    countries: list[str] = Field(default_factory=list)
    chokepoints: list[str] = Field(default_factory=list)
    ports: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)
    refineries: list[str] = Field(default_factory=list)
    grades: list[str] = Field(default_factory=list)
    spr_sites: list[str] = Field(default_factory=list)
    policies: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.countries
            or self.chokepoints
            or self.ports
            or self.routes
            or self.refineries
            or self.grades
            or self.spr_sites
            or self.policies
        )


class CausalChain(BaseModel):
    hypothesis_text: str
    confidence: float
    summary: str
    steps: list[CausalStep] = Field(default_factory=list)
    affected: AffectedEntities = Field(default_factory=AffectedEntities)
    twin_branch_id: str | None = None
    source: Literal["llm", "deterministic", "hybrid"] = "deterministic"

    def as_bullet_lines(self) -> list[str]:
        lines: list[str] = []
        for step in self.steps:
            prefix = f"[{step.entity_kind}:{step.entity_id}] " if step.entity_kind and step.entity_id else ""
            lines.append(f"{prefix}{step.claim}")
        return lines


# ---------------------------------------------------------------------------
# Entity detection over the Digital Twin
# ---------------------------------------------------------------------------


_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z\-']+")


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _WORD_RE.finditer(text)}


def _phrase_hit(haystack: str, phrase: str) -> bool:
    return phrase.lower() in haystack.lower()


def _event_text_blob(event: StructuredEventRecord, hypothesis_text: str | None) -> str:
    parts: list[str] = []
    if event.action_type:
        parts.append(event.action_type)
    if event.target:
        parts.append(event.target)
    parts.extend(event.actors or [])
    parts.append(str(event.extracted_payload or {}))
    if hypothesis_text:
        parts.append(hypothesis_text)
    return " | ".join(parts)


def detect_affected_entities(
    event: StructuredEventRecord,
    twin: DigitalTwinState,
    hypothesis_text: str | None = None,
) -> AffectedEntities:
    """Deterministically map an event onto twin entities via name matching.

    First, find direct hits (country/chokepoint/port/refinery/grade names in
    the event text). Then propagate: a chokepoint hit implies its routes
    which imply their destination ports/refineries and origin countries.
    """
    blob = _event_text_blob(event, hypothesis_text)
    tokens = _tokens(blob)

    countries: set[str] = set()
    chokepoints: set[str] = set()
    ports: set[str] = set()
    routes: set[str] = set()
    refineries: set[str] = set()
    grades: set[str] = set()
    spr_sites: set[str] = set()

    # Country hits (iso3 or name)
    for c in twin.countries:
        if c.iso3.lower() in tokens or _phrase_hit(blob, c.name):
            countries.add(c.iso3)

    # Chokepoint hits (partial name match, e.g. "Hormuz")
    for cp in twin.chokepoints:
        name_words = [w for w in cp.name.replace("-", " ").split() if len(w) > 3]
        if any(w.lower() in tokens for w in name_words) or _phrase_hit(blob, cp.name):
            chokepoints.add(cp.id)

    # Port hits
    for p in twin.ports:
        base = p.name.split(" (")[0]
        if any(w.lower() in tokens for w in base.split() if len(w) > 3):
            ports.add(p.id)

    # Refinery hits
    for r in twin.refineries:
        if r.name.lower() in blob.lower() or r.operator.lower() in blob.lower():
            refineries.add(r.id)

    # Grade hits
    for g in twin.crude_grades:
        if _phrase_hit(blob, g.name):
            grades.add(g.id)

    # ---- Propagation --------------------------------------------------------

    # Chokepoint → routes → destination refineries & origin countries
    for route in twin.routes:
        if any(cp in chokepoints for cp in route.chokepoint_ids):
            routes.add(route.id)
            dest_port = twin.port_by_id(route.destination_port_id)
            if dest_port is not None:
                ports.add(dest_port.id)
                for refinery in twin.refineries:
                    if refinery.country_iso3 == dest_port.country_iso3:
                        refineries.add(refinery.id)
            orig_port = twin.port_by_id(route.origin_port_id)
            if orig_port is not None:
                ports.add(orig_port.id)
                countries.add(orig_port.country_iso3)

    # Country → grades sourced from that country
    for grade in twin.crude_grades:
        if grade.source_country_iso3 in countries:
            grades.add(grade.id)

    # India is affected if any Indian port/refinery is affected
    indian_ports = {p.id for p in twin.ports if p.country_iso3 == "IND"}
    if any(pid in ports for pid in indian_ports) or any(
        r.country_iso3 == "IND" for r in twin.refineries if r.id in refineries
    ):
        countries.add("IND")
        for spr in twin.spr_sites:
            if spr.country_iso3 == "IND":
                spr_sites.add(spr.id)

    return AffectedEntities(
        countries=sorted(countries),
        chokepoints=sorted(chokepoints),
        ports=sorted(ports),
        routes=sorted(routes),
        refineries=sorted(refineries),
        grades=sorted(grades),
        spr_sites=sorted(spr_sites),
    )


# ---------------------------------------------------------------------------
# Chain construction
# ---------------------------------------------------------------------------


def _friendly(entity_kind: EntityKind, entity_id: str, twin: DigitalTwinState) -> str:
    if entity_kind == "country":
        c = next((x for x in twin.countries if x.iso3 == entity_id), None)
        return c.name if c else entity_id
    if entity_kind == "chokepoint":
        cp = twin.chokepoint_by_id(entity_id)
        return cp.name if cp else entity_id
    if entity_kind == "port":
        p = twin.port_by_id(entity_id)
        return p.name if p else entity_id
    if entity_kind == "refinery":
        r = twin.refinery_by_id(entity_id)
        return r.name if r else entity_id
    if entity_kind == "grade":
        g = twin.grade_by_id(entity_id)
        return g.name if g else entity_id
    if entity_kind == "spr_site":
        s = next((x for x in twin.spr_sites if x.id == entity_id), None)
        return s.name if s else entity_id
    return entity_id


def _describe_action_plain(action: str, target: str, actors: list[str]) -> str:
    """Convert DB action_type + target into a plain-English opening sentence."""
    a = action.lower().replace("_", " ")
    t = target.replace("_", " ")
    actor_str = " and ".join(actors[:2]) if actors else "an unidentified party"

    if any(w in a for w in ("sanction", "blacklist", "embargo", "restrict")):
        return (
            f"{actor_str} has been placed under international sanctions, restricting its ability "
            f"to operate in energy trade corridors. Tankers and counterparties linked to this entity "
            f"must now avoid trading with it — immediately creating uncertainty around crude shipments "
            f"to {t}."
        )
    if any(w in a for w in ("military", "attack", "strike", "conflict", "war", "seize", "blockade")):
        return (
            f"A security or military incident involving {actor_str} has been detected near {t}. "
            f"This raises the risk of physical disruption to nearby shipping lanes, loading terminals, "
            f"or export infrastructure in the region."
        )
    if any(w in a for w in ("weather", "storm", "cyclone", "flood", "hurricane", "typhoon")):
        return (
            f"Severe weather is affecting {t} — ports are suspending loading operations and tankers "
            f"are diverting away from the area. {actor_str} is responding to the emergency."
        )
    if any(w in a for w in ("opec", "output", "cut", "production", "quota")):
        return (
            f"{actor_str} has announced a change in crude oil output or quota for {t}. "
            f"This directly shifts the supply available to importing refiners — any cut means "
            f"tighter market conditions and higher spot prices."
        )
    if any(w in a for w in ("price", "rally", "spike", "drop", "crash")):
        return (
            f"A significant price movement in {t} has been detected, with {actor_str} as a key driver. "
            f"This affects the cost of crude imports and downstream refinery margins."
        )
    # Generic fallback — still human-readable
    readable_action = a.title()
    return (
        f"An intelligence signal has flagged a '{readable_action}' event linked to {actor_str}, "
        f"targeting {t}. This creates elevated uncertainty in the affected energy corridor."
    )


def _default_steps(
    event: StructuredEventRecord,
    twin: DigitalTwinState,
    affected: AffectedEntities,
    hypothesis_text: str | None,
    raw_reasoning_steps: list[str] | None,
) -> list[CausalStep]:
    """Build a clear, layman-friendly narrative causal chain.

    Each step answers "what happened, what does it mean, why does India care?"
    in plain English — no internal IDs, no database jargon.
    """
    steps: list[CausalStep] = []
    step_no = 1

    action = (event.action_type or "signal").strip()
    target = (event.target or "supply chain").strip()
    actors = event.actors[:3] if event.actors else []

    # Live evidence snapshot used to ground the narrative in observable data.
    tanker_count = len([t for t in twin.tankers if t.lat is not None and t.lon is not None])
    top_cp = max(twin.chokepoints, key=lambda c: c.risk_score, default=None)
    brent = twin.latest_price("grade_brent")
    wti = twin.latest_price("grade_wti")

    # ------------------------------------------------------------------
    # Step 1: Event trigger — plain English description of what happened
    # ------------------------------------------------------------------
    opening = _describe_action_plain(action, target, actors)
    steps.append(CausalStep(
        step_no=step_no,
        claim=opening,
        mechanism="triggers",
        evidence_refs=[f"structured_event:{event.id}"],
    ))
    step_no += 1

    # ------------------------------------------------------------------
    # Step 2: What live feeds currently show (AIS + risk + prices)
    # ------------------------------------------------------------------
    evidence_bits: list[str] = []
    evidence_bits.append(f"AIS currently tracks {tanker_count} active vessel positions")
    if top_cp is not None:
        risk_band = (
            "high" if top_cp.risk_score >= 0.6 else
            "elevated" if top_cp.risk_score >= 0.4 else
            "low"
        )
        evidence_bits.append(f"highest corridor risk is {top_cp.name} ({top_cp.risk_score:.2f}, {risk_band})")
    if brent is not None:
        evidence_bits.append(f"Brent is ${brent.price_usd_per_bbl:.2f}/bbl")
    if wti is not None:
        evidence_bits.append(f"WTI is ${wti.price_usd_per_bbl:.2f}/bbl")

    steps.append(CausalStep(
        step_no=step_no,
        claim=(
            "Live evidence check: " + "; ".join(evidence_bits) + ". "
            "This snapshot is used as the factual baseline before inferring India-specific disruption."
        ),
        mechanism="evidence_check",
        evidence_refs=["ais_stream", "digital_twin_risk", "alpha_vantage_prices"],
    ))
    step_no += 1

    # ------------------------------------------------------------------
    # Step 2: Chokepoint risk — explain in barrels/day, not tech IDs
    # ------------------------------------------------------------------
    for cp_id in affected.chokepoints:
        cp_obj = twin.chokepoint_by_id(cp_id)
        if cp_obj is None:
            continue
        risk_label = (
            "CRITICAL" if cp_obj.risk_score >= 0.8
            else "HIGH" if cp_obj.risk_score >= 0.6
            else "ELEVATED" if cp_obj.risk_score >= 0.4
            else "MODERATE"
        )
        throughput = cp_obj.throughput_mbd
        steps.append(CausalStep(
            step_no=step_no,
            claim=(
                f"The {cp_obj.name} — a critical maritime choke point through which roughly "
                f"{int(throughput)} million barrels of oil pass every day — is now rated {risk_label} risk. "
                f"Any slowdown here forces tankers to reroute via longer, costlier alternatives, "
                f"adding 7–15 extra sailing days."
            ),
            entity_kind="chokepoint",
            entity_id=cp_id,
            entity_name=cp_obj.name,
            mechanism="elevates_risk",
        ))
        step_no += 1

    # ------------------------------------------------------------------
    # Step 3: Shipping route impact — costs, not IDs
    # ------------------------------------------------------------------
    shown_routes = 0
    for route_id in affected.routes:
        if shown_routes >= 2:
            break
        route = next((r for r in twin.routes if r.id == route_id), None)
        if route is None:
            continue
        orig_port = twin.port_by_id(route.origin_port_id)
        dest_port = twin.port_by_id(route.destination_port_id)
        if not orig_port or not dest_port:
            continue
        orig_country = next((c.name for c in twin.countries if c.iso3 == orig_port.country_iso3), orig_port.country_iso3)
        dest_country = next((c.name for c in twin.countries if c.iso3 == dest_port.country_iso3), dest_port.country_iso3)
        ins = route.insurance_premium_multiplier
        transit = route.transit_days
        ins_note = f"insurance premiums are now {ins:.1f}× the normal rate" if ins > 1.1 else "insurance costs are rising"
        steps.append(CausalStep(
            step_no=step_no,
            claim=(
                f"Tankers sailing from {orig_country} to {dest_country} are directly affected: "
                f"{ins_note}, and the voyage takes {transit} days. Shipowners may delay departures "
                f"or demand higher freight rates — both of which raise the price paid by Indian refiners."
            ),
            entity_kind="route",
            entity_id=route_id,
            entity_name=f"{orig_country} → {dest_country}",
            mechanism="raises_freight_cost",
        ))
        step_no += 1
        shown_routes += 1

    # ------------------------------------------------------------------
    # Step 4: Crude grade supply — what India actually buys
    # ------------------------------------------------------------------
    shown_grades = 0
    for grade_id in affected.grades:
        if shown_grades >= 2:
            break
        grade = twin.grade_by_id(grade_id)
        if grade is None:
            continue
        src_country = next((c.name for c in twin.countries if c.iso3 == grade.source_country_iso3), grade.source_country_iso3)
        latest_price = twin.latest_price(grade_id)
        price_note = f"(currently ~${latest_price.price_usd_per_bbl:.2f}/bbl)" if latest_price is not None else ""
        steps.append(CausalStep(
            step_no=step_no,
            claim=(
                f"{grade.name} crude from {src_country} {price_note} is one of India's key feedstocks. "
                f"As tankers avoid or slow through this corridor, deliveries of this grade are delayed — "
                f"Indian refineries dependent on it must either find alternatives quickly or cut processing rates."
            ),
            entity_kind="grade",
            entity_id=grade_id,
            entity_name=grade.name,
            mechanism="delays_supply",
        ))
        step_no += 1
        shown_grades += 1

    # ------------------------------------------------------------------
    # Step 5: Indian refinery exposure — real names, real capacity
    # ------------------------------------------------------------------
    total_at_risk_kbd = 0
    shown_refineries = 0
    indian_refineries = [
        twin.refinery_by_id(rid) for rid in affected.refineries
        if twin.refinery_by_id(rid) is not None and twin.refinery_by_id(rid).country_iso3 == "IND"
    ]
    for ref in indian_refineries:
        if ref is None:
            continue
        total_at_risk_kbd += ref.capacity_kbd
        if shown_refineries < 2:
            util_note = f"currently running at {int(ref.utilization_pct * 100)}% capacity" if ref.utilization_pct else "at full capacity"
            steps.append(CausalStep(
                step_no=step_no,
                claim=(
                    f"{ref.name} (run by {ref.operator}, {ref.capacity_kbd} kbd processing capacity, "
                    f"{util_note}) relies on crude flowing through this corridor. "
                    f"A sustained disruption of 7–10 days would exhaust its buffer stocks and force "
                    f"a production slowdown — directly reducing India's fuel output."
                ),
                entity_kind="refinery",
                entity_id=ref.id,
                entity_name=ref.name,
                mechanism="feedstock_risk",
            ))
            step_no += 1
        shown_refineries += 1

    # ------------------------------------------------------------------
    # Step 6: India-level economic outcome
    # ------------------------------------------------------------------
    if "IND" in affected.countries:
        capacity_note = (
            f"With {total_at_risk_kbd:,} kbd of Indian refining capacity in the risk zone"
            if total_at_risk_kbd > 0
            else "With multiple Indian refineries exposed"
        )
        steps.append(CausalStep(
            step_no=step_no,
            claim=(
                f"{capacity_note}, India faces a higher crude import bill, potential fuel price increases "
                f"at the pump, and pressure on the rupee from the current account. "
                f"The government may need to activate its Strategic Petroleum Reserve (SPR) "
                f"or rapidly secure alternative crude shipments from non-affected suppliers."
            ),
            entity_kind="country",
            entity_id="IND",
            entity_name="India",
            mechanism="raises_import_bill",
        ))
        step_no += 1
    else:
        steps.append(CausalStep(
            step_no=step_no,
            claim=(
                "At this moment, no direct hit is detected on India's core import routes or refinery feedstock chains. "
                "For a newsroom or operations desk, this should be classified as a monitor signal, not an immediate India supply emergency."
            ),
            entity_kind="country",
            entity_id="IND",
            entity_name="India",
            mechanism="low_direct_linkage",
        ))
        step_no += 1

    # ------------------------------------------------------------------
    # Step 7: SPR trigger — explain what it is
    # ------------------------------------------------------------------
    for spr_id in affected.spr_sites[:1]:
        spr = next((s for s in twin.spr_sites if s.id == spr_id), None)
        if spr:
            steps.append(CausalStep(
                step_no=step_no,
                claim=(
                    f"India's Strategic Petroleum Reserve at {spr.name} — holding {spr.capacity_mbbl} million "
                    f"barrels of emergency crude — becomes a critical backstop. "
                    f"If disruption extends beyond ~10 days, the government could authorize a drawdown "
                    f"to prevent refinery shutdowns and fuel shortages."
                ),
                entity_kind="spr_site",
                entity_id=spr_id,
                entity_name=spr.name,
                mechanism="drawdown_candidate",
            ))
            step_no += 1

    # Append any additional LLM reasoning steps that weren't already captured
    existing_claims = {s.claim.strip().lower() for s in steps}
    _BOILERPLATE = (
        "detected event type", "target domain impacted",
        "historical analogs suggest", "structured_event:",
    )
    for raw in raw_reasoning_steps or []:
        if not raw:
            continue
        raw_stripped = raw.strip()
        raw_lower = raw_stripped.lower()
        # Skip boilerplate phrases and internal ID strings
        if any(bp in raw_lower for bp in _BOILERPLATE):
            continue
        if raw_stripped.startswith("[") or raw_lower in existing_claims:
            continue
        steps.append(CausalStep(step_no=step_no, claim=raw_stripped))
        step_no += 1

    if hypothesis_text and len(steps) <= 1:
        steps.append(CausalStep(step_no=step_no, claim=hypothesis_text))

    return steps


def build_causal_chain(
    event: StructuredEventRecord,
    twin: DigitalTwinState,
    *,
    hypothesis_text: str | None = None,
    confidence: float | None = None,
    raw_reasoning_steps: list[str] | None = None,
    source: Literal["llm", "deterministic", "hybrid"] = "deterministic",
) -> CausalChain:
    """Assemble a full `CausalChain` from event + twin (+ optional LLM output)."""
    affected = detect_affected_entities(event, twin, hypothesis_text)
    conf = 0.6 if confidence is None else max(0.0, min(1.0, float(confidence)))
    text = hypothesis_text or (
        f"{event.action_type or 'signal'} touching {event.target or 'supply chain'} "
        f"is likely to disturb Indian crude flows."
    )
    steps = _default_steps(event, twin, affected, text, raw_reasoning_steps)
    summary_bits: list[str] = []
    if affected.chokepoints:
        summary_bits.append(
            f"chokepoints={[_friendly('chokepoint', c, twin) for c in affected.chokepoints]}"
        )
    if affected.refineries:
        summary_bits.append(
            f"refineries={[_friendly('refinery', r, twin) for r in affected.refineries[:3]]}"
        )
    if affected.grades:
        summary_bits.append(
            f"grades={[_friendly('grade', g, twin) for g in affected.grades[:3]]}"
        )
    summary = "; ".join(summary_bits) or "no direct twin entities matched — narrative only."

    return CausalChain(
        hypothesis_text=text,
        confidence=conf,
        summary=summary,
        steps=steps,
        affected=affected,
        twin_branch_id=twin.branch_id,
        source=source if raw_reasoning_steps else "deterministic",
    )


def build_chain_from_hypothesis(
    hypothesis: HypothesisRecord,
    event: StructuredEventRecord,
    twin: DigitalTwinState,
) -> CausalChain:
    """Convenience helper used by orchestration to re-derive a chain from a
    persisted hypothesis when it wasn't stored with structured causality.
    """
    return build_causal_chain(
        event,
        twin,
        hypothesis_text=hypothesis.hypothesis_text,
        confidence=hypothesis.confidence,
        raw_reasoning_steps=hypothesis.reasoning_chain,
        source="hybrid",
    )


# ---------------------------------------------------------------------------
# Enforcement helpers for recommendations
# ---------------------------------------------------------------------------


REASONING_MISSING_ERROR = "Recommendation payload missing explainable reasoning_chain"


def attach_to_recommendation(
    recommendation: RecommendationInput,
    chain: CausalChain,
) -> RecommendationInput:
    """Return a new `RecommendationInput` whose payload embeds the chain.

    Adds two keys under `recommendation_payload`:
        - reasoning_chain: list[str]   short bullet form (for narrow UI)
        - causal_chain: dict           full structured chain (for rich UI)
    """
    payload = dict(recommendation.recommendation_payload or {})
    payload["reasoning_chain"] = chain.as_bullet_lines()
    payload["causal_chain"] = chain.model_dump(mode="json")
    return replace(recommendation, recommendation_payload=payload)


def assert_recommendation_explained(recommendation: RecommendationInput) -> None:
    payload = recommendation.recommendation_payload or {}
    chain = payload.get("reasoning_chain")
    if not isinstance(chain, list) or not chain:
        raise ValueError(REASONING_MISSING_ERROR)


def attach_and_enforce(
    recommendations: Iterable[RecommendationInput],
    chain: CausalChain,
) -> list[RecommendationInput]:
    """Attach chain to every recommendation and enforce presence — one pass."""
    out: list[RecommendationInput] = []
    for rec in recommendations:
        enriched = attach_to_recommendation(rec, chain)
        assert_recommendation_explained(enriched)
        out.append(enriched)
    return out


__all__ = [
    "CausalChain",
    "CausalStep",
    "AffectedEntities",
    "build_causal_chain",
    "build_chain_from_hypothesis",
    "detect_affected_entities",
    "attach_to_recommendation",
    "attach_and_enforce",
    "assert_recommendation_explained",
    "REASONING_MISSING_ERROR",
]
