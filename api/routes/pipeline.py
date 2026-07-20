from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timedelta, timezone
from time import perf_counter
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import require_api_key
from api.pipeline_store import load_latest_pipeline_state, load_pipeline_state, save_pipeline_state
from api.schemas import (
    PipelineDetailsResponse,
    PipelineStateResponse,
    RecommendationDetails,
    RedTeamDetails,
    SimulationDetails,
    TriggerPipelineRequest,
    HypothesisDetails,
)
from agents.reasoning_chain import build_chain_from_hypothesis
from digital_twin import build_digital_twin
from ingestion.storage import (
    RawSignalRecord,
    StructuredEventRow,
    append_raw_signals,
    append_structured_events,
    fetch_live_market_context,
    fetch_hypothesis_by_structured_event_id,
    fetch_hypothesis_review_by_hypothesis_id,
    fetch_recommendations_by_type,
    fetch_simulations_by_hypothesis_id,
    fetch_structured_event_by_id,
)
from orchestration.graph import run_pipeline_for_structured_event
from sqlalchemy import text, update as sa_update


router = APIRouter(prefix="/pipeline", tags=["pipeline"])


def _ops_friendly_hypothesis_text(text: str | None, event_target: str | None, chain: dict | None) -> str:
    raw = (text or "").strip()
    target = (event_target or "").lower()
    is_legacy = " involving " in raw and " likely to affect " in raw and "raising procurement" in raw.lower()
    if not is_legacy:
        return raw or "Signal under assessment."

    affected = (chain or {}).get("affected") if isinstance(chain, dict) else {}
    has_india_routes = bool((affected or {}).get("routes") or (affected or {}).get("refineries") or (affected or {}).get("chokepoints"))
    if "u.s." in target and not has_india_routes:
        return (
            "This is a U.S. inventory-market signal. Direct impact on India's crude security is currently low; "
            "treat it as watchlist unless shipping corridors, freight, or supplier flows deteriorate."
        )
    if has_india_routes:
        return (
            "This signal may affect India-relevant crude logistics. Priority is to monitor shipping flow, freight cost, "
            "and refinery feedstock risk over the next 24-72 hours."
        )
    return (
        "Signal detected, but direct India supply-chain linkage is not yet confirmed. Keep under monitoring and "
        "escalate only with corroborating logistics disruption evidence."
    )


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_usd_million_from_billion(value_bn: float) -> str:
    amount_m = abs(float(value_bn) * 1000.0)
    if amount_m >= 100:
        shown = f"{amount_m:.0f}"
    elif amount_m >= 10:
        shown = f"{amount_m:.1f}"
    else:
        shown = f"{amount_m:.2f}"
    sign = "+" if float(value_bn) >= 0 else "-"
    return f"{sign}${shown}M"


