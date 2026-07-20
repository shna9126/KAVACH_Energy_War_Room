from __future__ import annotations

import json
from dataclasses import dataclass

import requests
from ingestion.connectors._session import post as _post
from pydantic import BaseModel, Field, ValidationError

from ingestion.storage import HypothesisRecord, HypothesisReviewInput


class RedTeamPayload(BaseModel):
    rebuttal_text: str
    counter_confidence: float
    disproof_signals: list[str] = Field(default_factory=list)


@dataclass
class RedTeamAgentConfig:
    mode: str = "auto"  # auto | gemini | deterministic
    api_key: str = ""
    model: str = "gemini-2.5-pro"
    timeout_seconds: int = 45
    mission_objective: str = "balanced_resilience"


def _coerce_json_text(text: str) -> dict:
    cleaned = text.strip()
    if "{" in cleaned and "}" in cleaned:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            cleaned = cleaned[start : end + 1]
    return json.loads(cleaned)


def _build_prompt(hypothesis: HypothesisRecord, mission_objective: str = "balanced_resilience") -> str:
    chain_json = hypothesis.reasoning_chain_json if isinstance(hypothesis.reasoning_chain_json, dict) else {}
    chain_steps = chain_json.get("steps") if isinstance(chain_json.get("steps"), list) else []
    compact_steps = []
    for s in chain_steps[:7]:
        if not isinstance(s, dict):
            continue
        compact_steps.append(
            {
                "step_no": s.get("step_no"),
                "mechanism": s.get("mechanism"),
                "evidence_type": s.get("evidence_type"),
                "claim": str(s.get("claim", ""))[:180],
                "sources": s.get("source_labels") or [],
            }
        )

    mission = str(mission_objective or "balanced_resilience").strip().lower()
    mission_ctx = "balanced_resilience"
    if mission in {"maximize_supply_resilience", "resilience", "security"}:
        mission_ctx = "maximize_supply_resilience"
    elif mission in {"minimize_import_cost", "cost", "optimize_cost"}:
        mission_ctx = "minimize_import_cost"
    elif mission in {"maintain_import_coverage", "coverage"}:
        mission_ctx = "maintain_import_coverage"

    return (
        "You are a red-team analyst for energy supply-chain intelligence. "
        "Your rebuttal must be specific to the provided hypothesis context and must NOT reuse generic boilerplate. "
        "Find where the causal chain could break (substitution, inventories, policy buffers, routing, timing mismatch). "
        "Return JSON object only with keys rebuttal_text (string), counter_confidence (0..1), disproof_signals (array of 3-5 concise strings). "
        "Do not invent exact numbers unless they are already in the context. "
        f"Mission objective lens={mission_ctx}. "
        f"hypothesis_text={hypothesis.hypothesis_text}; confidence={hypothesis.confidence}; "
        f"reasoning_chain={hypothesis.reasoning_chain[:8]}; structured_chain_steps={compact_steps}"
    )


def _call_gemini(prompt: str, config: RedTeamAgentConfig) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.model}:generateContent"
    response = _post(
        url,
        params={"key": config.api_key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 900,
            },
        },
        timeout=config.timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Gemini response missing candidates")
    content = candidates[0].get("content", {})
    parts = content.get("parts", []) if isinstance(content, dict) else []
    if not isinstance(parts, list) or not parts:
        raise ValueError("Gemini response missing parts")
    text = "\n".join([p.get("text", "") for p in parts if isinstance(p, dict)]).strip()
    if not text:
        raise ValueError("Gemini response missing text")
    return text


def _normalize_reconciled(base_conf: float | None, counter_conf: float | None) -> float:
    b = float(base_conf) if isinstance(base_conf, (int, float)) else 0.6
    c = float(counter_conf) if isinstance(counter_conf, (int, float)) else 0.5
    b = max(0.0, min(1.0, b))
    c = max(0.0, min(1.0, c))
    # Penalize original confidence by strength of rebuttal.
    reconciled = b * (1.0 - 0.55 * c)
    return max(0.0, min(1.0, reconciled))


