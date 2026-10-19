import copy

import pytest

from patient360.domain.public import to_public

CASES = ["01-batch-normal", "02-batch-critical", "03-urgent", "04-on-demand"]


@pytest.mark.parametrize("case", CASES)
def test_public_output_drops_model_ids_and_usage_and_keeps_the_rest(flow, case):
    internal = flow(f"{case}/5-ai-service-output.json")
    public = to_public(internal)
    assert not ({"model_id", "translation_model_id", "usage"} & set(public["meta"]))
    assert public["meta"]["generated_at"] == internal["meta"]["generated_at"]
    assert {k: v for k, v in public.items() if k != "meta"} == {k: v for k, v in internal.items() if k != "meta"}


def test_input_is_not_mutated(flow):
    internal = flow("02-batch-critical/5-ai-service-output.json")
    before = copy.deepcopy(internal)
    to_public(internal)
    assert internal == before


def test_a_record_without_meta_is_returned_unchanged():
    assert to_public({"status": "error"}) == {"status": "error"}