def _build_data_fused_chain(
    *,
    base_chain: dict | None,
    event: object | None,
    twin: object | None,
    market_ctx: dict,
    simulations: list[SimulationDetails],
    economic: list[RecommendationDetails],
    procurement: list[RecommendationDetails],
    policy: list[RecommendationDetails],
) -> dict | None:
    """Assemble a semantic chain and clearly separate observed vs derived vs predicted."""
    if not isinstance(base_chain, dict):
        return base_chain

    affected = base_chain.get("affected") if isinstance(base_chain.get("affected"), dict) else {}
    source = str(base_chain.get("source") or "hybrid")
    twin_branch_id = base_chain.get("twin_branch_id")

    action = (getattr(event, "action_type", None) or "signal").replace("_", " ").strip()
    target = (getattr(event, "target", None) or "energy market").replace("_", " ").strip()
    actors = [str(a) for a in (getattr(event, "actors", None) or []) if str(a).strip()]
    payload = getattr(event, "extracted_payload", None)
    payload = payload if isinstance(payload, dict) else {}
    title = str(payload.get("headline") or payload.get("title") or payload.get("webTitle") or "").strip()

    recent_headlines = market_ctx.get("recent_headlines") if isinstance(market_ctx.get("recent_headlines"), list) else []
    eia_note = str(market_ctx.get("eia_note") or "").strip()

    text_blob = " ".join([
        action.lower(),
        target.lower(),
        " ".join(a.lower() for a in actors),
        title.lower(),
        " ".join(str(h).lower() for h in recent_headlines[:4]),
    ])

    security_keywords = ("conflict", "missile", "attack", "strike", "houthi", "yemen", "iran", "war", "blockade", "navy")
    weather_keywords = ("storm", "cyclone", "weather", "wave", "wind", "hurricane", "typhoon", "flood")
    shipping_keywords = ("hormuz", "red sea", "bab-el", "suez", "strait", "tanker", "shipping", "route", "port")
    inventory_keywords = ("inventory", "stocks", "distillate", "draw", "build", "eia", "fred")

    has_security_signal = any(k in text_blob for k in security_keywords)
    has_weather_signal = any(k in text_blob for k in weather_keywords)
    has_shipping_text = any(k in text_blob for k in shipping_keywords)
    has_inventory_signal = any(k in text_blob for k in inventory_keywords)

    tankers = list(getattr(twin, "tankers", []) or []) if twin is not None else []
    chokepoints = list(getattr(twin, "chokepoints", []) or []) if twin is not None else []
    ports = list(getattr(twin, "ports", []) or []) if twin is not None else []
    refineries = list(getattr(twin, "refineries", []) or []) if twin is not None else []

    live_tankers = [t for t in tankers if getattr(t, "lat", None) is not None and getattr(t, "lon", None) is not None]
    anchored = sum(1 for t in live_tankers if str(getattr(t, "status", "")) == "anchored")
    laden = sum(1 for t in live_tankers if str(getattr(t, "status", "")) == "laden")
    anchored_ratio = (anchored / len(live_tankers)) if live_tankers else 0.0

    top_cp = max(chokepoints, key=lambda c: float(getattr(c, "risk_score", 0.0) or 0.0), default=None)
    top_cp_name = str(getattr(top_cp, "name", "n/a")) if top_cp is not None else "n/a"
    top_cp_risk = float(getattr(top_cp, "risk_score", 0.0) or 0.0) if top_cp is not None else 0.0
    top_cp_status = str(getattr(top_cp, "status", "open")) if top_cp is not None else "open"

    high_cong = [p for p in ports if float(getattr(p, "congestion_pct", 0.0) or 0.0) >= 60.0]
    restricted_cp = [c for c in chokepoints if str(getattr(c, "status", "open")) in {"restricted", "closed"}]

    route_count = len((affected or {}).get("routes") or [])
    chokepoint_count = len((affected or {}).get("chokepoints") or [])
    refinery_ids = (affected or {}).get("refineries") or []
    ind_refineries = [
        r for r in refineries
        if getattr(r, "id", None) in refinery_ids and getattr(r, "country_iso3", None) == "IND"
    ]
    at_risk_kbd = sum(float(getattr(r, "capacity_kbd", 0.0) or 0.0) for r in ind_refineries)

    is_shipping_causal = bool(
        has_shipping_text
        or has_security_signal
        or has_weather_signal
        or route_count > 0
        or chokepoint_count > 0
    )

    brent = _to_float(market_ctx.get("brent_usd"))
    wti = _to_float(market_ctx.get("wti_usd"))
    if brent is None and twin is not None and hasattr(twin, "latest_price"):
        p = twin.latest_price("grade_brent")
        brent = _to_float(getattr(p, "price_usd_per_bbl", None)) if p is not None else None
    if wti is None and twin is not None and hasattr(twin, "latest_price"):
        p = twin.latest_price("grade_wti")
        wti = _to_float(getattr(p, "price_usd_per_bbl", None)) if p is not None else None

    sim_prob = None
    sim_shock = None
    sim_duration = None
    if simulations:
        p = simulations[0].percentiles or {}
        sim_prob = _to_float(p.get("disruption_prob"))
        sim_shock = _to_float(p.get("price_shock_pct"))
        sim_duration = _to_float(p.get("duration_days"))

    econ_bill = None
    econ_cpi = None
    econ_cad = None
    if economic:
        ep = economic[0].recommendation_payload if isinstance(economic[0].recommendation_payload, dict) else {}
        em = ep.get("economic_impact", {}) if isinstance(ep.get("economic_impact"), dict) else {}
        econ_bill = _to_float(em.get("import_bill_delta_usd_bn"))
        econ_cpi = _to_float(em.get("cpi_delta_pct"))
        econ_cad = _to_float(em.get("cad_delta_pct_of_gdp"))

    proc_demand = None
    proc_secured = None
    proc_gap = None
    if procurement:
        pp = procurement[0].recommendation_payload if isinstance(procurement[0].recommendation_payload, dict) else {}
        pr = pp.get("procurement", {}) if isinstance(pp.get("procurement"), dict) else {}
        proc_demand = _to_float(pr.get("demand_kbd"))
        proc_secured = _to_float(pr.get("secured_kbd"))
        proc_gap = _to_float(pr.get("gap_kbd"))

    spr_draw = None
    total_draw = None
    if policy:
        pl = policy[0].recommendation_payload if isinstance(policy[0].recommendation_payload, dict) else {}
        po = pl.get("policy", {}) if isinstance(pl.get("policy"), dict) else {}
        spr_draw = _to_float(po.get("recommended_spr_draw_mbd_day1"))
        total_draw = _to_float(po.get("total_draw_million_barrels"))

    steps: list[dict] = []
    step_no = 1

    def add_step(
        mechanism: str,
        claim: str,
        *,
        evidence_type: str,
        evidence_refs: list[str],
        source_labels: list[str] | None = None,
        entity_kind: str | None = None,
        entity_id: str | None = None,
        entity_name: str | None = None,
    ) -> None:
        nonlocal step_no
        step: dict = {
            "step_no": step_no,
            "claim": claim,
            "mechanism": mechanism,
            "evidence_type": evidence_type,
            "evidence_refs": evidence_refs,
            "source_labels": source_labels or [],
        }
        if entity_kind:
            step["entity_kind"] = entity_kind
        if entity_id:
            step["entity_id"] = entity_id
        if entity_name:
            step["entity_name"] = entity_name
        steps.append(step)
        step_no += 1

    add_step(
        "event_trigger",
        (
            f"Observed trigger: {action} on {target}. "
            + (f"Actors: {', '.join(actors[:3])}. " if actors else "")
            + (f"Headline: {title[:150]}." if title else "")
        ).strip(),
        evidence_type="observed",
        evidence_refs=["structured_event", "news_feeds"],
        source_labels=["Structured Event", "Guardian/NewsAPI/GDELT"],
    )

    # For non-shipping events (e.g. inventory), use financial transmission chain.
    if has_inventory_signal and not is_shipping_causal:
        add_step(
            "market_expectation",
            "Derived: inventory/stock signal implies tighter expected supply balance in oil benchmarks.",
            evidence_type="derived",
            evidence_refs=["event_semantics"],
            source_labels=["Rule Engine"],
        )

        price_bits: list[str] = []
        if brent is not None:
            price_bits.append(f"Brent observed at ${brent:.2f}/bbl")
        if wti is not None:
            price_bits.append(f"WTI observed at ${wti:.2f}/bbl")
        if eia_note:
            price_bits.append(f"macro series {eia_note[:90]}")
        if sim_shock is not None:
            price_bits.append(f"scenario model median Brent shock +{sim_shock * 100:.1f}% (prediction)")
        add_step(
            "price_transmission",
            "Observed/predicted price channel: " + ("; ".join(price_bits) if price_bits else "insufficient live quote data"),
            evidence_type="predicted" if sim_shock is not None else "observed",
            evidence_refs=["market_context", "simulation_percentiles"],
            source_labels=["Alpha Vantage", "Scenario Model"],
            entity_kind="commodity",
            entity_name="Crude basket",
        )

        add_step(
            "maritime_channel_check",
            (
                f"Observed maritime check: {len(live_tankers)} AIS vessels active; {top_cp_name} risk {top_cp_risk:.2f} ({top_cp_status}). "
                "This event is currently not treated as shipping-driven."
            ),
            evidence_type="observed",
            evidence_refs=["ais_stream", "digital_twin_chokepoints"],
            source_labels=["AIS Stream", "Digital Twin"],
        )
    else:
        weather_stress = min(1.0, 0.12 * len(high_cong) + 0.18 * len(restricted_cp))
        security_stress = 0.45 if has_security_signal else 0.0
        maritime_stress = min(1.0, 0.55 * top_cp_risk + 0.30 * anchored_ratio + 0.15 * (1.0 if top_cp_status != "open" else 0.0))

        if has_weather_signal or weather_stress > 0.0:
            add_step(
                "weather_branch",
                f"Observed weather/ops branch: high-congestion ports={len(high_cong)}, restricted chokepoints={len(restricted_cp)}.",
                evidence_type="observed",
                evidence_refs=["port_congestion", "chokepoint_status"],
                source_labels=["OpenWeather/Stormglass", "Digital Twin"],
            )

        if has_security_signal:
            add_step(
                "security_branch",
                "Observed security branch: conflict-risk indicators present in event/news context.",
                evidence_type="observed",
                evidence_refs=["news_feeds", "event_keywords"],
                source_labels=["News Feeds"],
            )

        add_step(
            "maritime_behavior",
            f"Observed maritime branch: AIS active={len(live_tankers)}, anchored={anchored}, laden={laden}; top corridor {top_cp_name} risk {top_cp_risk:.2f} ({top_cp_status}).",
            evidence_type="observed",
            evidence_refs=["ais_stream", "digital_twin_chokepoints"],
            source_labels=["AIS Stream", "Digital Twin"],
        )

        throughput_stress = min(1.0, 0.60 * maritime_stress + 0.20 * weather_stress + 0.20 * security_stress)
        add_step(
            "throughput_merge",
            (
                "Derived throughput stress using rule engine: "
                "0.60×Maritime + 0.20×Weather + 0.20×Security "
                f"= 0.60×{maritime_stress:.2f} + 0.20×{weather_stress:.2f} + 0.20×{security_stress:.2f} "
                f"= {throughput_stress:.2f}."
            ),
            evidence_type="derived",
            evidence_refs=["risk_fusion"],
            source_labels=["Rule Engine"],
        )

        price_bits: list[str] = []
        if brent is not None:
            price_bits.append(f"Brent ${brent:.2f}/bbl (observed)")
        if wti is not None:
            price_bits.append(f"WTI ${wti:.2f}/bbl (observed)")
        if sim_shock is not None:
            price_bits.append(f"Scenario model median Brent shock +{sim_shock * 100:.1f}% (prediction)")
        if not price_bits:
            price_bits.append("live oil-price quote unavailable in current pull")
        add_step(
            "price_transmission",
            "Throughput stress transmits to commodity pricing: " + "; ".join(price_bits) + ".",
            evidence_type="predicted" if sim_shock is not None else "observed",
            evidence_refs=["market_context", "simulation_percentiles"],
            source_labels=["Alpha Vantage", "Scenario Model"],
            entity_kind="commodity",
            entity_name="Crude basket",
        )

    add_step(
        "india_exposure",
        (
            f"Observed logistics exposure: routes={route_count}, Indian refineries={len(ind_refineries)}, capacity_in_scope={at_risk_kbd:.0f} kbd. "
            + (
                "Predicted macro exposure follows global price benchmark transmission, not physical route outage in this case."
                if route_count == 0 and len(ind_refineries) == 0
                else "Predicted macro exposure includes both logistics and benchmark-price channels."
            )
        ),
        evidence_type="derived",
        evidence_refs=["entity_mapping", "digital_twin_refineries"],
        source_labels=["Digital Twin", "Rule Engine"],
        entity_kind="country",
        entity_id="IND",
        entity_name="India",
    )

    macro_parts: list[str] = []
    if econ_bill is not None:
        macro_parts.append(f"import-bill delta {_fmt_usd_million_from_billion(econ_bill)}")
    if econ_cpi is not None:
        macro_parts.append(f"CPI impact +{econ_cpi:.3f}%")
    if econ_cad is not None:
        macro_parts.append(f"CAD impact +{econ_cad:.4f}% GDP")
    if sim_prob is not None:
        macro_parts.append(f"estimated disruption probability {sim_prob * 100:.1f}%")
    if sim_duration is not None:
        macro_parts.append(f"stress window {sim_duration:.1f} days")
    if macro_parts:
        add_step(
            "macro_impact",
            "Predicted macro outcomes: " + "; ".join(macro_parts) + ".",
            evidence_type="predicted",
            evidence_refs=["economic_agent_output", "simulation_percentiles"],
            source_labels=["Economic Model", "Scenario Model"],
            entity_kind="country",
            entity_id="IND",
            entity_name="India",
        )

    action_parts: list[str] = []
    if proc_demand is not None and proc_secured is not None:
        action_parts.append(f"increase inventory buffer if coverage drops below {proc_secured:.0f}/{proc_demand:.0f} kbd")
    if proc_gap is not None and proc_gap > 0:
        action_parts.append(f"switch suppliers to close {proc_gap:.0f} kbd gap")
        action_parts.append("hedge near-month futures for uncovered barrels")
    else:
        action_parts.append("keep supplier mix ready for rapid switch if risk worsens")
    if spr_draw is not None and spr_draw > 0:
        action_parts.append(f"activate SPR draw at {spr_draw:.3f} mbd")
    elif total_draw is not None:
        action_parts.append(f"SPR draw currently not triggered (planned {total_draw:.2f} mbbl)")
    add_step(
        "recommendation",
        "Recommended actions: " + "; ".join(action_parts) + ".",
        evidence_type="predicted",
        evidence_refs=["procurement_plan", "policy_plan"],
        source_labels=["Procurement Agent", "Policy Agent"],
        entity_kind="policy",
        entity_name="India response",
    )

    confidence_label = "low"
    if sim_prob is not None:
        if sim_prob >= 0.6:
            confidence_label = "high"
        elif sim_prob >= 0.35:
            confidence_label = "medium"

    hyp_text = (
        f"Observed signal: {action} on {target}. "
        "Derived interpretation: pressure transmits primarily through benchmark-price and demand-balance channels"
        + (" with a logistics channel also active" if is_shipping_causal else " while logistics channel remains weak")
        + f". Predicted India impact confidence: {confidence_label}."
    )

    summary_parts = [
        f"observed_steps={sum(1 for s in steps if s.get('evidence_type') == 'observed')}",
        f"derived_steps={sum(1 for s in steps if s.get('evidence_type') == 'derived')}",
        f"predicted_steps={sum(1 for s in steps if s.get('evidence_type') == 'predicted')}",
        f"shipping_channel={'on' if is_shipping_causal else 'off'}",
    ]
    if sim_prob is not None:
        summary_parts.append(f"prob={sim_prob * 100:.1f}%")

    return {
        "hypothesis_text": hyp_text,
        "confidence": base_chain.get("confidence"),
        "summary": "; ".join(summary_parts),
        "steps": steps,
        "affected": affected,
        "twin_branch_id": twin_branch_id,
        "source": source,
    }