def _deterministic_redteam(hypothesis: HypothesisRecord, mission_objective: str = "balanced_resilience") -> HypothesisReviewInput:
    """Context-aware deterministic fallback.

    Derives a rebuttal specific to the hypothesis text and confidence level
    so the output at least reflects the actual event rather than boilerplate.
    """
    text = (hypothesis.hypothesis_text or "").lower()
    conf = float(hypothesis.confidence or 0.6)
    reasoning = [str(r).lower() for r in (hypothesis.reasoning_chain or [])]
    chain_json = hypothesis.reasoning_chain_json if isinstance(hypothesis.reasoning_chain_json, dict) else {}
    steps = chain_json.get("steps") if isinstance(chain_json.get("steps"), list) else []
    mission = str(mission_objective or "balanced_resilience").strip().lower()

    step_blob = " ".join(
        str(s.get("mechanism", "")) + " " + str(s.get("claim", ""))
        for s in steps
        if isinstance(s, dict)
    ).lower()
    source_labels = []
    for s in steps:
        if isinstance(s, dict) and isinstance(s.get("source_labels"), list):
            source_labels.extend([str(x) for x in s.get("source_labels") if str(x).strip()])

    observed_steps = sum(1 for s in steps if isinstance(s, dict) and str(s.get("evidence_type", "")).lower() == "observed")
    predicted_steps = sum(1 for s in steps if isinstance(s, dict) and str(s.get("evidence_type", "")).lower() == "predicted")

    # ---- Classify the event type from hypothesis text -------------------------
    is_sanctions = any(w in (text + " " + step_blob) for w in ("sanction", "ofac", "blacklist", "embargo"))
    is_conflict = any(w in text for w in ("military", "attack", "strike", "conflict", "war", "missile"))
    is_chokepoint = any(w in text for w in ("hormuz", "suez", "bab-el", "malacca", "bosphorus"))
    is_price = any(w in (text + " " + step_blob) for w in ("price", "brent", "wti", "crude", "barrel", "benchmark"))
    is_weather = any(w in text for w in ("cyclone", "storm", "flood", "hurricane", "typhoon"))
    is_supply = any(w in text for w in ("opec", "output", "cut", "production", "spare capacity"))
    is_shipping = any(w in (text + " " + step_blob + " " + " ".join(reasoning)) for w in ("shipping", "route", "ais", "tanker", "port", "freight", "insurance"))
    is_inventory = any(w in (text + " " + step_blob) for w in ("inventory", "stock", "distillate", "draw", "build", "eia"))

    # Counter confidence adapts to evidence balance in the structured chain.
    evidence_strength = min(1.0, 0.35 + 0.08 * observed_steps + 0.04 * len(set(source_labels)))
    predictive_penalty = min(0.2, 0.03 * predicted_steps)
    # Stronger observed/source evidence -> weaker red-team confidence.
    dynamic_counter = max(0.25, min(0.72, 0.62 - 0.25 * evidence_strength + predictive_penalty))
    if mission in {"maximize_supply_resilience", "resilience", "security"}:
        dynamic_counter = min(0.78, dynamic_counter + 0.05)
    elif mission in {"minimize_import_cost", "cost", "optimize_cost"}:
        dynamic_counter = max(0.22, dynamic_counter - 0.04)

    # ---- Build a specific rebuttal --------------------------------------------
    if is_sanctions:
        rebuttal = (
            "Sanctions on individual entities rarely produce sustained supply disruption: "
            "shipping companies reroute via flag-of-convenience vessels, traders use "
            "intermediary jurisdictions, and state-owned refiners often hold forward cover "
            "that bridges any short-term gap. The hypothesis overstates near-term impact if "
            "no secondary sanctions target the vessel owners or insurers."
        )
        disproof = [
            "No corresponding restriction on P&I insurance coverage for affected routes",
            "Flag-of-convenience substitution observed in AIS data within 72 hours",
            "No corroborating OFAC designation on cargo receivers or terminal operators",
            f"Hypothesis confidence is {conf:.0%} — room for market absorption remains",
        ]
        counter_conf = 0.48
    elif is_conflict:
        rebuttal = (
            "Kinetic events in energy corridors historically produce sharp but short-lived "
            "freight spikes rather than prolonged supply cuts. Tanker operators reroute within "
            "days, and strategic reserves (IEA/SPR) act as a buffer if disruption exceeds "
            "10 days. The impact on Indian refinery feedstock may be limited if spot cargoes "
            "can be sourced via Cape of Good Hope at 12–15 days additional transit."
        )
        disproof = [
            "IEA member states hold 90+ days of import cover — activation threshold not yet met",
            "Cape of Good Hope route available at +12 d transit / +$0.8/bbl freight premium",
            "No confirmed physical damage to loading terminals or pipeline infrastructure",
            "Brent–Dubai spread has not widened to levels historically associated with rationing",
        ]
        counter_conf = 0.55
    elif is_chokepoint:
        rebuttal = (
            "Full chokepoint closure is extremely rare and historically brief: the Strait of Hormuz "
            "has never been fully closed despite multiple crises. Alternative routes exist — "
            "IPSA/Petroline bypass for Saudi crude, Fujairah as an alternative export hub — "
            "absorbing up to 30% of Hormuz throughput. The hypothesis may reflect a worst-case "
            "rather than a base-case scenario."
        )
        disproof = [
            "IPSA Petroline bypass capacity: ~5 mbd — covers roughly 30% of Hormuz flow",
            "Fujairah export hub operational — no reported infrastructure degradation",
            "No IRGC naval exercise unusual enough to trigger formal closure notice",
            "US 5th Fleet presence unchanged — deterrence remains credible",
        ]
        counter_conf = 0.50
    elif is_weather:
        rebuttal = (
            "Weather disruptions to port operations typically last 3–10 days before normality "
            "resumes. Offshore loading platforms are designed for extreme weather; onshore "
            "storage can bridge the gap. Insurance and force-majeure clauses prevent long-term "
            "contract defaults, limiting the impact to short-term price volatility rather than "
            "a structural supply deficit."
        )
        disproof = [
            "Offshore SPM buoys rated for category 4+ conditions — temporary suspension only",
            "Port authority pre-storm inventory loading typically buffers 5–7 days",
            "Historical weather events in region show median recovery under 8 days",
        ]
        counter_conf = 0.45
    elif is_inventory and is_price:
        rebuttal = (
            "Inventory-driven signals often transmit first through expectations and spreads, but pass-through to India "
            "can be diluted by term-contract coverage, refinery inventory buffers, and lag between benchmark moves and "
            "actual procurement settlement. The hypothesis may overstate immediate operational disruption even if price "
            "pressure appears in the benchmark channel."
        )
        disproof = [
            "No concurrent AIS-based throughput deterioration in India-bound corridors",
            "Refinery intake/utilization data has not yet shown synchronized stress",
            "Contracted cargo coverage can absorb short-lived benchmark volatility",
            f"Observed-evidence share in chain suggests measured confidence calibration (base {conf:.0%})",
        ]
        counter_conf = dynamic_counter
    elif is_shipping:
        rebuttal = (
            "Shipping risk does not always convert into supply loss: operators can reroute, charter substitution can occur, "
            "and port congestion spikes often normalize before refinery feedstock is materially affected. The hypothesis should "
            "separate route friction from confirmed throughput impairment."
        )
        disproof = [
            "No sustained multi-day AIS anomaly confirming route-level throughput collapse",
            "Freight and war-risk premia have not crossed stress-regime thresholds",
            "Alternative supplier/route mix remains available for short-horizon mitigation",
            "Policy buffers (inventory + SPR readiness) reduce immediate disruption probability",
        ]
        counter_conf = dynamic_counter
    elif is_supply or is_price:
        rebuttal = (
            "OPEC+ spare capacity (~3–4 mbd as of current estimates) provides a meaningful "
            "supply buffer. Saudi Arabia and UAE have demonstrated willingness to increase "
            "output during price spikes. Indian refiners also hold term contracts that insulate "
            "them from spot market volatility over 30–60 day horizons."
        )
        disproof = [
            "Saudi Arabia holds 2+ mbd of sustainable spare capacity per IEA estimates",
            "Indian refiners cover 60–70% of demand via long-term contracts — spot exposure limited",
            "OPEC+ meeting scheduled — coordinated output response mechanism already exists",
        ]
        counter_conf = dynamic_counter
    else:
        # Structured generic fallback built from available context, not fixed template text.
        src_count = len(set(source_labels))
        rebuttal = (
            "The proposed causal path may still be over-coupled: substitution channels, inventory buffers, and policy "
            "interventions can absorb part of the shock before it propagates to refinery operations. "
            f"Current chain shows {observed_steps} observed vs {predicted_steps} predicted steps, so near-term impact should "
            "be treated as conditional rather than deterministic."
        )
        disproof = [
            "Route-level confirmation remains incomplete without sustained logistics deterioration",
            "No broad cross-indicator confirmation yet across freight, receipts, and utilization",
            f"Evidence mix spans {src_count} source families, implying unresolved uncertainty bands",
            "Intervention levers (supplier switching, inventory draw, policy response) remain available",
        ]
        counter_conf = dynamic_counter

    if mission in {"maximize_supply_resilience", "resilience", "security"}:
        disproof.append("Resilience-first posture still requires early hard evidence before assuming prolonged shortages")
    elif mission in {"minimize_import_cost", "cost", "optimize_cost"}:
        disproof.append("Cost-first posture should challenge expensive interventions unless disruption persistence is verified")
    elif mission in {"maintain_import_coverage", "coverage"}:
        disproof.append("Coverage-first posture should test whether throughput continuity can be preserved via supplier switching")

    reconciled = _normalize_reconciled(hypothesis.confidence, counter_conf)
    return HypothesisReviewInput(
        hypothesis_id=hypothesis.id,
        rebuttal_text=rebuttal,
        counter_confidence=counter_conf,
        disproof_signals=disproof,
        reconciled_confidence=round(reconciled, 4),
        model_name="deterministic_redteam_v2",
    )


