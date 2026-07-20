from __future__ import annotations

import json
import time
from dataclasses import dataclass

import requests
from ingestion.connectors._session import post as _post
from pydantic import BaseModel, Field, ValidationError

from ingestion.storage import RawSignalRecord, StructuredEventInput


class GeminiExtraction(BaseModel):
    actors: list[str] = Field(default_factory=list)
    action_type: str
    target: str
    confidence: float
    reasoning: str = ""


@dataclass
class GeminiConfig:
    api_key: str
    model: str = "gemini-2.5-flash"
    timeout_seconds: int = 30
    max_retries: int = 3


def _coerce_json_text(text: str) -> dict:
    cleaned = text.strip()
    if "{" in cleaned and "}" in cleaned:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            cleaned = cleaned[start : end + 1]
    return json.loads(cleaned)


def parse_gemini_json_text(text: str) -> GeminiExtraction:
    payload = _coerce_json_text(text)
    parsed = GeminiExtraction.model_validate(payload)
    parsed.confidence = max(0.0, min(1.0, parsed.confidence))
    return parsed


def _build_prompt(record: RawSignalRecord) -> str:
    payload = record.raw_payload or {}
    # Only include short text fields — content/description can be very long
    title = (payload.get("webTitle") or payload.get("title") or "")[:200]
    desc = (payload.get("description") or "")[:300]
    url = (payload.get("url") or payload.get("webUrl") or "")[:120]
    compact_text = f"title={title!r} description={desc!r} url={url!r}"
    return (
        "Extract one energy-security event from this news signal as JSON. "
        "Return ONLY a JSON object. No markdown, no explanation. "
        "Keys: actors (array of 1-4 strings), action_type (string, snake_case, max 30 chars), "
        "target (string, max 50 chars), confidence (float 0-1), reasoning (string, max 80 chars). "
        f"source={record.source}; hints={record.entities_hint[:5]}; {compact_text}"
    )


def _call_gemini(prompt: str, config: GeminiConfig) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.model}:generateContent"
    params = {"key": config.api_key}
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 256,
            "responseMimeType": "application/json",
        },
    }
    response = _post(url, params=params, json=body, timeout=config.timeout_seconds)
    response.raise_for_status()
    payload = response.json()

    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Gemini response missing candidates")
    finish = candidates[0].get("finishReason")
    if isinstance(finish, str) and finish in {"MAX_TOKENS", "SAFETY", "RECITATION"}:
        raise ValueError(f"Gemini generation incomplete: {finish}")
    content = candidates[0].get("content", {})
    parts = content.get("parts", []) if isinstance(content, dict) else []
    if not isinstance(parts, list) or not parts:
        raise ValueError("Gemini response missing content parts")
    text_chunks = []
    for part in parts:
        if isinstance(part, dict):
            txt = part.get("text")
            if isinstance(txt, str) and txt.strip():
                text_chunks.append(txt)
    text = "\n".join(text_chunks).strip()
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Gemini response missing text")
    return text


def extract_structured_event_gemini(record: RawSignalRecord, config: GeminiConfig) -> StructuredEventInput:
    prompt = _build_prompt(record)
    last_error: Exception | None = None
    parsed = None
    for attempt in range(config.max_retries):
        try:
            text = _call_gemini(prompt, config)
            parsed = parse_gemini_json_text(text)
            break
        except (requests.RequestException, ValidationError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < config.max_retries - 1:
                time.sleep(0.4 * (attempt + 1))
    if parsed is None:
        raise ValueError(f"Gemini extraction failed after retries: {last_error}")

    actors = parsed.actors if parsed.actors else record.entities_hint[:10]
    return StructuredEventInput(
        raw_signal_id=record.id,
        event_ts=record.signal_ts,
        action_type=parsed.action_type,
        target=parsed.target,
        confidence=parsed.confidence,
        actors=actors,
        extracted_payload={
            "source": record.source,
            "source_id": record.source_id,
            "reasoning": parsed.reasoning,
            "model": config.model,
            "extraction_mode": "gemini",
        },
    )


def try_extract_structured_event_gemini(record: RawSignalRecord, config: GeminiConfig) -> StructuredEventInput | None:
    try:
        return extract_structured_event_gemini(record, config)
    except (requests.RequestException, ValidationError, ValueError, json.JSONDecodeError):
        return None
