import json

import pytest

from patient360.domain.builders.model_input import build_model_input

SCHEMA = {"type": "object", "properties": {}}
ARABIC = "عربي"


def test_the_request_holds_the_prompt_the_processed_json_and_the_schema():
    processed = {"patient": {"sex": "Female"}, "note": ARABIC}
    out = build_model_input(system_prompt="Be careful.", output_schema=SCHEMA, processed=processed)
    assert out["system"] == [{"type": "text", "text": "Be careful."}]
    assert out["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert (out["anthropic_version"], out["max_tokens"]) == ("bedrock-2023-05-31", 2048)
    [message] = out["messages"]
    assert message["role"] == "user"
    assert json.loads(message["content"][0]["text"]) == processed
    assert ARABIC in message["content"][0]["text"]   # not escaped


@pytest.mark.parametrize(("prompt", "schema"), [("", SCHEMA), ("   ", SCHEMA), ("Be careful.", {})])
def test_an_empty_prompt_or_schema_is_refused(prompt, schema):
    with pytest.raises(ValueError):
        build_model_input(system_prompt=prompt, output_schema=schema, processed={})