def _gemini_redteam(hypothesis: HypothesisRecord, config: RedTeamAgentConfig) -> HypothesisReviewInput:
    text = _call_gemini(_build_prompt(hypothesis, config.mission_objective), config)
    payload = RedTeamPayload.model_validate(_coerce_json_text(text))
    counter_conf = max(0.0, min(1.0, payload.counter_confidence))
    disproof = payload.disproof_signals if payload.disproof_signals else ["Insufficient contradictory evidence currently"]
    reconciled = _normalize_reconciled(hypothesis.confidence, counter_conf)

    return HypothesisReviewInput(
        hypothesis_id=hypothesis.id,
        rebuttal_text=payload.rebuttal_text,
        counter_confidence=counter_conf,
        disproof_signals=disproof,
        reconciled_confidence=round(reconciled, 4),
        model_name=config.model,
    )


def generate_redteam_review(hypothesis: HypothesisRecord, config: RedTeamAgentConfig) -> HypothesisReviewInput:
    if config.mode == "deterministic":
        return _deterministic_redteam(hypothesis, config.mission_objective)

    if config.mode == "gemini":
        if not config.api_key:
            raise ValueError("GEMINI_API_KEY missing for gemini mode")
        return _gemini_redteam(hypothesis, config)

    if config.api_key:
        try:
            return _gemini_redteam(hypothesis, config)
        except (requests.RequestException, ValidationError, ValueError, json.JSONDecodeError):
            return _deterministic_redteam(hypothesis, config.mission_objective)
    return _deterministic_redteam(hypothesis, config.mission_objective)
