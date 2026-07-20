from __future__ import annotations

import json
from dataclasses import dataclass

import requests
from ingestion.connectors._session import post as _post
from pydantic import BaseModel, Field, ValidationError

from agents.reasoning_chain import build_causal_chain
from digital_twin.graph_state import DigitalTwinState
from ingestion.storage import HypothesisInput, StructuredEventRecord


class HypothesisPayload(BaseModel):
    hypothesis: str
    confidence: float
    reasoning_chain: list[str] = Field(default_factory=list)


@dataclass
class HypothesisAgentConfig:
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


def _build_prompt(
    event: StructuredEventRecord,
    twin: DigitalTwinState | None = None,
    live_ctx: dict | None = None,
    mission_objective: str = "balanced_resilience",
) -> str:
    payload_text = json.dumps(event.extracted_payload or {}, ensure_ascii=False)
    twin_ctx = ""
    if twin is not None:
        chokepoints = ", ".join(f"{cp.name}(risk={cp.risk_score:.2f}, {cp.throughput_mbd:.0f}Mbd)" for cp in twin.chokepoints[:5])
        refineries = ", ".join(f"{r.name}({r.capacity_kbd}kbd,{r.operator})" for r in twin.refineries[:6])
        grades = ", ".join(f"{g.name}(from {g.source_country_iso3})" for g in twin.crude_grades[:8])
        twin_ctx = (
            " Digital Twin world-model: "
            f"chokepoints=[{chokepoints}]; "
            f"key_indian_refineries=[{refineries}]; "
            f"crude_grades=[{grades}]."
        )

    market_ctx = ""
    if live_ctx:
        parts = []
        if live_ctx.get("brent_usd"):
            parts.append(f"Brent crude is currently at ${live_ctx['brent_usd']}/barrel")
        if live_ctx.get("wti_usd"):
            parts.append(f"WTI at ${live_ctx['wti_usd']}/barrel")
        if live_ctx.get("eia_note"):
            parts.append(live_ctx["eia_note"])
        if live_ctx.get("recent_headlines"):
            parts.append("Recent market headlines: " + " | ".join(live_ctx["recent_headlines"][:3]))
        if parts:
            market_ctx = " Live market context: " + "; ".join(parts) + "."

    mission = str(mission_objective or "balanced_resilience").strip().lower()
    mission_ctx = (
        " Mission objective: balance resilience and import cost."
        if mission in {"balanced_resilience", "balanced"}
        else ""
    )
    if mission in {"maximize_supply_resilience", "resilience", "security"}:
        mission_ctx = " Mission objective: maximize supply resilience and continuity under disruption."
    elif mission in {"minimize_import_cost", "cost", "optimize_cost"}:
        mission_ctx = " Mission objective: minimize import cost and avoid unnecessary defensive escalation."
    elif mission in {"maintain_import_coverage", "coverage"}:
        mission_ctx = " Mission objective: maintain import coverage and refinery throughput reliability."

    return (
        "You are a senior energy-market analyst writing for a government policy briefing. "
        "Given the structured event below, generate ONE clear geopolitical-energy hypothesis "
        "and a reasoning_chain that traces the event step-by-step through to India's energy security. "
        "RULES: (1) Write in plain English a non-expert can understand — no jargon, no acronyms without explanation. "
        "(2) Each step in reasoning_chain must be a complete sentence describing ONE cause-and-effect link. "
        "(3) Start with the event trigger, then explain what happens to shipping/chokepoints, "
        "then crude supply, then Indian refineries, then the economic impact on India. "
        "(4) Be specific — name real entities from the Digital Twin context when relevant. "
        "(5) Return ONLY a JSON object with keys: hypothesis (string), confidence (0..1), reasoning_chain (array of strings, 5-8 steps). "
        f"Event: action_type={event.action_type}; target={event.target}; actors={event.actors[:5]}; "
        f"payload={payload_text[:1500]}.{twin_ctx}{market_ctx}{mission_ctx}"
    )


def _call_gemini(prompt: str, config: HypothesisAgentConfig) -> str:
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


def _normalize_payload(payload: HypothesisPayload) -> HypothesisPayload:
    payload.confidence = max(0.0, min(1.0, payload.confidence))
    if not payload.reasoning_chain:
        payload.reasoning_chain = ["Signal pattern implies elevated risk"]
    return payload


