from processing.extraction.gemini_extractor import parse_gemini_json_text


def test_parse_gemini_json_text_plain() -> None:
    text = '{"actors":["Iran"],"action_type":"supply_disruption","target":"shipping_corridor","confidence":0.77,"reasoning":"signal"}'
    out = parse_gemini_json_text(text)
    assert out.action_type == "supply_disruption"
    assert out.target == "shipping_corridor"
    assert out.confidence == 0.77


def test_parse_gemini_json_text_code_fence_and_confidence_clamp() -> None:
    text = "```json\n{\"actors\":[\"A\"],\"action_type\":\"price_shock\",\"target\":\"crude_market\",\"confidence\":5,\"reasoning\":\"x\"}\n```"
    out = parse_gemini_json_text(text)
    assert out.action_type == "price_shock"
    assert out.confidence == 1.0


def test_parse_gemini_json_text_with_preamble() -> None:
    text = "Here is the JSON requested:\n{\"actors\":[\"B\"],\"action_type\":\"sanctions\",\"target\":\"entity_or_vessel\",\"confidence\":0.6,\"reasoning\":\"ok\"}"
    out = parse_gemini_json_text(text)
    assert out.action_type == "sanctions"
    assert out.target == "entity_or_vessel"