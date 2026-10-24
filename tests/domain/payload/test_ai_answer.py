import json

import pytest
from pydantic import ValidationError

from patient360.domain.payload.ai_answer import AiAnswer


def both(english):
    return {"en": english, "ar": f"AR-{english}"}


def answer(**changes):
    base = {"summary": both("s"), "simple_summary": both("ss"), "critical_alerts": [], "note": both("n"),
            "recommendations": []}
    return {**base, **changes}


def test_a_good_answer_is_kept_as_it_is():
    data = answer(critical_alerts=[both("a")])
    assert AiAnswer.model_validate_json(json.dumps(data)).for_backend() == data


def test_keys_we_do_not_know_are_dropped_so_nothing_internal_can_leak():
    data = answer(model="model-x", usage={"input_tokens": 1})
    assert "model" not in AiAnswer.model_validate_json(json.dumps(data)).for_backend()


def test_a_symptom_answer_keeps_change_type_and_explanation():
    rec = {"test_name": "T", "priority": both("p"), "reason": both("r"), "timing": both("t"), "guideline": both("g"),
           "advisory": {"level": both("none"), "message": both("m")},
           "change_type": both("kept"), "explanation": both("e")}
    out = AiAnswer.model_validate_json(json.dumps(answer(recommendations=[rec]))).for_backend()
    assert out["recommendations"][0]["change_type"] == both("kept")


@pytest.mark.parametrize("data", [
    {"summary": "just text"},
    answer(summary={"en": "no arabic"}),
    answer(summary={"en": 5, "ar": "x"}),
    answer(note=None),
])
def test_a_wrong_shape_is_refused(data):
    with pytest.raises(ValidationError):
        AiAnswer.model_validate_json(json.dumps(data))