def _deterministic_hypothesis(
    event: StructuredEventRecord,
    twin: DigitalTwinState | None = None,
    mission_objective: str = "balanced_resilience",
) -> HypothesisInput:
    action = (event.action_type or "signal_watch").strip()
    target = (event.target or "energy_supply_chain").strip()
    actors = ", ".join(event.actors[:3]) if event.actors else "key actors"
    confidence = event.confidence if event.confidence is not None else 0.6
    confidence = max(0.0, min(1.0, confidence))

    blob = " ".join([action, target, " ".join(event.actors or [])]).lower()
    india_linked = any(k in blob for k in ("india", "indian", "hormuz", "arabian sea", "red sea", "opec", "iran", "iraq", "saudi"))

    if india_linked:
        hypothesis = (
            f"{action} involving {actors} may tighten crude logistics that matter to India, "
            "raising near-term freight and procurement risk for Indian refiners."
        )
        reasoning_chain = [
            "The trigger is directly linked to routes or suppliers relevant for Indian imports.",
            "If freight costs rise or cargoes are delayed, refinery feedstock planning becomes harder.",
            "This can push up import cost and domestic fuel pricing pressure.",
        ]
    else:
        confidence = min(confidence, 0.35)
        hypothesis = (
            f"{action} in {target} is primarily an external market signal. "
            "Direct impact on India's crude security is currently weak and should be treated as a watchlist item "
            "unless confirmed by shipping, freight, or supplier disruption evidence."
        )
        reasoning_chain = [
            "The signal is real, but it is not yet tied to Indian import routes or Indian refinery operations.",
            "No confirmed disruption is visible in key shipping corridors from this event alone.",
            "Escalate only if AIS flow, chokepoint risk, or freight indicators deteriorate.",
        ]

    mission = str(mission_objective or "balanced_resilience").strip().lower()
    if mission in {"maximize_supply_resilience", "resilience", "security"}:
        reasoning_chain.append("Given the resilience mission, prioritize continuity safeguards over cost optimization.")
    elif mission in {"minimize_import_cost", "cost", "optimize_cost"}:
        reasoning_chain.append("Given the cost mission, avoid overreacting unless logistics stress is directly confirmed.")
    elif mission in {"maintain_import_coverage", "coverage"}:
        reasoning_chain.append("Given the coverage mission, focus on stable throughput and feedstock continuity.")

    reasoning_chain_json: dict | None = None
    if twin is not None:
        chain = build_causal_chain(
            event,
            twin,
            hypothesis_text=hypothesis,
            confidence=confidence,
            raw_reasoning_steps=reasoning_chain,
            source="deterministic",
        )
        reasoning_chain = chain.as_bullet_lines()
        reasoning_chain_json = chain.model_dump(mode="json")
    return HypothesisInput(
        structured_event_id=event.id,
        hypothesis_text=hypothesis,
        confidence=confidence,
        reasoning_chain=reasoning_chain,
        reasoning_chain_json=reasoning_chain_json,
        model_name="deterministic_v1",
    )


def _gemini_hypothesis(
    event: StructuredEventRecord,
    config: HypothesisAgentConfig,
    twin: DigitalTwinState | None = None,
    live_ctx: dict | None = None,
) -> HypothesisInput:
    text = _call_gemini(_build_prompt(event, twin, live_ctx, config.mission_objective), config)
    payload = HypothesisPayload.model_validate(_coerce_json_text(text))
    payload = _normalize_payload(payload)
    reasoning_chain = list(payload.reasoning_chain)
    reasoning_chain_json: dict | None = None
    if twin is not None:
        chain = build_causal_chain(
            event,
            twin,
            hypothesis_text=payload.hypothesis,
            confidence=payload.confidence,
            raw_reasoning_steps=reasoning_chain,
            source="llm",
        )
        reasoning_chain = chain.as_bullet_lines()
        reasoning_chain_json = chain.model_dump(mode="json")
    return HypothesisInput(
        structured_event_id=event.id,
        hypothesis_text=payload.hypothesis,
        confidence=payload.confidence,
        reasoning_chain=reasoning_chain,
        reasoning_chain_json=reasoning_chain_json,
        model_name=config.model,
    )


def generate_hypothesis(
    event: StructuredEventRecord,
    config: HypothesisAgentConfig,
    twin: DigitalTwinState | None = None,
    live_ctx: dict | None = None,
) -> HypothesisInput:
    if config.mode == "deterministic":
        return _deterministic_hypothesis(event, twin, config.mission_objective)

    if config.mode == "gemini":
        if not config.api_key:
            raise ValueError("GEMINI_API_KEY missing for gemini mode")
        return _gemini_hypothesis(event, config, twin, live_ctx)

    if config.api_key:
        try:
            return _gemini_hypothesis(event, config, twin, live_ctx)
        except Exception:
            return _deterministic_hypothesis(event, twin, config.mission_objective)
    return _deterministic_hypothesis(event, twin, config.mission_objective)

