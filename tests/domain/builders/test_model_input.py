import json

import pytest

from patient360.domain.builders.model_input import aws_record, build_model_input, single_visit_file

SCHEMA = {"type": "object", "properties": {}}
ARABIC = "عربي"


def test_a_single_visit_file_is_flat_with_the_cached_prompt_and_no_guardrail_key():
    bare = build_model_input(system_prompt="Be careful.", output_schema=SCHEMA, processed={"patient": {}})
    out = single_visit_file("abc", bare)
    assert list(out) == ["recordId", "anthropic_version", "max_tokens", "system", "messages", "output_config"]
    assert out["recordId"] == "abc" and "modelInput" not in out and out["messages"] == bare["messages"]
    cached = {"type": "text", "text": "Be careful.", "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    assert out["system"] == [cached]
    assert "cache_control" not in bare["system"][0]     # the database copy and the batch line stay plain


def test_a_batch_line_keeps_the_model_input_wrapper():
    bare = build_model_input(system_prompt="Be careful.", output_schema=SCHEMA, processed={})
    assert aws_record("abc", bare) == {"recordId": "abc", "modelInput": bare}


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