# ---------------------------------------------------------------------------
# Refresh: ingest live news → extract → run pipeline on newest event
# ---------------------------------------------------------------------------

# Broad set of queries covering Hormuz, India, global oil geopolitics
_INGEST_QUERIES = [
    "Iran oil sanctions tanker Hormuz 2026",
    "India crude oil import energy security 2026",
    "OPEC oil production cut supply 2026",
    "Red Sea Houthi shipping attack tanker",
    "Russia oil embargo sanctions India 2026",
    "Saudi Arabia oil price supply 2026",
    "Strait of Hormuz shipping disruption risk",
]


def _normalize_mission_objective(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return "balanced_resilience"
    if raw in {"minimize_import_cost", "cost", "optimize_cost", "minimize cost"}:
        return "minimize_import_cost"
    if raw in {"maximize_supply_resilience", "resilience", "security", "maximize resilience"}:
        return "maximize_supply_resilience"
    if raw in {"maintain_import_coverage", "coverage", "maintain coverage"}:
        return "maintain_import_coverage"
    return "balanced_resilience"

_REFRESH_LOCK = threading.Lock()


def _ingest_news_parallel(database_url: str, guardian_key: str, newsapi_key: str, quick: bool) -> tuple[int, list[str]]:
    errors: list[str] = []
    page_size = 6 if quick else 10
    source_timeout = 12 if quick else 15
    # Deterministic rotating query slice (time-bucketed) to avoid random event
    # drift while still broadening coverage on each refresh.
    bucket = int(datetime.now(timezone.utc).timestamp() // 900)
    query_count = 2 if quick else 3
    query_batch = [
        _INGEST_QUERIES[(bucket + i) % len(_INGEST_QUERIES)]
        for i in range(query_count)
    ]

    jobs = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        if guardian_key:
            def _guardian_job(query: str) -> int:
                from ingestion.connectors import guardian as guardian_connector
                sigs = guardian_connector.fetch(
                    api_key=guardian_key,
                    query=query,
                    page_size=page_size,
                    section="business",
                    timeout=source_timeout,
                )
                rel = [s for s in sigs if sum(1 for t in _SIGNIFICANCE_TERMS if t in _signal_text(getattr(s, "raw_payload", {}))) >= 1]
                return append_raw_signals(database_url, rel)

            for q in query_batch:
                jobs.append((f"guardian[{q}]", pool.submit(_guardian_job, q)))

        if newsapi_key:
            def _newsapi_job(query: str) -> int:
                from ingestion.connectors import newsapi as newsapi_connector
                sigs = newsapi_connector.fetch(
                    api_key=newsapi_key,
                    query=query,
                    page_size=page_size,
                    timeout=source_timeout,
                )
                rel = [s for s in sigs if sum(1 for t in _SIGNIFICANCE_TERMS if t in _signal_text(getattr(s, "raw_payload", {}))) >= 1]
                return append_raw_signals(database_url, rel)

            for q in query_batch:
                jobs.append((f"newsapi[{q}]", pool.submit(_newsapi_job, q)))

        # Pull latest market context sources so freshness cards are not stuck
        # on old values when news is quiet.
        alpha_key = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
        if alpha_key:
            def _alpha_job() -> int:
                from ingestion.connectors import alpha_vantage

                sigs = alpha_vantage.fetch(api_key=alpha_key, grade="brent", interval="daily", timeout=8)
                sigs.extend(alpha_vantage.fetch(api_key=alpha_key, grade="wti", interval="daily", timeout=8))
                return append_raw_signals(database_url, sigs)

            jobs.append(("alpha_vantage", pool.submit(_alpha_job)))

        fred_key = os.getenv("FRED_API_KEY", "").strip()
        if fred_key:
            def _fred_job() -> int:
                from ingestion.connectors import fred

                sigs = fred.fetch(api_key=fred_key, series_id="DCOILBRENTEU", limit=5, timeout=8)
                sigs.extend(fred.fetch(api_key=fred_key, series_id="DEXINUS", limit=5, timeout=8))
                return append_raw_signals(database_url, sigs)

            jobs.append(("fred", pool.submit(_fred_job)))

        eia_key = os.getenv("EIA_API_KEY", "").strip()
        if eia_key:
            def _eia_job() -> int:
                from ingestion.connectors import eia

                sigs = eia.fetch(api_key=eia_key, length=4, timeout=8)
                return append_raw_signals(database_url, sigs)

            jobs.append(("eia", pool.submit(_eia_job)))

        ingested_new = 0
        for source, fut in jobs:
            try:
                ingested_new += int(fut.result())
            except Exception as exc:
                errors.append(f"{source}: {exc}")

    # AIS stream pull (short burst) so map vessel layer can update on refresh.
    ais_key = os.getenv("AIS_STREAM_API_KEY", "").strip()
    if ais_key:
        try:
            from ingestion.connectors import ais_stream as ais_stream_connector

            ais_signals = ais_stream_connector.fetch(
                api_key=ais_key,
                max_messages=8 if quick else 16,
                timeout_seconds=5 if quick else 8,
            )
            ingested_new += append_raw_signals(database_url, ais_signals)
        except Exception as exc:
            errors.append(f"ais_stream: {exc}")

    return ingested_new, errors


_SIGNIFICANCE_TERMS = {
    "hormuz",
    "suez",
    "bab-el-mandeb",
    "bab el mandeb",
    "strait",
    "shipping",
    "tanker",
    "freight",
    "opec",
    "sanction",
    "embargo",
    "crude",
    "brent",
    "wti",
    "inventory",
    "stock",
    "refinery",
    "import",
    "diesel",
    "distillate",
}


def _signal_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return str(payload or "").lower()
    parts: list[str] = []
    for key in ("title", "webTitle", "description", "content", "caption", "headline", "url", "webUrl"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    if not parts:
        parts.append(str(payload))
    return " ".join(parts).lower()


def _extract_entities(text: str, hints: list[str] | None) -> list[str]:
    out = [str(x).strip() for x in (hints or []) if str(x).strip()]
    known = [
        "India",
        "U.S.",
        "Iran",
        "Saudi Arabia",
        "Iraq",
        "Russia",
        "UAE",
        "OPEC",
        "Strait of Hormuz",
        "Suez Canal",
        "Bab-el-Mandeb",
        "Red Sea",
    ]
    low = text.lower()
    for item in known:
        if item.lower() in low and item not in out:
            out.append(item)
    # Keep only a small, stable list for explainability chips.
    return out[:8]


def _importance_score(source: str, action_type: str, confidence: float, text: str) -> float:
    source_boost = {
        "guardian": 0.34,
        "newsapi": 0.32,
        "gdelt_doc": 0.27,
        "gdelt": 0.24,
        "ais_stream": 0.16,
        "alpha_vantage_prices": 0.10,
        "fred": 0.08,
        "eia": 0.10,
    }.get(source, 0.05)

    action = str(action_type or "").lower()
    action_boost = {
        "supply_disruption": 0.35,
        "sanctions": 0.30,
        "price_shock": 0.22,
        "procurement_shift": 0.18,
        "signal_watch": 0.02,
        "information_unavailable": -0.08,
    }.get(action, 0.04)

    keyword_hits = sum(1 for term in _SIGNIFICANCE_TERMS if term in text)
    keyword_score = min(0.36, 0.07 * keyword_hits)
    conf_score = max(0.0, min(1.0, confidence)) * 0.25
    score = source_boost + action_boost + keyword_score + conf_score
    return round(max(0.0, min(1.0, score)), 4)


def _extract_structured_events(database_url: str, gemini_key: str, quick: bool) -> tuple[int, list[str]]:
    errors: list[str] = []
    extracted_new = 0
    max_rows = 10 if quick else 36
    max_extract_seconds = 5 if quick else 10
    try:
        from processing.extraction.deterministic_extractor import extract_structured_event_deterministic
        from processing.extraction.gemini_extractor import GeminiConfig, extract_structured_event_gemini
        from ingestion.storage import get_engine
        import json
        from sqlalchemy import text as sql_text

        use_gemini = bool(gemini_key and not quick)
        cfg = GeminiConfig(api_key=gemini_key, model="gemini-2.5-flash", timeout_seconds=4) if use_gemini else None
        eng = get_engine(database_url)
        with eng.connect() as conn:
            rows = conn.execute(sql_text(f"""
                SELECT rs.id, rs.source, rs.source_id, rs.signal_ts,
                       rs.entities_hint, rs.raw_payload,
                       se.id AS current_event_id,
                       lower(COALESCE(se.action_type,'')) AS current_action_type
                FROM raw_signals rs
                LEFT JOIN structured_events se ON se.raw_signal_id = rs.id
                WHERE rs.source IN ('guardian', 'newsapi', 'gdelt_doc', 'gdelt', 'ais_stream', 'alpha_vantage_prices', 'fred', 'eia')
                AND (
                    se.raw_signal_id IS NULL
                    OR (
                        rs.source IN ('guardian', 'newsapi', 'gdelt_doc', 'gdelt')
                        AND lower(COALESCE(se.action_type,'')) IN ('signal_watch', 'information_unavailable')
                    )
                )
                ORDER BY
                    CASE
                        WHEN rs.source IN ('guardian', 'newsapi', 'gdelt_doc', 'gdelt') THEN 0
                        WHEN rs.source = 'ais_stream' THEN 1
                        ELSE 2
                    END,
                    rs.signal_ts DESC,
                    rs.id DESC
                LIMIT {max_rows}
            """)).all()

        started = perf_counter()
        for row in rows:
            if perf_counter() - started > max_extract_seconds:
                break
            try:
                payload = json.loads(row.raw_payload) if isinstance(row.raw_payload, str) else row.raw_payload
                hints = json.loads(row.entities_hint) if isinstance(row.entities_hint, str) else row.entities_hint
                sig_ts = row.signal_ts
                if isinstance(sig_ts, str):
                    try:
                        sig_ts = datetime.fromisoformat(sig_ts.replace("Z", "+00:00"))
                    except Exception:
                        sig_ts = datetime.now(timezone.utc)
                if isinstance(sig_ts, datetime) and sig_ts.tzinfo is None:
                    sig_ts = sig_ts.replace(tzinfo=timezone.utc)
                sig = RawSignalRecord(
                    id=row.id,
                    source=row.source,
                    source_id=row.source_id,
                    signal_ts=sig_ts,
                    entities_hint=hints or [],
                    raw_payload=payload or {},
                )
                source = str(row.source or "")
                source_text = _signal_text(payload)

                # Skip stale raw signals during refresh extraction; they can be
                # backfilled offline but should not dominate interactive runs.
                if isinstance(sig.signal_ts, datetime):
                    ts = sig.signal_ts
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    max_age = timedelta(hours=72 if source in {"guardian", "newsapi", "gdelt_doc", "gdelt"} else 24)
                    if (datetime.now(timezone.utc) - ts) > max_age:
                        continue

                event = None
                if use_gemini and cfg is not None:
                    try:
                        event = extract_structured_event_gemini(sig, cfg)
                    except Exception:
                        event = None
                if event is None:
                    event = extract_structured_event_deterministic(sig)

                importance = _importance_score(source, str(event.action_type or ""), float(event.confidence or 0.0), source_text)
                min_score = 0.46 if source in {"guardian", "newsapi", "gdelt_doc", "gdelt"} else 0.62
                keyword_hits = sum(1 for term in _SIGNIFICANCE_TERMS if term in source_text)
                if source in {"guardian", "newsapi", "gdelt_doc", "gdelt"}:
                    if str(event.action_type or "").lower() in {"signal_watch", "information_unavailable"} and keyword_hits < 2:
                        continue
                if importance < min_score:
                    continue

                entities = _extract_entities(source_text, hints if isinstance(hints, list) else [])
                event.extracted_payload = {
                    **(event.extracted_payload or {}),
                    "importance_score": importance,
                    "significance_threshold": min_score,
                    "is_significant": True,
                    "entity_candidates": entities,
                    "classification": {
                        "source": source,
                        "action_type": event.action_type,
                    },
                }
                if row.current_event_id is not None:
                    with eng.begin() as wconn:
                        event_ts = event.event_ts
                        if event_ts.tzinfo is None:
                            event_ts = event_ts.replace(tzinfo=timezone.utc)
                        wconn.execute(
                            sa_update(StructuredEventRow)
                            .where(StructuredEventRow.id == int(row.current_event_id))
                            .values(
                                event_ts=event_ts.astimezone(timezone.utc),
                                action_type=event.action_type,
                                target=event.target,
                                confidence=event.confidence,
                                actors=event.actors or [],
                                extracted_payload=event.extracted_payload or {},
                            )
                        )
                    extracted_new += 1
                else:
                    extracted_new += append_structured_events(database_url, [event])
            except Exception:
                # Ignore per-record failures; a few bad records shouldn't block refresh.
                continue
    except Exception as exc:
        errors.append(f"extraction: {exc}")

    return extracted_new, errors


def _event_signature(database_url: str, event_id: int | None) -> tuple[str, str] | None:
    if event_id is None:
        return None
    from ingestion.storage import get_engine
    from sqlalchemy import text as sql_text

    eng = get_engine(database_url)
    with eng.connect() as conn:
        row = conn.execute(
            sql_text(
                "SELECT lower(COALESCE(se.action_type,'')) AS action, lower(COALESCE(se.target,'')) AS target "
                "FROM structured_events se WHERE se.id = :event_id LIMIT 1"
            ),
            {"event_id": int(event_id)},
        ).first()
    if row is None:
        return None
    return (str(row[0] or "").strip(), str(row[1] or "").strip())


def _pick_latest_event_id(
    database_url: str,
    exclude_event_id: int | None = None,
    exclude_signature: tuple[str, str] | None = None,
) -> tuple[int, bool, dict]:
    from ingestion.storage import get_engine
    from sqlalchemy import text as sql_text

    exclude_clause = "AND se.id <> :exclude_event_id " if exclude_event_id is not None else ""
    params = {"exclude_event_id": int(exclude_event_id)} if exclude_event_id is not None else {}
    exclude_sig_clause = ""
    if exclude_signature is not None:
        exclude_sig_clause = "AND NOT (lower(COALESCE(se.action_type,'')) = :exclude_action AND lower(COALESCE(se.target,'')) = :exclude_target) "
        params["exclude_action"] = str(exclude_signature[0] or "").strip().lower()
        params["exclude_target"] = str(exclude_signature[1] or "").strip().lower()

    live_news_sources = {"guardian", "newsapi", "gdelt_doc", "gdelt"}
    support_sources = {"ais_stream"}
    source_weight = {
        "guardian": 1.00,
        "newsapi": 0.98,
        "gdelt_doc": 0.92,
        "gdelt": 0.88,
        "ais_stream": 0.70,
        "alpha_vantage_prices": 0.72,
        "eia": 0.70,
        "fred": 0.66,
        "opensanctions": 0.55,
    }
    prefer_terms = ("hormuz", "suez", "bab", "strait", "shipping", "tanker", "oil", "refinery", "import")

    eng = get_engine(database_url)
    with eng.connect() as conn:
        rows = conn.execute(sql_text(
            "SELECT se.id, se.event_ts, lower(COALESCE(se.action_type,'')) AS action, "
            "lower(COALESCE(se.target,'')) AS target, COALESCE(se.confidence, 0) AS conf, "
            "lower(COALESCE(rs.source,'')) AS source "
            "FROM structured_events se "
            "LEFT JOIN raw_signals rs ON rs.id = se.raw_signal_id "
            "WHERE lower(COALESCE(se.target,'')) NOT LIKE '%example%' "
            "AND lower(COALESCE(CAST(se.actors AS TEXT),'')) NOT LIKE '%example%' "
            f"{exclude_clause}"
            "ORDER BY se.event_ts DESC, se.id DESC LIMIT 160"
        ), params).all()

    if not rows:
        raise ValueError("No structured events available yet — run ingestion and extraction")

    now = datetime.now(timezone.utc)
    news_candidates: list[dict] = []
    support_candidates: list[dict] = []
    fallback_candidates: list[dict] = []

    for row in rows:
        action = str(row.action or "").strip()
        target = str(row.target or "").strip()
        if exclude_signature is not None and action == str(exclude_signature[0]) and target == str(exclude_signature[1]):
            continue

        ts = row.event_ts
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                ts = now
        if ts is None:
            ts = now
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        age_h = max(0.0, (now - ts).total_seconds() / 3600.0)
        conf = max(0.0, min(1.0, _to_float(row.conf) or 0.0))
        source = str(row.source or "")
        recency_score = max(0.0, 1.0 - (age_h / 72.0))
        kw_bonus = 0.12 if any(term in (action + " " + target) for term in prefer_terms) else 0.0
        stale_penalty = 0.30 if age_h > 24 else 0.0
        low_signal_penalty = 0.22 if action in {"signal_watch", "information_unavailable"} else 0.0
        score = (1.15 * recency_score) + source_weight.get(source, 0.35) + (0.24 * conf) + kw_bonus - stale_penalty - low_signal_penalty
        item = {
            "event_id": int(row.id),
            "source": source,
            "age_hours": age_h,
            "age_minutes": int(age_h * 60),
            "confidence": conf,
            "score": round(score, 4),
            "action": action,
            "target": target,
        }
        if source in live_news_sources:
            news_candidates.append(item)
        elif source in support_sources:
            support_candidates.append(item)
        else:
            fallback_candidates.append(item)

    news_fresh = [c for c in news_candidates if c["age_hours"] <= 12.0]
    news_recent = [c for c in news_candidates if c["age_hours"] <= 36.0]
    support_recent = [c for c in support_candidates if c["age_hours"] <= 6.0]

    bucket = "news_fresh"
    pick_pool = news_fresh
    if not pick_pool:
        if news_recent:
            pick_pool = news_recent
            bucket = "news_recent"
        elif support_recent:
            pick_pool = support_recent
            bucket = "support_recent"
        elif news_candidates:
            pick_pool = news_candidates
            bucket = "news_stale"
        elif support_candidates:
            pick_pool = support_candidates
            bucket = "support_stale"
        else:
            pick_pool = fallback_candidates
            bucket = "fallback_other"

    if not pick_pool:
        raise ValueError("No eligible structured events after filtering")

    all_candidates = news_candidates + support_candidates + fallback_candidates
    best = max(pick_pool, key=lambda c: c["score"])

    sorted_all = sorted(all_candidates, key=lambda c: c["score"], reverse=True)
    seen_labels: set[str] = set()
    outranked: list[dict] = []
    for c in sorted_all:
        if c["event_id"] == best["event_id"]:
            continue
        label = f"{str(c['action']).replace('_', ' ')}: {str(c['target']).replace('_', ' ')}".strip()
        key = label.lower()
        if key in seen_labels:
            continue
        seen_labels.add(key)
        outranked.append(
            {
                "event_id": c["event_id"],
                "label": label,
                "source": c["source"],
                "score": c["score"],
                "age_minutes": c["age_minutes"],
            }
        )
        if len(outranked) >= 3:
            break

    confidence_label = "High" if best["confidence"] >= 0.72 else ("Medium" if best["confidence"] >= 0.45 else "Low")
    score_100 = int(max(1, min(99, round(best["score"] * 40))))

    live_event = best["source"] in live_news_sources
    stale_fallback = bucket in {"support_recent", "news_stale", "support_stale", "fallback_other"}
    reason = (
        f"bucket={bucket} source={best['source']} age={best['age_minutes']}m conf={best['confidence']:.2f} score={best['score']:.3f}"
    )
    meta = {
        "source": best["source"],
        "age_minutes": best["age_minutes"],
        "score": best["score"],
        "score_100": score_100,
        "bucket": bucket,
        "stale_fallback": stale_fallback,
        "confidence": round(float(best["confidence"]), 3),
        "confidence_label": confidence_label,
        "selected_label": f"{str(best['action']).replace('_', ' ')}: {str(best['target']).replace('_', ' ')}",
        "outranked": outranked,
        "reason": reason,
    }
    return best["event_id"], live_event, meta


def run_refresh_pipeline(
    database_url: str,
    *,
    quick: bool = False,
    run_pipeline: bool = True,
    force: bool = False,
    mission_objective: str | None = None,
    annual_import_budget_usd_bn: float | None = None,
) -> dict:
    guardian_key = os.getenv("GUARDIAN_API_KEY", "").strip()
    newsapi_key = os.getenv("NEWSAPI_KEY", "").strip()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()

    started = perf_counter()
    normalized_objective = _normalize_mission_objective(mission_objective)
    selected_event_meta: dict = {}
    with _REFRESH_LOCK:
        ingest_started = perf_counter()
        ingested_new, errors = _ingest_news_parallel(database_url, guardian_key, newsapi_key, quick)
        ingest_ms = int((perf_counter() - ingest_started) * 1000)

        extract_started = perf_counter()
        if ingested_new == 0 and not force:
            extracted_new, extract_errors = 0, []
        else:
            extracted_new, extract_errors = _extract_structured_events(database_url, gemini_key, quick)
        extract_ms = int((perf_counter() - extract_started) * 1000)
        errors.extend(extract_errors)

        event_id = None
        live_event = False
        pipeline_id = None
        run_ms = 0
        reused_latest = False
        selection_mode = "latest"
        if run_pipeline:
            previous_event_id = None
            latest_state = load_latest_pipeline_state() if not force else None
            if latest_state is not None:
                pipeline_id = latest_state[0]
                previous_event_id = latest_state[1].get("structured_event_id")

            if force or extracted_new > 0:
                event_id, live_event, selected_event_meta = _pick_latest_event_id(database_url)
                selection_mode = "latest"
            else:
                # No fresh extraction: still attempt a re-run from DB to avoid stale cached-only refreshes.
                event_id, live_event, selected_event_meta = _pick_latest_event_id(database_url)
                selection_mode = "latest"
                if previous_event_id is not None and int(previous_event_id) == int(event_id):
                    try:
                        rotated_event_id, rotated_live, rotated_meta = _pick_latest_event_id(database_url, exclude_event_id=int(previous_event_id))
                        event_id, live_event = rotated_event_id, rotated_live
                        selected_event_meta = rotated_meta
                        selection_mode = "rotated_recent"
                    except Exception:
                        reused_latest = True
                        selection_mode = "reused_latest"

                if not reused_latest and previous_event_id is not None:
                    prev_sig = _event_signature(database_url, int(previous_event_id))
                    cur_sig = _event_signature(database_url, int(event_id))
                    if prev_sig is not None and cur_sig is not None and prev_sig == cur_sig:
                        try:
                            distinct_event_id, distinct_live, distinct_meta = _pick_latest_event_id(
                                database_url,
                                exclude_event_id=int(event_id),
                                exclude_signature=prev_sig,
                            )
                            event_id, live_event = distinct_event_id, distinct_live
                            selected_event_meta = distinct_meta
                            selection_mode = "distinct_recent"
                        except Exception:
                            pass

            if previous_event_id is not None and selected_event_meta.get("bucket") in {"support_recent", "support_stale", "fallback_other"}:
                event_id = int(previous_event_id)
                reused_latest = True
                selection_mode = "reused_latest_no_significant_news"

            if selected_event_meta.get("stale_fallback") and not reused_latest:
                selection_mode = "stale_fallback"

            if not reused_latest:
                run_started = perf_counter()
                state = run_pipeline_for_structured_event(
                    database_url,
                    event_id,
                    mission_objective=normalized_objective,
                    annual_import_budget_usd_bn=annual_import_budget_usd_bn,
                )
                run_ms = int((perf_counter() - run_started) * 1000)
                state_payload = state.model_dump()
                save_pipeline_state(state.pipeline_id, state_payload)
                pipeline_id = state.pipeline_id

    total_ms = int((perf_counter() - started) * 1000)
    return {
        "pipeline_id": pipeline_id,
        "structured_event_id": event_id,
        "live_event": live_event,
        "ingested_new": ingested_new,
        "extracted_new": extracted_new,
        "errors": errors,
        "timings_ms": {
            "ingest": ingest_ms,
            "extract": extract_ms,
            "pipeline": run_ms,
            "total": total_ms,
        },
        "quick": quick,
        "reused_latest": reused_latest,
        "selection_mode": selection_mode,
        "selected_event": selected_event_meta,
        "mission_objective": normalized_objective,
        "annual_import_budget_usd_bn": annual_import_budget_usd_bn,
        "status": "ok" if run_pipeline else "warmed",
    }


@router.post("/refresh")
def refresh_ingest_and_run(
    quick: bool = Query(default=False),
    force: bool = Query(default=False),
    mission_objective: str | None = Query(default=None),
    annual_import_budget_usd_bn: float | None = Query(default=None, ge=0),
    _: None = Depends(require_api_key),
) -> dict:
    """Ingest fresh news → extract structured events → run pipeline on latest event.

    Called by the frontend Refresh Panels button so every refresh pulls real live data.
    """
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise HTTPException(status_code=400, detail="DATABASE_URL missing")

    try:
        return run_refresh_pipeline(
            database_url,
            quick=quick,
            run_pipeline=True,
            force=force,
            mission_objective=mission_objective,
            annual_import_budget_usd_bn=annual_import_budget_usd_bn,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def bootstrap_refresh_on_startup() -> dict | None:
    """Warm the pipeline during API startup so first page load has live data."""
    if (os.getenv("BOOTSTRAP_REFRESH_ON_STARTUP", "false").strip().lower() not in {"1", "true", "yes", "on"}):
        return None
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return None
    try:
        return run_refresh_pipeline(database_url, quick=True, run_pipeline=False)
    except Exception:
        # Startup should not crash if an upstream API is down.
        return None


@router.post("/trigger", response_model=PipelineStateResponse)
def trigger_pipeline(
    payload: TriggerPipelineRequest,
    _: None = Depends(require_api_key),
) -> PipelineStateResponse:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise HTTPException(status_code=400, detail="DATABASE_URL missing")

    structured_event_id = payload.structured_event_id

    try:
        state = run_pipeline_for_structured_event(
            database_url,
            structured_event_id,
            mission_objective=_normalize_mission_objective(getattr(payload, "mission_objective", None)),
            annual_import_budget_usd_bn=getattr(payload, "annual_import_budget_usd_bn", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    state_payload = state.model_dump()
    save_pipeline_state(state.pipeline_id, state_payload)
    return PipelineStateResponse(**state_payload)


@router.get("/{pipeline_id}", response_model=PipelineStateResponse)
def get_pipeline_result(pipeline_id: str) -> PipelineStateResponse:
    payload = load_pipeline_state(pipeline_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="pipeline_id not found")
    return PipelineStateResponse(**payload)


@router.get("/{pipeline_id}/details", response_model=PipelineDetailsResponse)
def get_pipeline_details(pipeline_id: str) -> PipelineDetailsResponse:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise HTTPException(status_code=400, detail="DATABASE_URL missing")

    payload = load_pipeline_state(pipeline_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="pipeline_id not found")

    state = PipelineStateResponse(**payload)

    hypothesis = fetch_hypothesis_by_structured_event_id(database_url, state.structured_event_id)
    hypothesis_details = None
    redteam_details = None
    simulations: list[SimulationDetails] = []
    economic: list[RecommendationDetails] = []
    procurement: list[RecommendationDetails] = []
    policy: list[RecommendationDetails] = []
    refinery: list[RecommendationDetails] = []

    event = fetch_structured_event_by_id(database_url, state.structured_event_id)

    if hypothesis is not None:
        causal_chain = hypothesis.reasoning_chain_json
        needs_rebuild = causal_chain is None
        if isinstance(causal_chain, dict):
            steps = causal_chain.get("steps") if isinstance(causal_chain.get("steps"), list) else []
            has_evidence_step = any((s or {}).get("mechanism") == "evidence_check" for s in steps if isinstance(s, dict))
            if not has_evidence_step:
                needs_rebuild = True
        if needs_rebuild and event is not None:
            twin = build_digital_twin(database_url, enable_live_enrichers=False)
            causal_chain = build_chain_from_hypothesis(hypothesis, event, twin).model_dump(mode="json")
        hypothesis_details = HypothesisDetails(
            id=hypothesis.id,
            hypothesis_text=_ops_friendly_hypothesis_text(hypothesis.hypothesis_text, event.target if event else None, causal_chain),
            confidence=hypothesis.confidence,
            reasoning_chain=hypothesis.reasoning_chain,
            causal_chain=causal_chain,
        )
        review = fetch_hypothesis_review_by_hypothesis_id(database_url, hypothesis.id)
        if review is not None:
            redteam_details = RedTeamDetails(
                id=review.id,
                rebuttal_text=review.rebuttal_text,
                counter_confidence=review.counter_confidence,
                reconciled_confidence=review.reconciled_confidence,
                disproof_signals=review.disproof_signals,
            )

        raw_sims = fetch_simulations_by_hypothesis_id(database_url, hypothesis.id)
        simulations = [
            SimulationDetails(
                id=s.id,
                horizon=s.horizon,
                percentiles=s.percentiles,
                distribution=s.distribution,
                metadata=s.metadata,
            )
            for s in raw_sims
            if s.id in state.simulation_ids
        ]

    eco_ids = set(state.economic_recommendation_ids)
    proc_ids = set(state.procurement_recommendation_ids)
    policy_ids = set(state.policy_recommendation_ids)
    refinery_ids = set(state.refinery_recommendation_ids)

    for rec in fetch_recommendations_by_type(database_url, "economic_impact"):
        if rec.id in eco_ids:
            economic.append(
                RecommendationDetails(
                    id=rec.id,
                    simulation_id=rec.simulation_id,
                    recommendation_type=rec.recommendation_type,
                    recommendation_payload=rec.recommendation_payload,
                    score=rec.score,
                )
            )
    for rec in fetch_recommendations_by_type(database_url, "procurement_plan"):
        if rec.id in proc_ids:
            procurement.append(
                RecommendationDetails(
                    id=rec.id,
                    simulation_id=rec.simulation_id,
                    recommendation_type=rec.recommendation_type,
                    recommendation_payload=rec.recommendation_payload,
                    score=rec.score,
                )
            )
    for rec in fetch_recommendations_by_type(database_url, "policy_plan"):
        if rec.id in policy_ids:
            policy.append(
                RecommendationDetails(
                    id=rec.id,
                    simulation_id=rec.simulation_id,
                    recommendation_type=rec.recommendation_type,
                    recommendation_payload=rec.recommendation_payload,
                    score=rec.score,
                )
            )
    for rec in fetch_recommendations_by_type(database_url, "refinery_impact"):
        if rec.id in refinery_ids:
            refinery.append(
                RecommendationDetails(
                    id=rec.id,
                    simulation_id=rec.simulation_id,
                    recommendation_type=rec.recommendation_type,
                    recommendation_payload=rec.recommendation_payload,
                    score=rec.score,
                )
            )

    if hypothesis_details is not None and isinstance(hypothesis_details.causal_chain, dict):
        twin_for_chain = build_digital_twin(database_url, enable_live_enrichers=False)
        market_ctx = fetch_live_market_context(database_url)
        fused_chain = _build_data_fused_chain(
            base_chain=hypothesis_details.causal_chain,
            event=event,
            twin=twin_for_chain,
            market_ctx=market_ctx,
            simulations=simulations,
            economic=economic,
            procurement=procurement,
            policy=policy,
        )
        hypothesis_details.causal_chain = fused_chain
        if isinstance(fused_chain, dict) and isinstance(fused_chain.get("hypothesis_text"), str):
            hypothesis_details.hypothesis_text = fused_chain.get("hypothesis_text") or hypothesis_details.hypothesis_text

    # Surface the annual import budget actually used by the Economic Agent so
    # the War Room UI can show "Economic model ran with budget = $X bn/yr".
    effective_budget: float | None = None
    for rec in economic:
        assumptions = (rec.recommendation_payload or {}).get("assumptions") if isinstance(rec.recommendation_payload, dict) else None
        candidate = _to_float((assumptions or {}).get("annual_import_bill_usd_bn")) if isinstance(assumptions, dict) else None
        if candidate is not None and candidate > 0:
            effective_budget = candidate
            break

    return PipelineDetailsResponse(
        state=state,
        hypothesis=hypothesis_details,
        redteam=redteam_details,
        simulations=simulations,
        economic=economic,
        procurement=procurement,
        policy=policy,
        refinery=refinery,
        effective_import_budget_usd_bn=effective_budget,
    )
