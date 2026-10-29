import pytest

from patient360.domain.builders.model_input import build_model_input
from patient360.prompts import PROMPT_AND_SCHEMA_BY_SOURCE

SOURCES = ["urgent", "on-demand", "batch", "symptom-checker"]


def test_every_source_has_a_prompt_and_a_schema():
    assert sorted(PROMPT_AND_SCHEMA_BY_SOURCE) == sorted(SOURCES)
    for prompt, schema in PROMPT_AND_SCHEMA_BY_SOURCE.values():
        assert prompt.strip() and schema["type"] == "object" and schema["properties"]


def test_urgent_and_on_demand_each_have_their_own_prompt():
    """The owner made the two texts the same; the source reaches the model in the input JSON (``source`` key)."""
    assert PROMPT_AND_SCHEMA_BY_SOURCE["urgent"][0].strip() and PROMPT_AND_SCHEMA_BY_SOURCE["on-demand"][0].strip()


@pytest.mark.parametrize("source", SOURCES)
def test_the_prompt_uses_the_names_our_ai_json_really_has(source):
    prompt, _ = PROMPT_AND_SCHEMA_BY_SOURCE[source]
    assert "age_days" in prompt and "age_years" not in prompt
    assert "PreparationName" not in prompt and "previous_value" not in prompt and "{test:" not in prompt


@pytest.mark.parametrize("source", SOURCES)
def test_a_model_input_can_be_built_for_every_source(source):
    prompt, schema = PROMPT_AND_SCHEMA_BY_SOURCE[source]
    request = build_model_input(system_prompt=prompt, output_schema=schema, processed={"patient": {}})
    assert request["system"][0]["text"] == prompt and not prompt.startswith('"')
